"""access_log.role (Block 4B Item 5)

Nullable snapshot of app.access_log.user_role()'s answer at the moment of
each hit -- "Audit log of who viewed and changed what" (Master Plan v3.6
section 35). Never backfilled for existing rows: there is no reliable way
to know what role a past visitor had at the time of a historical hit,
and guessing one would misrepresent the audit trail.

Verified rollback: downgrade() drops the column, checked by running
upgrade -> downgrade -> upgrade against the local DB before this
migration was committed.

Revision ID: 7d917d4d4406
Revises: 6fec08eb6113
Create Date: 2026-09-13 15:00:00.000000
"""
from alembic import op
import sqlalchemy as sa

revision = '7d917d4d4406'
down_revision = '6fec08eb6113'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('access_log', sa.Column('role', sa.String(), nullable=True))
    op.create_index(op.f('ix_access_log_role'), 'access_log', ['role'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_access_log_role'), table_name='access_log')
    op.drop_column('access_log', 'role')
