"""AB 802 statewide benchmarking (ab802_buildings)

One new table, ab802_buildings, keyed (portfolio_manager_property_id,
year_ending) -- see app.models.Ab802Building and app.pipeline.ab802's
module docstrings for the full design (annual full-replace-per-year, no
owner column, the loose lat/long-then-address join to RetrofitBuilding and
EbeweBenchmark).

Revision ID: d752ba32526f
Revises: 21978311fa2c
Create Date: 2026-09-07 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa
import sqlmodel


revision = 'd752ba32526f'
down_revision = '21978311fa2c'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('ab802_buildings',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('portfolio_manager_property_id', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
    sa.Column('standard_id', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
    sa.Column('property_name', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
    sa.Column('address_1', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
    sa.Column('city', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
    sa.Column('state_province', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
    sa.Column('postal_code', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
    sa.Column('property_gfa_sqft', sa.Float(), nullable=True),
    sa.Column('primary_property_type', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
    sa.Column('all_property_use_types', sa.Text(), nullable=True),
    sa.Column('weather_normalized_site_eui', sa.Float(), nullable=True),
    sa.Column('natural_gas_use_kbtu', sa.Float(), nullable=True),
    sa.Column('electricity_grid_purchase_kbtu', sa.Float(), nullable=True),
    sa.Column('electricity_onsite_renewable_kbtu', sa.Float(), nullable=True),
    sa.Column('fuel_oil_2_use_kbtu', sa.Float(), nullable=True),
    sa.Column('district_steam_use_kbtu', sa.Float(), nullable=True),
    sa.Column('diesel_use_kbtu', sa.Float(), nullable=True),
    sa.Column('propane_use_kbtu', sa.Float(), nullable=True),
    sa.Column('district_hot_water_use_kbtu', sa.Float(), nullable=True),
    sa.Column('district_chilled_water_use_kbtu', sa.Float(), nullable=True),
    sa.Column('year_built', sa.Integer(), nullable=True),
    sa.Column('w_energy', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
    sa.Column('energy_star_score', sa.Integer(), nullable=True),
    sa.Column('energy_star_certified', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
    sa.Column('energy_star_cert_years', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
    sa.Column('latitude', sa.Float(), nullable=True),
    sa.Column('longitude', sa.Float(), nullable=True),
    sa.Column('county_from_geocoding', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
    sa.Column('report_generation_date', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
    sa.Column('used_estimated_energy_values', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
    sa.Column('alert_partial_year_data', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
    sa.Column('total_ghg_emissions_metric_tons', sa.Float(), nullable=True),
    sa.Column('ghg_emissions_intensity', sa.Float(), nullable=True),
    sa.Column('year_ending', sa.Integer(), nullable=False),
    sa.Column('number_of_buildings', sa.Integer(), nullable=True),
    sa.Column('in_territory', sa.Boolean(), nullable=False),
    sa.Column('assessor_match_method', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
    sa.Column('assessor_match_distance_m', sa.Float(), nullable=True),
    sa.Column('retrofit_apn', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
    sa.Column('ebewe_building_id', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
    sa.Column('benchmarking_filer', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
    sa.Column('source_url', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
    sa.Column('imported_at', sa.DateTime(), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('portfolio_manager_property_id', 'year_ending', name='uq_ab802_property_year')
    )
    op.create_index(op.f('ix_ab802_buildings_portfolio_manager_property_id'), 'ab802_buildings', ['portfolio_manager_property_id'], unique=False)
    op.create_index(op.f('ix_ab802_buildings_primary_property_type'), 'ab802_buildings', ['primary_property_type'], unique=False)
    op.create_index(op.f('ix_ab802_buildings_year_built'), 'ab802_buildings', ['year_built'], unique=False)
    op.create_index(op.f('ix_ab802_buildings_county_from_geocoding'), 'ab802_buildings', ['county_from_geocoding'], unique=False)
    op.create_index(op.f('ix_ab802_buildings_year_ending'), 'ab802_buildings', ['year_ending'], unique=False)
    op.create_index(op.f('ix_ab802_buildings_in_territory'), 'ab802_buildings', ['in_territory'], unique=False)
    op.create_index(op.f('ix_ab802_buildings_assessor_match_method'), 'ab802_buildings', ['assessor_match_method'], unique=False)
    op.create_index(op.f('ix_ab802_buildings_retrofit_apn'), 'ab802_buildings', ['retrofit_apn'], unique=False)
    op.create_index(op.f('ix_ab802_buildings_imported_at'), 'ab802_buildings', ['imported_at'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_ab802_buildings_imported_at'), table_name='ab802_buildings')
    op.drop_index(op.f('ix_ab802_buildings_retrofit_apn'), table_name='ab802_buildings')
    op.drop_index(op.f('ix_ab802_buildings_assessor_match_method'), table_name='ab802_buildings')
    op.drop_index(op.f('ix_ab802_buildings_in_territory'), table_name='ab802_buildings')
    op.drop_index(op.f('ix_ab802_buildings_year_ending'), table_name='ab802_buildings')
    op.drop_index(op.f('ix_ab802_buildings_county_from_geocoding'), table_name='ab802_buildings')
    op.drop_index(op.f('ix_ab802_buildings_year_built'), table_name='ab802_buildings')
    op.drop_index(op.f('ix_ab802_buildings_primary_property_type'), table_name='ab802_buildings')
    op.drop_index(op.f('ix_ab802_buildings_portfolio_manager_property_id'), table_name='ab802_buildings')
    op.drop_table('ab802_buildings')
