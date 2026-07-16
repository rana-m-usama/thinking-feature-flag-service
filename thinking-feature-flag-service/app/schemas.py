"""Pydantic request/response models.

These are also the OpenAPI contract — the examples below are what renders at /docs,
so they are written to be copy-pasteable rather than illustrative.
"""

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.models import AuditAction, Environment, FlagType

# --- Targeting -----------------------------------------------------------------

Operator = Literal["in", "not_in", "eq", "neq", "contains", "starts_with", "ends_with"]


class TargetingRule(BaseModel):
    """A single targeting rule.

    Rules are evaluated in array order, first match wins, and a match short-circuits
    the percentage rollout entirely. The operator set is deliberately small; the spec
    says "targeting rules" without enumerating operators, so this covers the common
    cases and the README documents the omissions.
    """

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "attribute": "plan",
                "operator": "in",
                "values": ["enterprise"],
                "value": True,
            }
        }
    )

    attribute: str = Field(
        description="Context key to match on. The literal `user_id` resolves to the "
        "request's user_id rather than a context entry."
    )
    operator: Operator
    values: list[Any] = Field(description="Operands. `eq`/`neq` use the first element.")
    value: Any = Field(description="Value served when this rule matches.")


# --- Tenants -------------------------------------------------------------------


class TenantCreate(BaseModel):
    model_config = ConfigDict(json_schema_extra={"example": {"name": "bizscout-web"}})
    name: str = Field(min_length=1, max_length=255)


class TenantCreated(BaseModel):
    """Returned once, at registration.

    `api_key` appears in this response and nowhere else, ever — only its SHA-256 hash
    is persisted, so it cannot be recovered afterwards.
    """

    id: uuid.UUID
    name: str
    created_at: datetime
    api_key: str = Field(description="Shown once. Not recoverable — store it now.")


class TenantResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    name: str
    created_at: datetime


# --- Flags ---------------------------------------------------------------------


class FlagCreate(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "key": "checkout.new_flow",
                "name": "New checkout flow",
                "description": "Rebuilt checkout with the single-page payment step",
                "type": "boolean",
                "default_value": False,
            }
        }
    )

    key: str = Field(
        min_length=1,
        max_length=255,
        pattern=r"^[a-zA-Z0-9._-]+$",
        description="Stable identifier, unique per tenant. Used in URLs and mixed into "
        "the rollout hash, so renaming a flag reshuffles its cohort — treat as immutable.",
    )
    name: str = Field(min_length=1, max_length=255)
    description: str | None = None
    type: FlagType
    default_value: Any = Field(
        description="Served when the flag is off, archived, or the user is outside the "
        "rollout. The only 'off' value in the system."
    )

    @model_validator(mode="after")
    def check_default_matches_type(self) -> "FlagCreate":
        if not _value_matches_type(self.default_value, self.type):
            raise ValueError(f"default_value must be a {self.type.value}")
        return self


class FlagUpdate(BaseModel):
    """Covers the spec's PUT: "toggle on/off, change rollout percentage, update
    targeting rules, modify default value".

    That list mixes two scopes. `name`, `description` and `default_value` belong to the
    flag itself; `enabled`, `value`, `rollout_percentage` and `targeting_rules` belong
    to one environment's config. The spec's URL carries no environment, so `environment`
    is required in the body whenever an environment-scoped field is present.
    """

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "environment": "production",
                "enabled": True,
                "value": True,
                "rollout_percentage": 25,
                "targeting_rules": [
                    {"attribute": "plan", "operator": "in", "values": ["enterprise"], "value": True}
                ],
            }
        }
    )

    # Flag-level
    name: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = None
    default_value: Any = None

    # Environment-level
    environment: Environment | None = None
    enabled: bool | None = None
    value: Any = None
    rollout_percentage: int | None = Field(default=None, ge=0, le=100)
    targeting_rules: list[TargetingRule] | None = None

    _ENV_SCOPED = ("enabled", "value", "rollout_percentage", "targeting_rules")

    @model_validator(mode="after")
    def check_environment_present(self) -> "FlagUpdate":
        fields_set = self.model_fields_set
        touches_env = any(f in fields_set for f in self._ENV_SCOPED)
        if touches_env and self.environment is None:
            raise ValueError(
                "environment is required when updating enabled, value, "
                "rollout_percentage or targeting_rules"
            )
        return self


class FlagConfigResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    environment: Environment
    enabled: bool
    value: Any
    rollout_percentage: int
    targeting_rules: list[dict]
    updated_at: datetime


class FlagResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    key: str
    name: str
    description: str | None
    type: FlagType
    default_value: Any
    archived_at: datetime | None
    created_at: datetime
    updated_at: datetime
    configs: list[FlagConfigResponse]


# --- Audit ---------------------------------------------------------------------


class AuditEntry(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    action: AuditAction
    environment: Environment | None
    old_value: dict | None
    new_value: dict | None
    actor_key_id: uuid.UUID | None
    created_at: datetime


# --- Evaluation ----------------------------------------------------------------


class EvaluateRequest(BaseModel):
    """The spec's body verbatim, plus an optional `flag_keys` scope.

    `tenant_id` is in the body because the spec puts it there, but it is *not* trusted:
    it is checked against the authenticated key and a mismatch is a 403. The key is the
    authority on tenancy, never the payload.
    """

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "tenant_id": "3f1a...",
                "environment": "production",
                "user_id": "user_2c91",
                "context": {"plan": "free", "country": "DE"},
            }
        }
    )

    tenant_id: uuid.UUID
    environment: Environment
    user_id: str = Field(
        min_length=1,
        max_length=255,
        description="Opaque identifier from the caller's system. Never stored — it is an "
        "argument to the rollout hash and nothing else.",
    )
    context: dict[str, Any] = Field(
        default_factory=dict, description="Attributes targeting rules match against."
    )
    flag_keys: list[str] | None = Field(
        default=None, description="Restrict to these flags. Omit to evaluate all active flags."
    )


class BulkEvaluateRequest(BaseModel):
    """ "Bulk evaluate all active flags for a user context in a single request"."""

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "tenant_id": "3f1a...",
                "environment": "production",
                "user_id": "user_2c91",
                "context": {"plan": "enterprise"},
            }
        }
    )

    tenant_id: uuid.UUID
    environment: Environment
    user_id: str = Field(min_length=1, max_length=255)
    context: dict[str, Any] = Field(default_factory=dict)


class EvaluateResponse(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "user_id": "user_2c91",
                "environment": "production",
                "flags": {"checkout.new_flow": False, "search.new_ranking": "variant_b"},
            }
        }
    )

    user_id: str
    environment: Environment
    flags: dict[str, Any] = Field(description="flag_key -> evaluated value")


# --- Shared --------------------------------------------------------------------


def _value_matches_type(value: Any, flag_type: FlagType) -> bool:
    """Guard the declared type against the stored value.

    `flags.type` and the jsonb value columns live in two places with no constraint
    between them — jsonb will happily store the string "true" for a boolean flag, and
    the evaluator would then return a truthy string where the contract promises a bool.
    Postgres cannot express this check across tables without denormalising `type` onto
    the config row, so it is enforced here and at every write path.
    """
    match flag_type:
        case FlagType.boolean:
            return isinstance(value, bool)
        case FlagType.number:
            return isinstance(value, (int, float)) and not isinstance(value, bool)
        case FlagType.string:
            return isinstance(value, str)
    return False
