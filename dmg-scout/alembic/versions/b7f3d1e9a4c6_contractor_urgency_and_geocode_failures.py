"""contractor urgency ranking + durable geocode-failure marker

Adds Contractor.nearby_urgency_score / nearby_estimated_tons (raw proximity
count alone didn't discriminate -- measured 2026-08-15, top 10 by count
spanned 1,430-1,456, under 2% -- so /contractors now ranks on aggregate
service-life urgency instead, count kept for context) and a new
retrofit_geocode_failures table (durable "attempted, unmatched" marker so
app.pipeline.retrofit.geocode_retrofit_buildings stops resubmitting the same
permanently-unmatchable addresses to Census on every run -- confirmed real,
the same 1,896 apns retried five times with zero new matches in one
session). See Contractor and RetrofitGeocodeFailure docstrings in
app/models.py.

Revision ID: b7f3d1e9a4c6
Revises: a1c4e7f9b2d3
Create Date: 2026-08-16 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = 'b7f3d1e9a4c6'
down_revision = 'a1c4e7f9b2d3'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('contractors', sa.Column('nearby_urgency_score', sa.Float(), nullable=True))
    op.add_column('contractors', sa.Column('nearby_estimated_tons', sa.Float(), nullable=True))
    op.create_table(
        'retrofit_geocode_failures',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('apn', sa.String(), nullable=False),
        sa.Column('source_address', sa.Text(), nullable=False, server_default=''),
        sa.Column('attempted_at', sa.DateTime(), nullable=False),
    )
    op.create_index('ix_retrofit_geocode_failures_apn', 'retrofit_geocode_failures', ['apn'], unique=True)
    op.create_index('ix_retrofit_geocode_failures_attempted_at', 'retrofit_geocode_failures', ['attempted_at'])


def downgrade() -> None:
    op.drop_index('ix_retrofit_geocode_failures_attempted_at', table_name='retrofit_geocode_failures')
    op.drop_index('ix_retrofit_geocode_failures_apn', table_name='retrofit_geocode_failures')
    op.drop_table('retrofit_geocode_failures')
    op.drop_column('contractors', 'nearby_estimated_tons')
    op.drop_column('contractors', 'nearby_urgency_score')
