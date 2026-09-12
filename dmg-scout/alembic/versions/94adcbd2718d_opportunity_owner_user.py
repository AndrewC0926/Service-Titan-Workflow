"""opportunities.owner_user (Block 4B-prep Item 2)

Required, no real default -- every caller must state who is creating the
Opportunity (see app.models.Opportunity.owner_user's own docstring).
server_default is a migration-time-only placeholder for the ALTER TABLE
itself (there are 0 real Opportunity rows in production as of this
migration, confirmed directly, so this never actually backfills a real
row with a fake owner) -- dropped immediately after, same pattern this
repo's other NOT-NULL-column additions already use.

Verified rollback: downgrade() drops the column, checked by actually
running upgrade -> downgrade -> upgrade against the local DB before this
migration was committed.

Revision ID: 94adcbd2718d
Revises: 5897d8167aab
Create Date: 2026-09-13 05:00:00.000000
"""
from alembic import op
import sqlalchemy as sa

revision = '94adcbd2718d'
down_revision = '5897d8167aab'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('opportunities', sa.Column('owner_user', sa.String(), nullable=False,
                                             server_default='system'))
    op.create_index(op.f('ix_opportunities_owner_user'), 'opportunities', ['owner_user'], unique=False)
    op.alter_column('opportunities', 'owner_user', server_default=None)


def downgrade() -> None:
    op.drop_index(op.f('ix_opportunities_owner_user'), table_name='opportunities')
    op.drop_column('opportunities', 'owner_user')
