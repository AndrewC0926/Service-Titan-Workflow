"""service life years past

Adds retrofit_buildings.service_life_years_past -- age_years minus the
service-life LOW threshold. status alone is a 4-value tier that saturates
hard on a population built to skew old (measured: 95% "overdue" on
replacement_candidate after the YearBuilt-proxy dating work), which made
the status stop discriminating and left size doing the real sorting while
the board labelled it urgency -- same failure shape as the spillover
weight. This is the gradient underneath the tier: persisted so
rank_buildings and the board display the SAME number, not two that can
drift. See app/pipeline/retrofit.py:rank_buildings.

Revision ID: 8d3c6f1a92be
Revises: 256a5b3fb78b
Create Date: 2026-08-09 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = '8d3c6f1a92be'
down_revision = '256a5b3fb78b'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('retrofit_buildings', sa.Column('service_life_years_past', sa.Float(), nullable=True))


def downgrade() -> None:
    op.drop_column('retrofit_buildings', 'service_life_years_past')
