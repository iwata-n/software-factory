"""Keep terminal Run timestamps independently of the retained event stream."""

import sqlalchemy as sa
from alembic import op

revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("runs", sa.Column("finished_at", sa.Float(), nullable=True))
    op.add_column("runs", sa.Column("evidence_expired_at", sa.Float(), nullable=True))
    op.add_column("runs", sa.Column("events_pruned_through", sa.Integer(), nullable=True))
    op.execute(
        """
        UPDATE runs SET finished_at = COALESCE(
            (SELECT max(run_events.created_at) FROM run_events
             WHERE run_events.workspace_id = runs.workspace_id AND run_events.run_id = runs.id),
            runs.created_at
        ) WHERE status IN ('SUCCEEDED', 'FAILED', 'CANCELLED')
        """
    )


def downgrade():
    op.drop_column("runs", "events_pruned_through")
    op.drop_column("runs", "evidence_expired_at")
    op.drop_column("runs", "finished_at")
