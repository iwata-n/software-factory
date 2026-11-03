"""Separate signed historical CI observations from gate evidence."""

import sqlalchemy as sa
from alembic import op

revision = "0022"
down_revision = "0021"
branch_labels = None
depends_on = None


def upgrade():
    op.create_unique_constraint(
        "uq_ci_webhook_workspace_id", "ci_webhook_deliveries", ["workspace_id", "id"]
    )
    op.create_table(
        "historical_ci_observations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("workspace_id", sa.String(36), sa.ForeignKey("workspaces.id"), nullable=False),
        sa.Column("run_id", sa.String(36), nullable=False),
        sa.Column("candidate_id", sa.String(36), nullable=False),
        sa.Column("inbox_id", sa.String(36), nullable=False),
        sa.Column("body", sa.JSON(), nullable=False),
        sa.Column("received_at", sa.Float(), nullable=False),
        sa.UniqueConstraint("workspace_id", "candidate_id", "inbox_id"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "inbox_id"],
            ["ci_webhook_deliveries.workspace_id", "ci_webhook_deliveries.id"],
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "run_id", "candidate_id"],
            ["candidates.workspace_id", "candidates.run_id", "candidates.id"],
        ),
    )
    op.create_index(
        "ix_historical_ci_observations_workspace_id", "historical_ci_observations", ["workspace_id"]
    )
    op.execute(
        "CREATE TRIGGER historical_ci_observation_immutable BEFORE UPDATE OR DELETE "
        "ON historical_ci_observations FOR EACH ROW EXECUTE FUNCTION factory_evidence_immutable()"
    )


def downgrade():
    op.drop_table("historical_ci_observations")
    op.drop_constraint("uq_ci_webhook_workspace_id", "ci_webhook_deliveries", type_="unique")
