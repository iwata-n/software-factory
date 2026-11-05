"""Persist specification interview turns alongside task chat."""

import sqlalchemy as sa
from alembic import op

revision = "0028"
down_revision = "0027"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("task_chat_turns", sa.Column("interview", sa.JSON()))


def downgrade():
    op.drop_column("task_chat_turns", "interview")
