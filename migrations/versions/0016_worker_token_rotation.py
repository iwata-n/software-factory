"""Expiring Worker credentials with a single predecessor grace window."""

import sqlalchemy as sa
from alembic import op

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("principals", sa.Column("token_expires_at", sa.Float(), nullable=True))
    op.add_column("principals", sa.Column("previous_token_hash", sa.String(64), nullable=True))
    op.add_column("principals", sa.Column("previous_token_expires_at", sa.Float(), nullable=True))
    op.create_unique_constraint(
        "uq_principals_previous_token", "principals", ["previous_token_hash"]
    )
    op.execute(
        "CREATE TRIGGER audit_event_immutable BEFORE UPDATE OR DELETE ON audit_events "
        "FOR EACH ROW EXECUTE FUNCTION factory_evidence_immutable()"
    )
    op.execute(
        "UPDATE principals SET token_expires_at = extract(epoch FROM clock_timestamp()) + 86400 "
        "WHERE role = 'WORKER'"
    )


def downgrade():
    op.execute("DROP TRIGGER audit_event_immutable ON audit_events")
    op.drop_constraint("uq_principals_previous_token", "principals", type_="unique")
    op.drop_column("principals", "previous_token_expires_at")
    op.drop_column("principals", "previous_token_hash")
    op.drop_column("principals", "token_expires_at")
