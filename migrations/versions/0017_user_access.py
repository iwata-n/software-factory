"""Separate Workspace administration, operation, and read-only access."""

import sqlalchemy as sa
from alembic import op

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "principals",
        sa.Column("user_access", sa.String(20), nullable=False, server_default="ADMIN"),
    )
    op.create_check_constraint(
        "ck_principals_user_access", "principals", "user_access IN ('ADMIN', 'OPERATOR', 'VIEWER')"
    )


def downgrade():
    op.drop_constraint("ck_principals_user_access", "principals", type_="check")
    op.drop_column("principals", "user_access")
