"""Retain replay receipts independently of purged Run resources."""

import sqlalchemy as sa
from alembic import op

revision = "0020"
down_revision = "0019"
branch_labels = None
depends_on = None


def publication_identity_guard(retention):
    op.execute(f"""
        CREATE OR REPLACE FUNCTION factory_publication_identity_immutable() RETURNS trigger AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                {retention}
                RAISE EXCEPTION 'PUBLICATION_IDENTITY_IMMUTABLE' USING ERRCODE='23514';
            END IF;
            IF ROW(NEW.id, NEW.workspace_id, NEW.repository_id, NEW.run_id, NEW.candidate_id,
                   NEW.job_id, NEW.gate_id, NEW.kind, NEW.operation_key, NEW.target::jsonb,
                   NEW.target_hash, NEW.created_at)
                IS DISTINCT FROM
               ROW(OLD.id, OLD.workspace_id, OLD.repository_id, OLD.run_id, OLD.candidate_id,
                   OLD.job_id, OLD.gate_id, OLD.kind, OLD.operation_key, OLD.target::jsonb,
                   OLD.target_hash, OLD.created_at) THEN
                RAISE EXCEPTION 'PUBLICATION_IDENTITY_IMMUTABLE' USING ERRCODE='23514';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
    """)


def upgrade():
    op.add_column("tasks", sa.Column("retention_touched_at", sa.Float(), nullable=True))
    op.execute("UPDATE tasks SET retention_touched_at = extract(epoch FROM clock_timestamp())")
    op.add_column(
        "api_idempotency_records", sa.Column("parent_deleted_at", sa.Float(), nullable=True)
    )
    op.add_column("api_idempotency_records", sa.Column("retired_lease_id", sa.String(36)))
    publication_identity_guard(
        "IF current_setting('factory.retention', true) = 'on' THEN RETURN OLD; END IF;"
    )


def downgrade():
    op.drop_column("tasks", "retention_touched_at")
    publication_identity_guard("")
    op.drop_column("api_idempotency_records", "retired_lease_id")
    op.drop_column("api_idempotency_records", "parent_deleted_at")
