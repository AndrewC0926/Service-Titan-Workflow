"""replacement candidate estimated tonnage

Adds retrofit_buildings.estimated_tons_low/high/basis -- a sqft-derived
tonnage BAND for the replacement_candidate population (no permit text
exists to mine mined_tons_each from). See app/pipeline/retrofit.py:
estimate_tonnage and config.yaml's retrofit.candidate_sqft_per_ton.

service_life_status/service_life_basis/equipment_age_years already exist
on this table (added by 43c1b9205250 for the recently_active population)
and are now also populated for replacement_candidate rows via a
YearBuilt-derived proxy -- no schema change needed for that, just a basis
string prefix (YEARBUILT-DERIVED) distinguishing it from a permit-verified
one.

Revision ID: 256a5b3fb78b
Revises: 7ea1dc25b51c
Create Date: 2026-08-09 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = '256a5b3fb78b'
down_revision = '7ea1dc25b51c'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('retrofit_buildings', sa.Column('estimated_tons_low', sa.Float(), nullable=True))
    op.add_column('retrofit_buildings', sa.Column('estimated_tons_high', sa.Float(), nullable=True))
    op.add_column('retrofit_buildings', sa.Column('estimated_tons_basis', sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column('retrofit_buildings', 'estimated_tons_basis')
    op.drop_column('retrofit_buildings', 'estimated_tons_high')
    op.drop_column('retrofit_buildings', 'estimated_tons_low')
