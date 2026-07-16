"""Flag evaluation — the hot path.

Shape of a request: authenticate, load the compiled flag set for (tenant, environment)
from Redis, run the pure engine in-process, return. On a cache hit there is no database
call at all, which is what lets this scale horizontally — any instance can serve any
request because evaluation is a pure function of (request, flag set).
"""

import logging
import time
import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import cache
from app.db import get_db
from app.evaluation import FlagSnapshot, evaluate_all
from app.metrics import record_evaluation
from app.models import ApiKey, Environment, Flag, FlagEnvironmentConfig
from app.schemas import BulkEvaluateRequest, EvaluateRequest, EvaluateResponse
from app.security import authenticate

router = APIRouter(prefix="/api/v1/evaluate", tags=["evaluation"])
logger = logging.getLogger(__name__)


async def _load_flag_set(
    db: AsyncSession, tenant_id: uuid.UUID, environment: Environment
) -> list[FlagSnapshot]:
    """Fetch the compiled flag set, from cache or Postgres.

    The Postgres path is a single join returning every active flag with its config for
    this environment — no N+1, no per-flag queries. That result is exactly the cache
    payload, so a miss costs one query regardless of how many flags the tenant has.
    """
    cached = await cache.get_flag_set(tenant_id, environment)
    if cached is not None:
        return cached

    result = await db.execute(
        select(Flag, FlagEnvironmentConfig)
        .join(FlagEnvironmentConfig, FlagEnvironmentConfig.flag_id == Flag.id)
        .where(
            Flag.tenant_id == tenant_id,
            FlagEnvironmentConfig.environment == environment,
            Flag.archived_at.is_(None),
        )
    )
    snapshots = [
        FlagSnapshot(
            key=flag.key,
            type=flag.type.value,
            default_value=flag.default_value,
            enabled=config.enabled,
            value=config.value,
            rollout_percentage=config.rollout_percentage,
            targeting_rules=config.targeting_rules,
            archived=False,
        )
        for flag, config in result.all()
    ]
    await cache.set_flag_set(tenant_id, environment, snapshots)
    return snapshots


def _assert_tenant_matches(body_tenant_id: uuid.UUID, api_key: ApiKey) -> None:
    """The API key is the authority on tenancy — never the request body.

    The spec puts `tenant_id` in the evaluation payload, so we accept it, but a payload
    is caller-controlled input. Without this check any tenant could read any other
    tenant's flag values by changing one field in the JSON.
    """
    if body_tenant_id != api_key.tenant_id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "tenant_id does not match the API key")


@router.post(
    "",
    response_model=EvaluateResponse,
    summary="Evaluate flags for a user context",
    description=(
        "Evaluates flags for one user in one environment.\n\n"
        "Pass `flag_keys` to scope the response to specific flags; omit it to evaluate "
        "every active flag, which makes this endpoint equivalent to `/evaluate/bulk`. The "
        "spec describes both endpoints with the same request body and does not say how they "
        "differ — `flag_keys` is the assumption that distinguishes them.\n\n"
        "`user_id` is **never stored**. It is an argument to the rollout hash and nothing "
        "else; the same `user_id` always resolves to the same value for a given flag.\n\n"
        "`tenant_id` in the body must match the authenticating key or the request is 403."
    ),
    responses={403: {"description": "tenant_id does not match the API key"}},
)
async def evaluate_flags(
    payload: EvaluateRequest,
    api_key: ApiKey = Depends(authenticate),
    db: AsyncSession = Depends(get_db),
) -> EvaluateResponse:
    _assert_tenant_matches(payload.tenant_id, api_key)
    started = time.perf_counter()

    snapshots = await _load_flag_set(db, api_key.tenant_id, payload.environment)
    if payload.flag_keys is not None:
        wanted = set(payload.flag_keys)
        snapshots = [s for s in snapshots if s.key in wanted]

    results = evaluate_all(snapshots, payload.user_id, payload.context)
    elapsed_ms = (time.perf_counter() - started) * 1000
    record_evaluation(str(api_key.tenant_id), "/api/v1/evaluate", elapsed_ms, len(results))

    logger.info(
        "flags_evaluated",
        extra={
            "tenant_id": str(api_key.tenant_id),
            "environment": payload.environment.value,
            "flag_count": len(results),
            "duration_ms": round(elapsed_ms, 3),
            # Reasons, not user identities: enough to answer "why did this flag serve
            # that value" without persisting the caller's end users.
            "reasons": {r.key: r.reason.value for r in results},
        },
    )

    return EvaluateResponse(
        user_id=payload.user_id,
        environment=payload.environment,
        flags={r.key: r.value for r in results},
    )


@router.post(
    "/bulk",
    response_model=EvaluateResponse,
    summary="Bulk evaluate all active flags for a user context",
    description=(
        "Evaluates every active flag for one user in a single request.\n\n"
        "This is the endpoint an SDK calls on session start: one round trip, one cache "
        "read, and the whole flag set resolved in-process. Archived flags are omitted "
        "entirely rather than returned as their defaults."
    ),
    responses={403: {"description": "tenant_id does not match the API key"}},
)
async def evaluate_bulk(
    payload: BulkEvaluateRequest,
    api_key: ApiKey = Depends(authenticate),
    db: AsyncSession = Depends(get_db),
) -> EvaluateResponse:
    _assert_tenant_matches(payload.tenant_id, api_key)
    started = time.perf_counter()

    snapshots = await _load_flag_set(db, api_key.tenant_id, payload.environment)
    results = evaluate_all(snapshots, payload.user_id, payload.context)
    elapsed_ms = (time.perf_counter() - started) * 1000
    record_evaluation(str(api_key.tenant_id), "/api/v1/evaluate/bulk", elapsed_ms, len(results))

    logger.info(
        "flags_evaluated_bulk",
        extra={
            "tenant_id": str(api_key.tenant_id),
            "environment": payload.environment.value,
            "flag_count": len(results),
            "duration_ms": round(elapsed_ms, 3),
        },
    )

    return EvaluateResponse(
        user_id=payload.user_id,
        environment=payload.environment,
        flags={r.key: r.value for r in results},
    )
