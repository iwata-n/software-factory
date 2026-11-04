"""Associate Codex answer proposals with the plan that asked the questions."""

import sqlalchemy as sa
from alembic import op

revision = "0027"
down_revision = "0026"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("task_chat_turns", sa.Column("plan_id", sa.String(36)))
    op.create_foreign_key(
        "fk_task_chat_plan",
        "task_chat_turns",
        "plan_versions",
        ["workspace_id", "run_id", "plan_id"],
        ["workspace_id", "run_id", "id"],
        ondelete="CASCADE",
    )


def downgrade():
    op.drop_constraint("fk_task_chat_plan", "task_chat_turns", type_="foreignkey")
    op.drop_column("task_chat_turns", "plan_id")
