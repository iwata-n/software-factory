"""Remember which task specification has completed clarification."""

import sqlalchemy as sa
from alembic import op

revision = "0029"
down_revision = "0028"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("tasks", sa.Column("clarified_spec_version", sa.Integer()))
    op.execute("""
        UPDATE tasks t SET clarified_spec_version = t.latest_spec_version
        WHERE EXISTS (
            SELECT 1 FROM task_chat_turns c JOIN runs r ON r.id = c.run_id
            WHERE r.task_id = t.id AND c.workspace_id = t.workspace_id
              AND c.interview->>'adopted_version' = t.latest_spec_version::text
        )
    """)


def downgrade():
    op.drop_column("tasks", "clarified_spec_version")
