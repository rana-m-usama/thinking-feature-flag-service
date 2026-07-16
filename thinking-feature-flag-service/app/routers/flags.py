"""Feature flag CRUD and audit history.

Every query in this module filters on `tenant_id` from the authenticated key, never
from the path alone. `require_tenant` has already proven the key owns the path tenant;
the redundant filter means a bug in the guard still cannot leak another tenant's rows.
"""

import uuid
from datetime import UTC, datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app import cache
from app.db import get_db
from app.models import (
    ApiKey,
    AuditAction,
    Environment,
    Flag,
    FlagAuditLog,
    FlagEnvironmentConfig,
    FlagType,
)
from app.schemas import AuditEntry, FlagCreate, FlagResponse, FlagUpdate, _value_matches_type
from app.security import require_tenant

router = APIRouter(prefix="/api/v1/tenants/{tenant_id}/flags", tags=["flags"])


def _drop_unchanged(entity: Any, changes: dict[str, Any]) -> dict[str, Any]:
    """Keep only the fields whose value actually differs from what is stored.

    Without this, `PUT {"rollout_percentage": 25}` against a config already at 25 writes
    an audit record reading `old={"rollout_percentage": 25} new={"rollout_percentage": 25}`.
    That is a lie by omission: the audit trail exists to answer "what changed", and a
    record whose old and new values are identical asserts that something did. During an
    incident it sends someone hunting a change that never happened, and it lets a busy
    idempotent client (a CI job re-applying desired state every deploy, say) bury the
    real history under thousands of phantom entries.

    Idempotent PUTs are normal and correct. Recording them as changes is not.
    """
    return {k: v for k, v in changes.items() if getattr(entity, k) != v}


def _seed_value(flag_type: FlagType, default_value: Any) -> Any:
    """Choose the initial `value` for a new flag's environment configs.

    A flag is created with one value — `default_value`, the "off" value. But the config
    row needs an "on" value too, and the spec's create payload has nowhere to put it.

    For booleans the on value is unambiguous: the negation. A flag defaulting to False
    turns on to True; a kill switch defaulting to True turns "on" to False.

    For strings and numbers there is no inverse, so we seed the default and the caller
    sets the real variant via PUT before enabling. Seeding the default means a
    misconfigured string flag serves its default rather than null — inert, not broken.
    """
    if flag_type is FlagType.boolean:
        return not default_value
    return default_value


@router.post(
    "",
    response_model=FlagResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a feature flag",
    description=(
        "Creates the flag definition and **eagerly seeds a config row for all three "
        "environments**, each `enabled: false`.\n\n"
        "Eager seeding means a flag is never in a 'config missing' state, production is "
        "dark by default the instant the flag exists, and the evaluator has no null branch "
        "on its hot path.\n\n"
        "`default_value` must match `type` — jsonb would otherwise happily store the "
        '*string* `"true"` for a boolean flag and the evaluator would return a truthy '
        "string where the contract promises a bool."
    ),
    responses={409: {"description": "A flag with this key already exists for this tenant"}},
)
async def create_flag(
    payload: FlagCreate,
    tenant_id: uuid.UUID,
    api_key: ApiKey = Depends(require_tenant),
    db: AsyncSession = Depends(get_db),
) -> FlagResponse:
    flag = Flag(
        tenant_id=api_key.tenant_id,
        key=payload.key,
        name=payload.name,
        description=payload.description,
        type=payload.type,
        default_value=payload.default_value,
    )
    db.add(flag)
    try:
        await db.flush()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(
            status.HTTP_409_CONFLICT, f"Flag '{payload.key}' already exists"
        ) from None

    seeded = _seed_value(payload.type, payload.default_value)
    for environment in Environment:
        db.add(
            FlagEnvironmentConfig(
                flag_id=flag.id,
                environment=environment,
                tenant_id=api_key.tenant_id,
                enabled=False,
                value=seeded,
                rollout_percentage=100,
                targeting_rules=[],
            )
        )

    db.add(
        FlagAuditLog(
            tenant_id=api_key.tenant_id,
            flag_id=flag.id,
            environment=None,
            action=AuditAction.flag_created,
            old_value=None,
            new_value={
                "key": payload.key,
                "name": payload.name,
                "type": payload.type.value,
                "default_value": payload.default_value,
            },
            actor_key_id=api_key.id,
        )
    )
    await db.commit()
    await db.refresh(flag)
    await cache.invalidate(api_key.tenant_id)
    return FlagResponse.model_validate(flag)


@router.get(
    "",
    response_model=list[FlagResponse],
    summary="List flags",
    description=(
        "Filter by `environment` and by `status`.\n\n"
        "`environment` narrows the `configs` array on each flag to that environment only — "
        "the flag list itself is environment-independent, since a flag exists for the tenant "
        "rather than for one environment.\n\n"
        "`status` defaults to `active`; archived flags are excluded unless asked for."
    ),
)
async def list_flags(
    tenant_id: uuid.UUID,
    environment: Environment | None = Query(
        default=None, description="Narrow each flag's configs to this environment"
    ),
    status_filter: Literal["active", "archived", "all"] = Query(
        default="active", alias="status", description="Archived flags are excluded by default"
    ),
    api_key: ApiKey = Depends(require_tenant),
    db: AsyncSession = Depends(get_db),
) -> list[FlagResponse]:
    stmt = select(Flag).where(Flag.tenant_id == api_key.tenant_id)
    if status_filter == "active":
        stmt = stmt.where(Flag.archived_at.is_(None))
    elif status_filter == "archived":
        stmt = stmt.where(Flag.archived_at.is_not(None))

    flags = (await db.execute(stmt.order_by(Flag.key))).scalars().unique().all()

    responses = []
    for flag in flags:
        response = FlagResponse.model_validate(flag)
        if environment is not None:
            response.configs = [c for c in response.configs if c.environment == environment]
        responses.append(response)
    return responses


async def _get_flag_or_404(db: AsyncSession, tenant_id: uuid.UUID, flag_key: str) -> Flag:
    result = await db.execute(select(Flag).where(Flag.tenant_id == tenant_id, Flag.key == flag_key))
    flag = result.scalar_one_or_none()
    if flag is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Flag '{flag_key}' not found")
    return flag


@router.put(
    "/{flag_key}",
    response_model=FlagResponse,
    summary="Update a flag",
    description=(
        "Handles the spec's four operations: toggle on/off, change rollout percentage, "
        "update targeting rules, modify default value.\n\n"
        "Those span two scopes. `name`, `description` and `default_value` belong to the "
        "flag; `enabled`, `value`, `rollout_percentage` and `targeting_rules` belong to one "
        "environment. The URL carries no environment, so **`environment` is required in the "
        "body whenever an environment-scoped field is present**.\n\n"
        "Only the fields you send are changed. Each scope writes its own audit record."
    ),
    responses={
        404: {"description": "Flag not found"},
        422: {"description": "Type mismatch or missing environment"},
    },
)
async def update_flag(
    payload: FlagUpdate,
    tenant_id: uuid.UUID,
    flag_key: str,
    api_key: ApiKey = Depends(require_tenant),
    db: AsyncSession = Depends(get_db),
) -> FlagResponse:
    flag = await _get_flag_or_404(db, api_key.tenant_id, flag_key)
    fields_set = payload.model_fields_set

    # --- Flag-level changes ---------------------------------------------------
    flag_changes: dict[str, Any] = {}
    if "name" in fields_set and payload.name is not None:
        flag_changes["name"] = payload.name
    if "description" in fields_set:
        flag_changes["description"] = payload.description
    if "default_value" in fields_set:
        if not _value_matches_type(payload.default_value, flag.type):
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                f"default_value must be a {flag.type.value}",
            )
        flag_changes["default_value"] = payload.default_value

    flag_changes = _drop_unchanged(flag, flag_changes)
    if flag_changes:
        old = {k: getattr(flag, k) for k in flag_changes}
        for k, v in flag_changes.items():
            setattr(flag, k, v)
        db.add(
            FlagAuditLog(
                tenant_id=api_key.tenant_id,
                flag_id=flag.id,
                environment=None,
                action=AuditAction.flag_updated,
                old_value=old,
                new_value=flag_changes,
                actor_key_id=api_key.id,
            )
        )

    # --- Environment-level changes --------------------------------------------
    if payload.environment is not None:
        config = await db.get(FlagEnvironmentConfig, (flag.id, payload.environment))
        if config is None:
            raise HTTPException(
                status.HTTP_404_NOT_FOUND,
                f"No config for '{flag_key}' in {payload.environment.value}",
            )

        config_changes: dict[str, Any] = {}
        if "enabled" in fields_set and payload.enabled is not None:
            config_changes["enabled"] = payload.enabled
        if "rollout_percentage" in fields_set and payload.rollout_percentage is not None:
            config_changes["rollout_percentage"] = payload.rollout_percentage
        if "targeting_rules" in fields_set and payload.targeting_rules is not None:
            config_changes["targeting_rules"] = [r.model_dump() for r in payload.targeting_rules]
        if "value" in fields_set:
            if not _value_matches_type(payload.value, flag.type):
                raise HTTPException(
                    status.HTTP_422_UNPROCESSABLE_ENTITY, f"value must be a {flag.type.value}"
                )
            config_changes["value"] = payload.value

        config_changes = _drop_unchanged(config, config_changes)
        if config_changes:
            old = {k: getattr(config, k) for k in config_changes}
            for k, v in config_changes.items():
                setattr(config, k, v)
            db.add(
                FlagAuditLog(
                    tenant_id=api_key.tenant_id,
                    flag_id=flag.id,
                    environment=payload.environment,
                    action=AuditAction.config_updated,
                    old_value=old,
                    new_value=config_changes,
                    actor_key_id=api_key.id,
                )
            )

    await db.commit()
    await db.refresh(flag)
    # Flag-level edits change every environment's compiled set, so clear all three.
    await cache.invalidate(api_key.tenant_id, None if flag_changes else payload.environment)
    return FlagResponse.model_validate(flag)


@router.delete(
    "/{flag_key}",
    response_model=FlagResponse,
    summary="Archive (soft-delete) a flag",
    description=(
        "Sets `archived_at`. Rows are never removed.\n\n"
        "Hard deletion would orphan the audit trail, which the spec requires to be "
        "immutable and append-only — and an archived flag still needs its history readable "
        "for exactly the post-incident questions archiving tends to provoke.\n\n"
        "An archived flag evaluates to its `default_value` for every user, so archiving is "
        "a safe operation: it can only turn a feature off, never on."
    ),
    responses={404: {"description": "Flag not found"}},
)
async def archive_flag(
    tenant_id: uuid.UUID,
    flag_key: str,
    api_key: ApiKey = Depends(require_tenant),
    db: AsyncSession = Depends(get_db),
) -> FlagResponse:
    flag = await _get_flag_or_404(db, api_key.tenant_id, flag_key)
    if flag.archived_at is None:
        flag.archived_at = datetime.now(UTC)
        db.add(
            FlagAuditLog(
                tenant_id=api_key.tenant_id,
                flag_id=flag.id,
                environment=None,
                action=AuditAction.flag_archived,
                old_value={"archived_at": None},
                new_value={"archived_at": flag.archived_at.isoformat()},
                actor_key_id=api_key.id,
            )
        )
        await db.commit()
        await db.refresh(flag)
        await cache.invalidate(api_key.tenant_id)
    return FlagResponse.model_validate(flag)


@router.get(
    "/{flag_key}/history",
    response_model=list[AuditEntry],
    summary="View a flag's change history",
    description=(
        "Chronological, newest first. Append-only: rows here cannot be updated or deleted, "
        "enforced by a database trigger and by table grants rather than by application "
        "discipline.\n\n"
        "`actor_key_id` identifies the **API key** that made the change, not a person — the "
        "service has no notion of human identity. See the README on attribution."
    ),
    responses={404: {"description": "Flag not found"}},
)
async def flag_history(
    tenant_id: uuid.UUID,
    flag_key: str,
    limit: int = Query(default=50, ge=1, le=200),
    api_key: ApiKey = Depends(require_tenant),
    db: AsyncSession = Depends(get_db),
) -> list[AuditEntry]:
    flag = await _get_flag_or_404(db, api_key.tenant_id, flag_key)
    result = await db.execute(
        select(FlagAuditLog)
        .where(FlagAuditLog.tenant_id == api_key.tenant_id, FlagAuditLog.flag_id == flag.id)
        .order_by(FlagAuditLog.created_at.desc(), FlagAuditLog.id.desc())
        .limit(limit)
    )
    return [AuditEntry.model_validate(row) for row in result.scalars().all()]
