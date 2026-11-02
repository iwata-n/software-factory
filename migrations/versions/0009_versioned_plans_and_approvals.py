"""versioned plans and fixed approval targets

Revision ID: 0009
Revises: 0008
"""

import hashlib
import json

import sqlalchemy as sa
from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("plan_versions", sa.Column("body_hash", sa.String(64), nullable=True))
    connection = op.get_bind()
    plans = sa.table(
        "plan_versions",
        sa.column("id", sa.String),
        sa.column("body", sa.JSON),
        sa.column("body_hash", sa.String),
    )
    for row in connection.execute(sa.select(plans.c.id, plans.c.body)):
        encoded = json.dumps(row.body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        fingerprint = hashlib.sha256(encoded.encode()).hexdigest()
        connection.execute(plans.update().where(plans.c.id == row.id).values(body_hash=fingerprint))
    op.alter_column("plan_versions", "body_hash", nullable=False)
    op.add_column("plan_versions", sa.Column("producer_job_id", sa.String(36), nullable=True))
    op.create_unique_constraint(
        "uq_plans_producer_job", "plan_versions", ["workspace_id", "producer_job_id"]
    )
    op.create_unique_constraint(
        "uq_plans_workspace_run_id", "plan_versions", ["workspace_id", "run_id", "id"]
    )
    op.create_foreign_key(
        "fk_plan_producer_job",
        "plan_versions",
        "jobs",
        ["workspace_id", "run_id", "producer_job_id"],
        ["workspace_id", "run_id", "id"],
    )
    op.create_table(
        "approval_requests",
        sa.Column("run_id", sa.String(length=36), nullable=False),
        sa.Column("plan_id", sa.String(length=36), nullable=True),
        sa.Column("action", sa.String(length=40), nullable=False),
        sa.Column("target", sa.JSON(), nullable=False),
        sa.Column("target_hash", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("expires_at", sa.Float(), nullable=False),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("workspace_id", sa.String(length=36), nullable=False),
        sa.CheckConstraint("status IN ('PENDING','APPROVED','DENIED','EXPIRED','INVALIDATED')"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "run_id", "plan_id"],
            ["plan_versions.workspace_id", "plan_versions.run_id", "plan_versions.id"],
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "run_id"],
            ["runs.workspace_id", "runs.id"],
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("workspace_id", "id"),
        sa.UniqueConstraint("workspace_id", "run_id", "action", "target_hash"),
        sa.UniqueConstraint(
            "workspace_id", "run_id", "id", name="uq_approval_requests_workspace_run_id"
        ),
    )
    op.create_index(
        op.f("ix_approval_requests_workspace_id"),
        "approval_requests",
        ["workspace_id"],
        unique=False,
    )
    op.create_table(
        "approval_decisions",
        sa.Column("run_id", sa.String(length=36), nullable=False),
        sa.Column("approval_id", sa.String(length=36), nullable=False),
        sa.Column("actor_id", sa.String(length=36), nullable=False),
        sa.Column("decision", sa.String(length=20), nullable=False),
        sa.Column("reason", sa.String(length=4000), nullable=False),
        sa.Column("target_hash", sa.String(length=64), nullable=False),
        sa.Column("before_revision", sa.Integer(), nullable=False),
        sa.Column("after_revision", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("workspace_id", sa.String(length=36), nullable=False),
        sa.CheckConstraint("decision IN ('approve','deny')"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "actor_id"],
            ["principals.workspace_id", "principals.id"],
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "run_id", "approval_id"],
            ["approval_requests.workspace_id", "approval_requests.run_id", "approval_requests.id"],
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("workspace_id", "approval_id"),
    )
    op.create_index(
        op.f("ix_approval_decisions_workspace_id"),
        "approval_decisions",
        ["workspace_id"],
        unique=False,
    )
    for table in ("plan_versions", "approval_decisions"):
        op.execute(
            f"CREATE TRIGGER evidence_immutable BEFORE UPDATE OR DELETE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION factory_evidence_immutable()"
        )
    op.execute("""CREATE FUNCTION factory_approval_protected() RETURNS trigger AS $$
        BEGIN
            IF coalesce(current_setting('factory.retention', true), 'off') = 'on' THEN
                IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
                RETURN NEW;
            END IF;
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'APPROVAL_TARGET_IMMUTABLE'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            IF NEW.target::jsonb IS DISTINCT FROM OLD.target::jsonb
                OR NEW.id IS DISTINCT FROM OLD.id
                OR NEW.target_hash IS DISTINCT FROM OLD.target_hash
                OR NEW.workspace_id IS DISTINCT FROM OLD.workspace_id
                OR NEW.run_id IS DISTINCT FROM OLD.run_id OR NEW.action IS DISTINCT FROM OLD.action
                OR NEW.plan_id IS DISTINCT FROM OLD.plan_id
                OR NEW.expires_at IS DISTINCT FROM OLD.expires_at
                OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
                RAISE EXCEPTION 'APPROVAL_TARGET_IMMUTABLE'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            RETURN NEW;
        END; $$ LANGUAGE plpgsql""")
    op.execute(
        "CREATE TRIGGER approval_target_immutable BEFORE UPDATE OR DELETE ON approval_requests "
        "FOR EACH ROW EXECUTE FUNCTION factory_approval_protected()"
    )


def downgrade():
    op.drop_index("ix_approval_decisions_workspace_id", table_name="approval_decisions")
    op.drop_table("approval_decisions")
    op.drop_index("ix_approval_requests_workspace_id", table_name="approval_requests")
    op.drop_table("approval_requests")
    op.execute("DROP FUNCTION factory_approval_protected()")
    op.execute("DROP TRIGGER evidence_immutable ON plan_versions")
    op.drop_constraint("fk_plan_producer_job", "plan_versions", type_="foreignkey")
    op.drop_constraint("uq_plans_producer_job", "plan_versions", type_="unique")
    op.drop_constraint("uq_plans_workspace_run_id", "plan_versions", type_="unique")
    op.drop_column("plan_versions", "producer_job_id")
    op.drop_column("plan_versions", "body_hash")
