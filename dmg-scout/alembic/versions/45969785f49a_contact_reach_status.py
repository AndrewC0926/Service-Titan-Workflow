"""contact reach status

Adds contacts.reach_status (confirmed | pending), backfilled to 'confirmed'
for every existing row -- every Contact row written before this migration
required a phone or email to exist at all (the dashboard form and
import_enriched_contact() both enforce that). 'pending' is new: a name and
title found by the free Lusha/Apollo search layer, no reveal spent, no
phone/email yet -- see app/enrichment.py:import_pending_contact and
Contact.reach_status in app/models.py.

Revision ID: 45969785f49a
Revises: 18a93f30c146
Create Date: 2026-08-08 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa
import sqlmodel


revision = '45969785f49a'
down_revision = '18a93f30c146'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('contacts', sa.Column(
        'reach_status', sqlmodel.sql.sqltypes.AutoString(), nullable=False,
        server_default='confirmed'))
    op.create_index(op.f('ix_contacts_reach_status'), 'contacts', ['reach_status'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_contacts_reach_status'), table_name='contacts')
    op.drop_column('contacts', 'reach_status')
