"""Persist Workspace execution control and its revision."""

import sqlalchemy as sa
from alembic import op

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "workspaces",
        sa.Column("execution_stopped", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "workspaces",
        sa.Column("execution_stop_revision", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade():
    op.drop_column("workspaces", "execution_stop_revision")
    op.drop_column("workspaces", "execution_stopped")
