"""SQLAlchemy models — the five tables from the agreed ERD.

Design notes that are easy to lose:

* `tenant_id` is denormalised onto `flags`, `flag_environment_configs` and
  `flag_audit_log` even though it is reachable via `flag_id`. Every hot query
  filters on it directly and every composite index leads with it, so no query can
  reach another tenant's rows without an explicit join through `tenants`.
* `flag_environment_configs` has no surrogate key: `(flag_id, environment)` is the
  natural key and nothing references the table.
* `flag_audit_log` is append-only. That is enforced by a trigger and by table
  grants in the migration, not by application discipline.
"""

import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class Environment(enum.StrEnum):
    development = "development"
    staging = "staging"
    production = "production"


class FlagType(enum.StrEnum):
    boolean = "boolean"
    string = "string"
    number = "number"


class AuditAction(enum.StrEnum):
    flag_created = "flag.created"
    flag_updated = "flag.updated"
    flag_archived = "flag.archived"
    config_updated = "config.updated"


# Reuse a single PG enum type per Python enum; `create_type=False` on all but the
# first would be needed if we let SQLAlchemy emit DDL, but Alembic owns the DDL.
_environment_enum = Enum(
    Environment, name="environment", values_callable=lambda e: [m.value for m in e]
)
_flag_type_enum = Enum(FlagType, name="flag_type", values_callable=lambda e: [m.value for m in e])
_audit_action_enum = Enum(
    AuditAction, name="audit_action", values_callable=lambda e: [m.value for m in e]
)


def _uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)


class Tenant(Base):
    """One row per application. "Register a new tenant (application)"."""

    __tablename__ = "tenants"

    id: Mapped[uuid.UUID] = _uuid_pk()
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    api_keys: Mapped[list["ApiKey"]] = relationship(
        back_populates="tenant", cascade="all, delete-orphan"
    )
    flags: Mapped[list["Flag"]] = relationship(
        back_populates="tenant", cascade="all, delete-orphan"
    )


class ApiKey(Base):
    """Tenant credentials. "API key authentication per tenant ... keys stored hashed".

    `key_hash` is SHA-256 rather than bcrypt/argon2 on purpose: keys are 256-bit
    random values, not user-chosen passwords, so a slow hash buys nothing against
    brute force but makes authentication O(n) — you cannot look a bcrypt hash up by
    index, you would have to scan every row and compare. SHA-256 is deterministic,
    so auth is a single indexed lookup.

    `name` exists solely so the audit trail's "who changed it" is not tautological;
    with one key per tenant the actor would always equal the tenant, which is
    already a column on the audit row.
    """

    __tablename__ = "api_keys"

    id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    key_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    tenant: Mapped["Tenant"] = relationship(back_populates="api_keys")

    __table_args__ = (Index("ix_api_keys_tenant_id", "tenant_id"),)


class Flag(Base):
    """The flag definition — what the flag *is*, independent of environment.

    `default_value` is the value served when the flag is off, archived, or the user
    falls outside the rollout. It is the only "off" value in the system.
    """

    __tablename__ = "flags"

    id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    key: Mapped[str] = mapped_column(String(255), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    type: Mapped[FlagType] = mapped_column(_flag_type_enum, nullable=False)
    default_value: Mapped[dict] = mapped_column(JSONB, nullable=False)
    # Soft delete. "DELETE ... Soft-delete (archive) a flag" + the active/archived
    # filter on the list endpoint.
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    tenant: Mapped["Tenant"] = relationship(back_populates="flags")
    configs: Mapped[list["FlagEnvironmentConfig"]] = relationship(
        back_populates="flag", cascade="all, delete-orphan", lazy="selectin"
    )

    __table_args__ = (
        UniqueConstraint("tenant_id", "key", name="uq_flags_tenant_key"),
        Index("ix_flags_tenant_archived", "tenant_id", "archived_at"),
    )


class FlagEnvironmentConfig(Base):
    """Per-environment state — what the flag is *doing* in dev/staging/production.

    One row per (flag, environment), created eagerly for all three environments when
    the flag is created so that production is never in a "config missing" state and
    the evaluator has no null branch to handle.
    """

    __tablename__ = "flag_environment_configs"

    flag_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("flags.id", ondelete="CASCADE"), primary_key=True
    )
    environment: Mapped[Environment] = mapped_column(_environment_enum, primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )

    # "toggle on/off"
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # Served when enabled AND inside the rollout AND no targeting rule matched.
    # Required for the `string`/`number` types to mean anything: a flag carrying only
    # `default_value` returns that value to everyone and the rollout has nothing to
    # select between.
    value: Mapped[dict] = mapped_column(JSONB, nullable=False)
    # "change rollout percentage"
    rollout_percentage: Mapped[int] = mapped_column(Integer, nullable=False, default=100)
    # "update targeting rules"
    targeting_rules: Mapped[list] = mapped_column(JSONB, nullable=False, server_default="[]")

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    flag: Mapped["Flag"] = relationship(back_populates="configs")

    __table_args__ = (
        CheckConstraint(
            "rollout_percentage >= 0 AND rollout_percentage <= 100",
            name="ck_config_rollout_range",
        ),
        # Guard rails on the jsonb blob. Rules live in jsonb because they are always
        # read as a whole with the config row and never queried independently — but
        # jsonb accepts anything, so bound the size to keep the hot path off TOAST.
        CheckConstraint("jsonb_typeof(targeting_rules) = 'array'", name="ck_config_rules_is_array"),
        CheckConstraint("jsonb_array_length(targeting_rules) <= 50", name="ck_config_rules_max"),
        CheckConstraint("pg_column_size(targeting_rules) < 8192", name="ck_config_rules_size"),
        Index("ix_configs_tenant_env", "tenant_id", "environment"),
    )


class FlagAuditLog(Base):
    """Append-only change history. "Audit records are immutable (append-only)".

    Immutability is enforced in the migration by a BEFORE UPDATE OR DELETE trigger
    that raises, plus grants that give the application role INSERT and SELECT only.
    Application-level discipline is not immutability; the database has to say no.
    """

    __tablename__ = "flag_audit_log"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    # RESTRICT, not CASCADE: a cascading delete is still a DELETE, and the immutability
    # trigger rejects it. Cascading would also be a backdoor through immutability —
    # erase the audit trail by deleting its tenant. See the migration for the full note.
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="RESTRICT"), nullable=False
    )
    flag_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("flags.id", ondelete="RESTRICT"), nullable=False
    )
    # NULL for flag-definition-level changes (create, rename, archive) which are not
    # scoped to one environment.
    environment: Mapped[Environment | None] = mapped_column(_environment_enum, nullable=True)
    action: Mapped[AuditAction] = mapped_column(_audit_action_enum, nullable=False)
    # "what changed" and "the previous value"
    old_value: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    new_value: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    # "who changed it" — actors are keys, not humans. Real human attribution needs an
    # identity layer the spec does not ask for.
    actor_key_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("api_keys.id", ondelete="SET NULL"), nullable=True
    )
    # "when"
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        # Serves GET /tenants/{id}/flags/{flag_key}/history — chronological, newest first.
        Index("ix_audit_tenant_flag_created", "tenant_id", "flag_id", "created_at"),
    )
