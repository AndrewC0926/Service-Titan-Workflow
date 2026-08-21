"""schedule_entries.capacity_btuh

Adds schedule_entries.capacity_btuh and .capacity_corroborated. A document
row can state the same headline capacity twice, once in tons and once in
BTU/H (e.g. "Total Capacity 92,700 BTU/H" / "Nom Tons 8" on the same AH1
spec block) -- that is corroboration of one fact, not two competing
answers to pick between. capacity_btuh holds the second, independently
grounded reading; capacity_corroborated records whether the two agreed.
See app/grounding.py's ground_schedule_entry and app/schemas.py's
capacity_btuh docstring.

Revision ID: 7058fd02a356
Revises: e8f1b3d6a904
Create Date: 2026-08-21 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = '7058fd02a356'
down_revision = 'e8f1b3d6a904'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('schedule_entries', sa.Column('capacity_btuh', sa.Float(), nullable=True))
    op.add_column('schedule_entries', sa.Column('capacity_corroborated', sa.Boolean(), nullable=True))


def downgrade() -> None:
    op.drop_column('schedule_entries', 'capacity_corroborated')
    op.drop_column('schedule_entries', 'capacity_btuh')
