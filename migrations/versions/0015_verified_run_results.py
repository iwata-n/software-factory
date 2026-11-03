"""Immutable verified Run results."""

import sqlalchemy as sa
from alembic import op

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "verified_run_results",
        sa.Column("run_id", sa.String(36), nullable=False),
        sa.Column("body", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("workspace_id", sa.String(36), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"]),
        sa.ForeignKeyConstraint(["workspace_id", "run_id"], ["runs.workspace_id", "runs.id"]),
        sa.UniqueConstraint("workspace_id", "run_id"),
    )
    op.create_index(
        op.f("ix_verified_run_results_workspace_id"), "verified_run_results", ["workspace_id"]
    )
    op.execute(
        "CREATE TRIGGER verified_run_result_immutable BEFORE UPDATE OR DELETE ON "
        "verified_run_results FOR EACH ROW EXECUTE FUNCTION factory_evidence_immutable()"
    )


def downgrade():
    op.drop_table("verified_run_results")
