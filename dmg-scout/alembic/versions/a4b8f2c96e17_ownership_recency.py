"""change-of-ownership recency

Adds ownership_recency (durable, survives RetrofitBuilding's own
DELETE-and-reinsert rebuild -- same pattern as retrofit_geocodes) and
retrofit_buildings.last_sale_date/last_sale_source/last_sale_checked_at,
rejoined at build time. See app/pipeline/ownership.py's module docstring
for the LA County Recorder compliance finding and why this uses the
Assessor's own RecordingDate field instead.

Revision ID: a4b8f2c96e17
Revises: f9c3e7a15d82
Create Date: 2026-08-16 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = 'a4b8f2c96e17'
down_revision = 'f9c3e7a15d82'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'ownership_recency',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('apn', sa.String(), nullable=False),
        sa.Column('last_sale_date', sa.DateTime(), nullable=False),
        sa.Column('source', sa.String(), nullable=False, server_default='la_county_assessor_recording_date'),
        sa.Column('checked_at', sa.DateTime(), nullable=False),
    )
    op.create_index('ix_ownership_recency_apn', 'ownership_recency', ['apn'], unique=True)
    op.create_index('ix_ownership_recency_last_sale_date', 'ownership_recency', ['last_sale_date'])
    op.create_index('ix_ownership_recency_checked_at', 'ownership_recency', ['checked_at'])

    op.add_column('retrofit_buildings', sa.Column('last_sale_date', sa.DateTime(), nullable=True))
    op.add_column('retrofit_buildings', sa.Column('last_sale_source', sa.String(), nullable=True))
    op.add_column('retrofit_buildings', sa.Column('last_sale_checked_at', sa.DateTime(), nullable=True))


def downgrade() -> None:
    op.drop_column('retrofit_buildings', 'last_sale_checked_at')
    op.drop_column('retrofit_buildings', 'last_sale_source')
    op.drop_column('retrofit_buildings', 'last_sale_date')
    op.drop_table('ownership_recency')
