"""opportunities.facility_perm_id -- the third anchor (Block 4A Item 1)

"An Opportunity may anchor on an Account, a Building, or a Deadline
facility" (Master Plan v3.6). Ab869Plan is facility-grain (one row per
perm_id, unique -- see its own uq_ab869_plan_perm_id constraint), so a
plain string FK to it, nullable, additive.

Verified rollback: downgrade() drops the index then the column, checked
by actually running upgrade -> downgrade -> upgrade against the local DB.

Revision ID: aebb7f6bd153
Revises: 067c0ef15e70
Create Date: 2026-09-13 00:05:00.000000
"""
from alembic import op
import sqlalchemy as sa

revision = 'aebb7f6bd153'
down_revision = '067c0ef15e70'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('opportunities', sa.Column('facility_perm_id', sa.String(), nullable=True))
    op.create_foreign_key('fk_opportunities_facility_perm_id_ab869_plans', 'opportunities',
                          'ab869_plans', ['facility_perm_id'], ['perm_id'])
    op.create_index(op.f('ix_opportunities_facility_perm_id'), 'opportunities', ['facility_perm_id'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_opportunities_facility_perm_id'), table_name='opportunities')
    op.drop_constraint('fk_opportunities_facility_perm_id_ab869_plans', 'opportunities', type_='foreignkey')
    op.drop_column('opportunities', 'facility_perm_id')
