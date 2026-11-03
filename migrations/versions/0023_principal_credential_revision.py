"""Track credential changes for auditable revocation."""

import sqlalchemy as sa
from alembic import op

revision = "0023"
down_revision = "0022"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "principals",
        sa.Column("credential_revision", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade():
    op.drop_column("principals", "credential_revision")
