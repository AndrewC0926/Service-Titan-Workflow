"""competitor_lines.covered_counties

Adds competitor_lines.covered_counties (JSON list, default []). A rep
firm's territory is not statewide just because it's the only channel found
for a manufacturer -- Sigler SoCal Engineering's own locations page names
5 of Scout's 7 territory counties (Los Angeles, Orange, Riverside, San
Bernardino, San Diego) and excludes Imperial and Kern. Storing the
confirmed counties on the row itself (backed by the row's own existing
source_url/retrieved_at) lets app/schedule_mapping.py's resolve_displacement
resolve an out-of-coverage-county project to "unknown" instead of to a rep
firm that never confirmed it serves that county. Empty list means "not
researched", not "covers everywhere" -- see CompetitorLine's docstring.

Revision ID: 2ecc399b640a
Revises: 7058fd02a356
Create Date: 2026-08-21 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = '2ecc399b640a'
down_revision = '7058fd02a356'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('competitor_lines', sa.Column(
        'covered_counties', sa.JSON(), nullable=False, server_default='[]'))


def downgrade() -> None:
    op.drop_column('competitor_lines', 'covered_counties')
