"""service frequency: manual entry, retrofit board override

Adds service_frequency_reports (the durable, manually-entered source of
truth -- one row per report, keyed by apn, never touched by
RetrofitBuilding's rebuild-and-replace) and the three cache columns on
retrofit_buildings that the rebuild joins the latest report onto:
service_calls_per_year / service_calls_per_year_source /
service_calls_per_year_reported_at. See app/models.py's
ServiceFrequencyReport docstring and app/pipeline/retrofit.py:rank_buildings.

Revision ID: 2f7cdd7ffbb6
Revises: 88e57e48bf17
Create Date: 2026-08-11 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = '2f7cdd7ffbb6'
down_revision = '88e57e48bf17'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'service_frequency_reports',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('apn', sa.String(), nullable=False),
        sa.Column('service_calls_per_year', sa.Float(), nullable=False),
        sa.Column('source', sa.String(), nullable=False),
        sa.Column('reported_at', sa.DateTime(), nullable=False),
        sa.Column('equipment_note', sa.String(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
    )
    op.create_index('ix_service_frequency_reports_apn', 'service_frequency_reports', ['apn'])

    op.add_column('retrofit_buildings', sa.Column('service_calls_per_year', sa.Float(), nullable=True))
    op.add_column('retrofit_buildings', sa.Column('service_calls_per_year_source', sa.String(), nullable=True))
    op.add_column('retrofit_buildings', sa.Column(
        'service_calls_per_year_reported_at', sa.DateTime(), nullable=True))


def downgrade() -> None:
    op.drop_column('retrofit_buildings', 'service_calls_per_year_reported_at')
    op.drop_column('retrofit_buildings', 'service_calls_per_year_source')
    op.drop_column('retrofit_buildings', 'service_calls_per_year')
    op.drop_index('ix_service_frequency_reports_apn', table_name='service_frequency_reports')
    op.drop_table('service_frequency_reports')
