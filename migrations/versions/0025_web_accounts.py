"""Add browser accounts and revocable sessions."""

import sqlalchemy as sa
from alembic import op

revision = "0025"
down_revision = "0024"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "web_accounts",
        sa.Column("principal_id", sa.String(36), sa.ForeignKey("principals.id"), primary_key=True),
        sa.Column("username", sa.String(64), nullable=False, unique=True),
        sa.Column("password_salt", sa.LargeBinary(), nullable=False),
        sa.Column("password_hash", sa.LargeBinary(), nullable=False),
        sa.Column("login_failures", sa.Integer(), nullable=False),
        sa.Column("login_after", sa.Float(), nullable=False),
    )
    op.create_table(
        "web_sessions",
        sa.Column("token_hash", sa.String(64), primary_key=True),
        sa.Column(
            "principal_id",
            sa.String(36),
            sa.ForeignKey("web_accounts.principal_id"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.Float(), nullable=False),
    )
    op.create_index("ix_web_sessions_principal_id", "web_sessions", ["principal_id"])


def downgrade():
    op.drop_table("web_sessions")
    op.drop_table("web_accounts")
