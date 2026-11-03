"""Add the Jira revision and Slack delivery ledgers."""

import sqlalchemy as sa
from alembic import op

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "jira_issue_revisions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("workspace_id", sa.String(36), sa.ForeignKey("workspaces.id"), nullable=False),
        sa.Column("integration_id", sa.String(36), nullable=False),
        sa.Column("issue_id", sa.String(32), nullable=False),
        sa.Column("source_version", sa.BigInteger(), nullable=False),
        sa.Column("issue_url", sa.String(500), nullable=False),
        sa.Column("fingerprint", sa.String(64)),
        sa.Column("task_id", sa.String(36)),
        sa.Column("spec_version", sa.Integer()),
        sa.Column("state", sa.String(20), nullable=False),
        sa.UniqueConstraint("workspace_id", "integration_id", "issue_id", "source_version"),
        sa.ForeignKeyConstraint(["workspace_id", "task_id"], ["tasks.workspace_id", "tasks.id"]),
    )
    op.create_index(
        "ix_jira_issue_revisions_workspace_id", "jira_issue_revisions", ["workspace_id"]
    )
    op.create_table(
        "slack_deliveries",
        sa.Column("outbox_id", sa.String(36), sa.ForeignKey("outbox_events.id"), primary_key=True),
        sa.Column("channel", sa.String(32), nullable=False),
        sa.Column("state", sa.String(20), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("due_at", sa.Float(), nullable=False),
        sa.Column("claim_id", sa.String(36), nullable=False),
        sa.Column("message_id", sa.String(32)),
        sa.Column("reason", sa.String(40)),
    )


def downgrade():
    op.drop_table("slack_deliveries")
    op.drop_table("jira_issue_revisions")
