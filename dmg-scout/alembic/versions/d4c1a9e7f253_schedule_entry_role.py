"""schedule_entries.role

Adds schedule_entries.role -- which of app.accounts.ROLE_ORDER's 13
building roles a scheduled tag's equipment_type belongs to, so a tag maps
onto DMG's own line card (ProductLine.building_role) directly. See
app/schedule_mapping.py.

Revision ID: d4c1a9e7f253
Revises: b7e42f9a1c36
Create Date: 2026-08-20 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = 'd4c1a9e7f253'
down_revision = 'b7e42f9a1c36'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('schedule_entries', sa.Column('role', sa.String(), nullable=True))
    op.create_index('ix_schedule_entries_role', 'schedule_entries', ['role'])


def downgrade() -> None:
    op.drop_index('ix_schedule_entries_role', table_name='schedule_entries')
    op.drop_column('schedule_entries', 'role')
