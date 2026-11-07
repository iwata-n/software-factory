"""Separate project source visibility from the execution write allowlist."""

from alembic import op

revision = "0030"
down_revision = "0029"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""UPDATE projects SET policy =
        (policy::jsonb || '{"allowed_read_paths":["."]}'::jsonb)::json
        WHERE NOT (policy::jsonb ? 'allowed_read_paths')""")


def downgrade():
    op.execute("UPDATE projects SET policy = (policy::jsonb - 'allowed_read_paths')::json")
