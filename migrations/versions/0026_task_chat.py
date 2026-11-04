"""Persist task conversations and their bounded worker claims."""

import sqlalchemy as sa
from alembic import op

revision = "0026"
down_revision = "0025"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "task_chat_turns",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("workspace_id", sa.String(36), sa.ForeignKey("workspaces.id"), nullable=False),
        sa.Column("run_id", sa.String(36), nullable=False),
        sa.Column("worker_id", sa.String(36), nullable=True),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("answer", sa.Text(), nullable=True),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("error", sa.String(100), nullable=True),
        sa.Column("usage", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("deadline_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(
            ["workspace_id", "run_id"], ["runs.workspace_id", "runs.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "worker_id"], ["workers.workspace_id", "workers.id"]
        ),
        sa.CheckConstraint("status IN ('QUEUED','RUNNING','ANSWERED','FAILED')"),
    )
    op.create_index(
        "ix_task_chat_turns_workspace_id", "task_chat_turns", ["workspace_id", "created_at"]
    )
    op.create_index("ix_task_chat_turns_run_id", "task_chat_turns", ["run_id", "created_at"])


def downgrade():
    op.drop_table("task_chat_turns")
