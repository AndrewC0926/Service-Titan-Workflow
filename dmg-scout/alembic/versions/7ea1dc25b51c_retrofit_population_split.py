"""retrofit population split

Adds retrofit_buildings.population (recently_active | replacement_candidate)
and building_age_years. Presence of a permit is evidence someone already
replaced; ABSENCE across the full 2010-present permit window is the real
retrofit-opportunity signal -- see app/pipeline/retrofit.py:
find_replacement_candidates.

Revision ID: 7ea1dc25b51c
Revises: 43c1b9205250
Create Date: 2026-08-08 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa
import sqlmodel


revision = '7ea1dc25b51c'
down_revision = '43c1b9205250'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('retrofit_buildings', sa.Column(
        'population', sqlmodel.sql.sqltypes.AutoString(), nullable=False,
        server_default='recently_active'))
    op.add_column('retrofit_buildings', sa.Column('building_age_years', sa.Float(), nullable=True))
    op.create_index(op.f('ix_retrofit_buildings_population'), 'retrofit_buildings', ['population'])


def downgrade() -> None:
    op.drop_index(op.f('ix_retrofit_buildings_population'), table_name='retrofit_buildings')
    op.drop_column('retrofit_buildings', 'building_age_years')
    op.drop_column('retrofit_buildings', 'population')
