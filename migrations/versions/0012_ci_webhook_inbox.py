"""Persist authenticated GitHub delivery signals."""

import sqlalchemy as sa
from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "ci_webhook_deliveries",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("workspace_id", sa.String(36), sa.ForeignKey("workspaces.id"), nullable=False),
        sa.Column("repository_id", sa.String(36), nullable=False),
        sa.Column("integration_id", sa.String(36), nullable=False),
        sa.Column("delivery_id", sa.String(36), nullable=False),
        sa.Column("event", sa.String(20), nullable=False),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("received_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(
            ["workspace_id", "repository_id"], ["repositories.workspace_id", "repositories.id"]
        ),
        sa.UniqueConstraint("workspace_id", "repository_id", "integration_id", "delivery_id"),
    )
    op.create_index(
        "ix_ci_webhook_deliveries_workspace_id", "ci_webhook_deliveries", ["workspace_id"]
    )


def downgrade():
    op.drop_table("ci_webhook_deliveries")
