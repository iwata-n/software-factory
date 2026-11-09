"""Persist the desired number of parallel Codex workers per workspace."""

import sqlalchemy as sa
from alembic import op

revision = "0032"
down_revision = "0031"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "workspaces",
        sa.Column("codex_worker_count", sa.Integer(), nullable=False, server_default="10"),
    )
    op.create_check_constraint(
        "ck_codex_worker_count", "workspaces", "codex_worker_count BETWEEN 1 AND 32"
    )


def downgrade():
    op.drop_constraint("ck_codex_worker_count", "workspaces", type_="check")
    op.drop_column("workspaces", "codex_worker_count")
