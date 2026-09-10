"""accounts NetSuite import fields

Phase B for the NetSuite "Scout" customer import (7,093-row saved-search
export). Adds every column app.importers.netsuite_customers needs and
makes account_type nullable -- see that module's own docstring for why
(the former "mechanical_contractor" default/fallback silently mis-typed
every blank-Category row, 3,674 of 7,093 in the real file).

netsuite_internal_id is the real join key for anything NetSuite-sourced
(unique, since NetSuite's own internal id is unique per customer record);
name matching is never used for identity here. netsuite_parent_internal_id
holds the PARENT's own internal id when NetSuite's export actually supplied
a differing one -- see app.importers.netsuite_customers's own docstring for
whether the real file's 86 child rows carried that (checked directly
against the real export before writing this migration, not assumed).

Revision ID: a1f278b35dac
Revises: ae1f913b9bb6
Create Date: 2026-09-11 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = 'a1f278b35dac'
down_revision = 'ae1f913b9bb6'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column('accounts', 'account_type', existing_type=sa.VARCHAR(),
                    nullable=True, server_default=None)

    op.add_column('accounts', sa.Column('netsuite_internal_id', sa.Integer(), nullable=True))
    op.create_index(op.f('ix_accounts_netsuite_internal_id'), 'accounts', ['netsuite_internal_id'],
                    unique=True)

    op.add_column('accounts', sa.Column('netsuite_entity_id', sa.String(), nullable=True))

    op.add_column('accounts', sa.Column('netsuite_parent_internal_id', sa.Integer(), nullable=True))
    op.create_index(op.f('ix_accounts_netsuite_parent_internal_id'), 'accounts',
                    ['netsuite_parent_internal_id'], unique=False)

    op.add_column('accounts', sa.Column('is_active', sa.Boolean(), nullable=True))
    op.create_index(op.f('ix_accounts_is_active'), 'accounts', ['is_active'], unique=False)

    op.add_column('accounts', sa.Column('netsuite_sales_rep', sa.String(), nullable=True))
    op.add_column('accounts', sa.Column('netsuite_last_modified', sa.DateTime(), nullable=True))

    op.add_column('accounts', sa.Column('is_dmg_internal', sa.Boolean(), nullable=False,
                                        server_default=sa.false()))
    op.create_index(op.f('ix_accounts_is_dmg_internal'), 'accounts', ['is_dmg_internal'], unique=False)
    op.alter_column('accounts', 'is_dmg_internal', server_default=None)


def downgrade() -> None:
    op.drop_index(op.f('ix_accounts_is_dmg_internal'), table_name='accounts')
    op.drop_column('accounts', 'is_dmg_internal')

    op.drop_column('accounts', 'netsuite_last_modified')
    op.drop_column('accounts', 'netsuite_sales_rep')

    op.drop_index(op.f('ix_accounts_is_active'), table_name='accounts')
    op.drop_column('accounts', 'is_active')

    op.drop_index(op.f('ix_accounts_netsuite_parent_internal_id'), table_name='accounts')
    op.drop_column('accounts', 'netsuite_parent_internal_id')

    op.drop_column('accounts', 'netsuite_entity_id')

    op.drop_index(op.f('ix_accounts_netsuite_internal_id'), table_name='accounts')
    op.drop_column('accounts', 'netsuite_internal_id')

    # Restore the pre-migration default so a straight downgrade leaves the
    # column exactly as it was -- app.importers.accounts_csv/netsuite_customers
    # are source-controlled separately and are NOT reverted by this downgrade;
    # re-running either against a downgraded DB would fail on a NULL
    # account_type insert until app.models.Account is reverted too.
    op.alter_column('accounts', 'account_type', existing_type=sa.VARCHAR(),
                    nullable=False, server_default='mechanical_contractor')
