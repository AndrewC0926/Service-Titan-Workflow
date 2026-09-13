"""dmg_customer_roster (Block 4B-prep-3 Item 5)

Minimal snapshot of DMG's own NetSuite customer master -- company_name,
netsuite_internal_id, category, assigned_rep only. Created empty; Andrew
loads it separately when authorized (see app.pipeline.dmg_customer_roster's
own module docstring). No uniqueness constraint on name_norm -- the real
master file carries more than one row for the exact same company name
under distinct internal ids (see app.models.DmgCustomerRoster's own
docstring).

Verified rollback: downgrade() drops the table, checked by running
upgrade -> downgrade -> upgrade against the local DB before this
migration was committed.

Revision ID: f2e00a774632
Revises: 593ad5c8447c
Create Date: 2026-09-13 09:00:00.000000
"""
from alembic import op
import sqlalchemy as sa

revision = 'f2e00a774632'
down_revision = '593ad5c8447c'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'dmg_customer_roster',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('company_name', sa.String(), nullable=False),
        sa.Column('name_norm', sa.String(), nullable=False),
        sa.Column('netsuite_internal_id', sa.String(), nullable=True),
        sa.Column('category', sa.String(), nullable=True),
        sa.Column('assigned_rep', sa.String(), nullable=True),
        sa.Column('imported_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_dmg_customer_roster_name_norm'), 'dmg_customer_roster', ['name_norm'], unique=False)
    op.create_index(op.f('ix_dmg_customer_roster_netsuite_internal_id'), 'dmg_customer_roster',
                    ['netsuite_internal_id'], unique=False)
    op.create_index(op.f('ix_dmg_customer_roster_imported_at'), 'dmg_customer_roster', ['imported_at'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_dmg_customer_roster_imported_at'), table_name='dmg_customer_roster')
    op.drop_index(op.f('ix_dmg_customer_roster_netsuite_internal_id'), table_name='dmg_customer_roster')
    op.drop_index(op.f('ix_dmg_customer_roster_name_norm'), table_name='dmg_customer_roster')
    op.drop_table('dmg_customer_roster')
