"""Initial schema: the five tables from the agreed ERD.

Revision ID: 0001
Revises:
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # gen_random_uuid() lives here. Available in core Postgres since 13, so no
    # uuid-ossp dependency — which matters because Cloud SQL restricts extensions.
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")

    environment = postgresql.ENUM(
        "development", "staging", "production", name="environment", create_type=False
    )
    flag_type = postgresql.ENUM("boolean", "string", "number", name="flag_type", create_type=False)
    audit_action = postgresql.ENUM(
        "flag.created", "flag.updated", "flag.archived", "config.updated",
        name="audit_action", create_type=False,
    )
    for enum in (environment, flag_type, audit_action):
        enum.create(op.get_bind(), checkfirst=True)

    # --- tenants --------------------------------------------------------------
    op.create_table(
        "tenants",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )

    # --- api_keys -------------------------------------------------------------
    op.create_table(
        "api_keys",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        # 64 chars = SHA-256 hex. Unique so authentication is one indexed lookup.
        sa.Column("key_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_api_keys_tenant_id", "api_keys", ["tenant_id"])

    # --- flags ----------------------------------------------------------------
    op.create_table(
        "flags",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("key", sa.String(255), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("type", flag_type, nullable=False),
        sa.Column("default_value", postgresql.JSONB(), nullable=False),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("tenant_id", "key", name="uq_flags_tenant_key"),
    )
    # Leads with tenant_id: every query is tenant-scoped, so the index must be too.
    op.create_index("ix_flags_tenant_archived", "flags", ["tenant_id", "archived_at"])

    # --- flag_environment_configs ---------------------------------------------
    op.create_table(
        "flag_environment_configs",
        sa.Column("flag_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("flags.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("environment", environment, primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("value", postgresql.JSONB(), nullable=False),
        sa.Column("rollout_percentage", sa.Integer(), nullable=False, server_default="100"),
        sa.Column("targeting_rules", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("rollout_percentage >= 0 AND rollout_percentage <= 100",
                           name="ck_config_rollout_range"),
        # jsonb accepts anything, so bound what the hot path can be asked to read.
        # Past ~2KB Postgres TOASTs the value out of line and every evaluation pays an
        # extra fetch — which would quietly undo the reason rules are inline at all.
        sa.CheckConstraint("jsonb_typeof(targeting_rules) = 'array'", name="ck_config_rules_is_array"),
        sa.CheckConstraint("jsonb_array_length(targeting_rules) <= 50", name="ck_config_rules_max"),
        sa.CheckConstraint("pg_column_size(targeting_rules) < 8192", name="ck_config_rules_size"),
    )
    op.create_index("ix_configs_tenant_env", "flag_environment_configs", ["tenant_id", "environment"])

    # --- flag_audit_log -------------------------------------------------------
    # RESTRICT, not CASCADE, on both parents — and the distinction is load-bearing.
    #
    # The immutability trigger below rejects any DELETE on this table. A cascading
    # delete is still a DELETE, so `DELETE FROM tenants` would fire the trigger from
    # inside the cascade and abort with an error that names a plpgsql function rather
    # than the actual problem. Worse, if the cascade *did* succeed it would be a
    # backdoor straight through immutability: erase the audit trail by deleting its
    # tenant.
    #
    # RESTRICT makes the schema state the real rule: a tenant or flag with audit
    # history cannot be deleted, and the error says exactly that. Purging a tenant is
    # therefore a deliberate operation (drop trigger, delete, recreate) rather than
    # something a stray CASCADE does quietly. The spec has no tenant-deletion endpoint,
    # so nothing needs this today; GDPR erasure would be the reason to build it, and it
    # should be explicit when it arrives.
    op.create_table(
        "flag_audit_log",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("tenants.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("flag_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("flags.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("environment", environment, nullable=True),
        sa.Column("action", audit_action, nullable=False),
        sa.Column("old_value", postgresql.JSONB(), nullable=True),
        sa.Column("new_value", postgresql.JSONB(), nullable=True),
        sa.Column("actor_key_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("api_keys.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_audit_tenant_flag_created", "flag_audit_log",
                    ["tenant_id", "flag_id", "created_at"])

    # --- Audit immutability ---------------------------------------------------
    # "Audit records are immutable (append-only)."
    #
    # Enforced by the database, not by the application. An ORM that never issues an
    # UPDATE is a convention; a trigger that refuses one is a guarantee. This also
    # holds against psql, a migration, and a future maintainer who has not read the
    # spec — which is the entire point of an audit trail.
    #
    # Pair this with a least-privilege grant in production:
    #   REVOKE UPDATE, DELETE, TRUNCATE ON flag_audit_log FROM <app_role>;
    # The trigger stops the statement; the grant stops it being attempted. TRUNCATE in
    # particular fires no row-level trigger, so the grant is not redundant.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION reject_audit_mutation()
        RETURNS TRIGGER AS $$
        BEGIN
            RAISE EXCEPTION 'flag_audit_log is append-only: % is not permitted', TG_OP
                USING ERRCODE = 'insufficient_privilege';
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_flag_audit_log_immutable
        BEFORE UPDATE OR DELETE ON flag_audit_log
        FOR EACH ROW EXECUTE FUNCTION reject_audit_mutation();
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_flag_audit_log_immutable ON flag_audit_log")
    op.execute("DROP FUNCTION IF EXISTS reject_audit_mutation()")
    op.drop_table("flag_audit_log")
    op.drop_table("flag_environment_configs")
    op.drop_table("flags")
    op.drop_table("api_keys")
    op.drop_table("tenants")
    for name in ("audit_action", "flag_type", "environment"):
        op.execute(f"DROP TYPE IF EXISTS {name}")
