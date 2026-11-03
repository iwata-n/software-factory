"""Store encrypted Workspace integration settings."""

import sqlalchemy as sa
from alembic import op

revision = "0024"
down_revision = "0023"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "integration_settings",
        sa.Column("workspace_id", sa.String(36), sa.ForeignKey("workspaces.id"), primary_key=True),
        sa.Column("provider", sa.String(20), primary_key=True),
        sa.Column("ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column("oauth_state_hash", sa.String(64), unique=True),
    )


def downgrade():
    op.drop_table("integration_settings")
