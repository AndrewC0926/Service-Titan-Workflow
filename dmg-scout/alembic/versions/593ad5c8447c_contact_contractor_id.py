"""contacts.contractor_id (Block 4B-prep-2 Item 1)

Nullable FK to contractors.id -- a Contact can be linked to an Account, a
Contractor (CSLB license roster), both, or neither. Populated by
app.pipeline.contact_contractor_match, not by this migration.

Verified rollback: downgrade() drops the column, checked by running
upgrade -> downgrade -> upgrade against the local DB before this
migration was committed.

Revision ID: 593ad5c8447c
Revises: 94adcbd2718d
Create Date: 2026-09-13 06:00:00.000000
"""
from alembic import op
import sqlalchemy as sa

revision = '593ad5c8447c'
down_revision = '94adcbd2718d'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('contacts', sa.Column('contractor_id', sa.Integer(), nullable=True))
    op.create_index(op.f('ix_contacts_contractor_id'), 'contacts', ['contractor_id'], unique=False)
    op.create_foreign_key(
        'fk_contacts_contractor_id_contractors', 'contacts', 'contractors', ['contractor_id'], ['id'],
    )


def downgrade() -> None:
    op.drop_constraint('fk_contacts_contractor_id_contractors', 'contacts', type_='foreignkey')
    op.drop_index(op.f('ix_contacts_contractor_id'), table_name='contacts')
    op.drop_column('contacts', 'contractor_id')
