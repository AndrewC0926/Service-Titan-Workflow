"""contacts: NetSuite import fields (Block 4B-prep Item 1)

Additive only. Verified rollback: downgrade() drops every added column in
reverse order, checked by actually running upgrade -> downgrade -> upgrade
against the local DB before this migration was committed.

Revision ID: 5897d8167aab
Revises: cb0064d3f10d
Create Date: 2026-09-13 04:00:00.000000
"""
from alembic import op
import sqlalchemy as sa

revision = '5897d8167aab'
down_revision = 'cb0064d3f10d'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('contacts', sa.Column('netsuite_internal_id', sa.Integer(), nullable=True))
    op.add_column('contacts', sa.Column('first_name', sa.String(), nullable=True))
    op.add_column('contacts', sa.Column('last_name', sa.String(), nullable=True))
    op.add_column('contacts', sa.Column('mobile', sa.String(), nullable=True))
    op.add_column('contacts', sa.Column('is_active', sa.Boolean(), nullable=True))
    op.add_column('contacts', sa.Column('customer_ref_id', sa.String(), nullable=True))
    op.add_column('contacts', sa.Column('customer_ref_name', sa.String(), nullable=True))
    op.add_column('contacts', sa.Column('account_id', sa.Integer(), sa.ForeignKey('accounts.id'), nullable=True))
    op.add_column('contacts', sa.Column('reachable', sa.Boolean(), nullable=False, server_default='false'))

    op.create_unique_constraint('uq_contacts_netsuite_internal_id', 'contacts', ['netsuite_internal_id'])
    for col in ('netsuite_internal_id', 'customer_ref_id', 'customer_ref_name', 'account_id', 'reachable'):
        op.create_index(op.f(f'ix_contacts_{col}'), 'contacts', [col], unique=False)
    op.alter_column('contacts', 'reachable', server_default=None)


def downgrade() -> None:
    for col in ('reachable', 'account_id', 'customer_ref_name', 'customer_ref_id', 'netsuite_internal_id'):
        op.drop_index(op.f(f'ix_contacts_{col}'), table_name='contacts')
    op.drop_constraint('uq_contacts_netsuite_internal_id', 'contacts', type_='unique')

    op.drop_column('contacts', 'reachable')
    op.drop_column('contacts', 'account_id')
    op.drop_column('contacts', 'customer_ref_name')
    op.drop_column('contacts', 'customer_ref_id')
    op.drop_column('contacts', 'is_active')
    op.drop_column('contacts', 'mobile')
    op.drop_column('contacts', 'last_name')
    op.drop_column('contacts', 'first_name')
    op.drop_column('contacts', 'netsuite_internal_id')
