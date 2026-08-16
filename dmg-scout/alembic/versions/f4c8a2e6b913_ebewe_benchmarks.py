"""ebewe benchmarks

Adds ebewe_benchmarks (raw annual filings from data.lacity.org Socrata
9yda-i4ya) and the retrofit_buildings columns that carry the address-joined
result of the MOST RECENT filing per building onto a retrofit board row --
see app/pipeline/ebewe.py and app/pipeline/regulatory.py:arcx_compliance_status.

ebewe_matched is a plain (unindexed-elsewhere) boolean gate for the
"has EBEWE data" board filter; ebewe_arcx_due_this_year is a second,
independent boolean for the A/RCx compliance-cycle flag, which is useful on
its own regardless of how much of the board carries a benchmark match.

Revision ID: f4c8a2e6b913
Revises: e2a9c4f7b1d8
Create Date: 2026-08-16 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = 'f4c8a2e6b913'
down_revision = 'e2a9c4f7b1d8'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'ebewe_benchmarks',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('source', sa.String(), nullable=False),
        sa.Column('building_id', sa.String(), nullable=False),
        sa.Column('program_year', sa.Integer(), nullable=False),
        sa.Column('ain_last3', sa.String(), nullable=True),
        sa.Column('building_address', sa.String(), nullable=True),
        sa.Column('postal_code', sa.String(), nullable=True),
        sa.Column('primary_property_type', sa.String(), nullable=True),
        sa.Column('property_gfa', sa.Float(), nullable=True),
        sa.Column('year_built', sa.Integer(), nullable=True),
        sa.Column('occupancy', sa.Float(), nullable=True),
        sa.Column('compliance_status', sa.String(), nullable=True),
        sa.Column('organization', sa.String(), nullable=True),
        sa.Column('number_of_buildings', sa.Integer(), nullable=True),
        sa.Column('site_eui', sa.Float(), nullable=True),
        sa.Column('source_eui', sa.Float(), nullable=True),
        sa.Column('weather_normalized_site_eui', sa.Float(), nullable=True),
        sa.Column('weather_normalized_source_eui', sa.Float(), nullable=True),
        sa.Column('percent_diff_national_median_site_eui', sa.Float(), nullable=True),
        sa.Column('percent_diff_national_median_source_eui', sa.Float(), nullable=True),
        sa.Column('energy_star_score', sa.Integer(), nullable=True),
        sa.Column('energy_star_cert_years', sa.String(), nullable=True),
        sa.Column('total_ghg_emissions', sa.Float(), nullable=True),
        sa.Column('indoor_water_use', sa.Float(), nullable=True),
        sa.Column('indoor_water_use_intensity', sa.Float(), nullable=True),
        sa.Column('outdoor_water_use', sa.Float(), nullable=True),
        sa.Column('total_water_use', sa.Float(), nullable=True),
        sa.Column('source_url', sa.String(), nullable=False),
        sa.Column('imported_at', sa.DateTime(), nullable=False),
        sa.UniqueConstraint('building_id', 'program_year', name='uq_ebewe_building_year'),
    )
    op.create_index('ix_ebewe_benchmarks_source', 'ebewe_benchmarks', ['source'])
    op.create_index('ix_ebewe_benchmarks_building_id', 'ebewe_benchmarks', ['building_id'])
    op.create_index('ix_ebewe_benchmarks_program_year', 'ebewe_benchmarks', ['program_year'])
    op.create_index('ix_ebewe_benchmarks_imported_at', 'ebewe_benchmarks', ['imported_at'])

    op.add_column('retrofit_buildings', sa.Column('ebewe_matched', sa.Boolean(), nullable=False,
                                                    server_default=sa.false()))
    op.add_column('retrofit_buildings', sa.Column('ebewe_building_id', sa.String(), nullable=True))
    op.add_column('retrofit_buildings', sa.Column('ebewe_program_year', sa.Integer(), nullable=True))
    op.add_column('retrofit_buildings', sa.Column('ebewe_energy_star_score', sa.Integer(), nullable=True))
    op.add_column('retrofit_buildings', sa.Column('ebewe_site_eui', sa.Float(), nullable=True))
    op.add_column('retrofit_buildings', sa.Column('ebewe_weather_normalized_site_eui', sa.Float(), nullable=True))
    op.add_column('retrofit_buildings', sa.Column('ebewe_property_type', sa.String(), nullable=True))
    op.add_column('retrofit_buildings', sa.Column('ebewe_arcx_due_this_year', sa.Boolean(), nullable=False,
                                                    server_default=sa.false()))
    op.add_column('retrofit_buildings', sa.Column('ebewe_arcx_next_compliance_date', sa.DateTime(), nullable=True))
    op.create_index('ix_retrofit_buildings_ebewe_matched', 'retrofit_buildings', ['ebewe_matched'])
    op.create_index('ix_retrofit_buildings_ebewe_arcx_due_this_year', 'retrofit_buildings',
                     ['ebewe_arcx_due_this_year'])


def downgrade() -> None:
    op.drop_index('ix_retrofit_buildings_ebewe_arcx_due_this_year', table_name='retrofit_buildings')
    op.drop_index('ix_retrofit_buildings_ebewe_matched', table_name='retrofit_buildings')
    op.drop_column('retrofit_buildings', 'ebewe_arcx_next_compliance_date')
    op.drop_column('retrofit_buildings', 'ebewe_arcx_due_this_year')
    op.drop_column('retrofit_buildings', 'ebewe_property_type')
    op.drop_column('retrofit_buildings', 'ebewe_weather_normalized_site_eui')
    op.drop_column('retrofit_buildings', 'ebewe_site_eui')
    op.drop_column('retrofit_buildings', 'ebewe_energy_star_score')
    op.drop_column('retrofit_buildings', 'ebewe_program_year')
    op.drop_column('retrofit_buildings', 'ebewe_building_id')
    op.drop_column('retrofit_buildings', 'ebewe_matched')

    op.drop_table('ebewe_benchmarks')


