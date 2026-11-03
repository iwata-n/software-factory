"""Persist read-only publication reconciliation across restarts."""

import sqlalchemy as sa
from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None

TABLE = "publication_operations"
NAME = "ck_publication_operations_state"
PREVIOUS = "state IN ('PENDING','IN_FLIGHT','UNKNOWN','SUCCEEDED','BLOCKED','CANCELLED')"


def upgrade():
    for constraint in sa.inspect(op.get_bind()).get_check_constraints(TABLE):
        if "state" in constraint["sqltext"]:
            op.drop_constraint(constraint["name"], TABLE, type_="check")
            break
    op.create_check_constraint(
        NAME, TABLE, PREVIOUS.replace("'UNKNOWN'", "'UNKNOWN','RECONCILING'")
    )


def downgrade():
    op.execute(
        sa.text(
            "UPDATE publication_operations SET state = 'UNKNOWN', "
            "reconcile_attempts = reconcile_attempts - 1 WHERE state = 'RECONCILING'"
        )
    )
    op.drop_constraint(NAME, TABLE, type_="check")
    op.create_check_constraint(NAME, TABLE, PREVIOUS)
