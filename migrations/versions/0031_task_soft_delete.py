"""Record task deletion without removing execution or audit history."""

import sqlalchemy as sa
from alembic import op

revision = "0031"
down_revision = "0030"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("tasks", sa.Column("deleted_at", sa.Float(), nullable=True))
    op.add_column("tasks", sa.Column("deleted_by", sa.String(36), nullable=True))
    op.create_foreign_key(
        "fk_tasks_deleted_by",
        "tasks",
        "principals",
        ["workspace_id", "deleted_by"],
        ["workspace_id", "id"],
    )
    op.create_check_constraint(
        "ck_tasks_deletion_identity",
        "tasks",
        "(deleted_at IS NULL) = (deleted_by IS NULL)",
    )


def downgrade():
    op.drop_constraint("ck_tasks_deletion_identity", "tasks", type_="check")
    op.drop_constraint("fk_tasks_deleted_by", "tasks", type_="foreignkey")
    op.drop_column("tasks", "deleted_by")
    op.drop_column("tasks", "deleted_at")
