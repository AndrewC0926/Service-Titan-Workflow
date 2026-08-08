"""retrofit buildings

One table: retrofit_buildings. Deduplicates EquipmentPermit to one row per
building (by APN), joined against assessor parcel characteristics, with
regulatory triggers evaluated and a rank score computed -- see
app/pipeline/retrofit.py. Deliberately no owner name / mailing address
column: verified no free bulk source of that data exists for LA County.

Revision ID: 43c1b9205250
Revises: 2a79a9306050
Create Date: 2026-08-08 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa
import sqlmodel


revision = '43c1b9205250'
down_revision = '2a79a9306050'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'retrofit_buildings',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('apn', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('county', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('state', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('address', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column('use_code', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column('use_desc', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column('sqft', sa.Float(), nullable=True),
        sa.Column('year_built', sa.Integer(), nullable=True),
        sa.Column('permit_count', sa.Integer(), nullable=False),
        sa.Column('latest_permit_nbr', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column('latest_install_year', sa.Integer(), nullable=True),
        sa.Column('equipment_type', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column('mined_tons_each', sa.Float(), nullable=True),
        sa.Column('mined_equipment_count', sa.Integer(), nullable=True),
        sa.Column('inferred_refrigerant', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column('sb1206_trigger_status', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column('sb1206_detail', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column('carb_candidate', sa.Boolean(), nullable=False),
        sa.Column('carb_use_code', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column('ebewe_candidate', sa.Boolean(), nullable=False),
        sa.Column('service_life_status', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column('service_life_basis', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column('equipment_age_years', sa.Float(), nullable=True),
        sa.Column('rank_score', sa.Float(), nullable=True),
        sa.Column('permit_source_url', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column('assessor_source_url', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column('built_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('apn', name='uq_retrofit_building_apn'),
    )
    op.create_index(op.f('ix_retrofit_buildings_apn'), 'retrofit_buildings', ['apn'])
    op.create_index(op.f('ix_retrofit_buildings_county'), 'retrofit_buildings', ['county'])
    op.create_index(op.f('ix_retrofit_buildings_state'), 'retrofit_buildings', ['state'])
    op.create_index(op.f('ix_retrofit_buildings_carb_candidate'), 'retrofit_buildings', ['carb_candidate'])
    op.create_index(op.f('ix_retrofit_buildings_ebewe_candidate'), 'retrofit_buildings', ['ebewe_candidate'])
    op.create_index(op.f('ix_retrofit_buildings_service_life_status'), 'retrofit_buildings', ['service_life_status'])
    op.create_index(op.f('ix_retrofit_buildings_rank_score'), 'retrofit_buildings', ['rank_score'])
    op.create_index(op.f('ix_retrofit_buildings_built_at'), 'retrofit_buildings', ['built_at'])


def downgrade() -> None:
    op.drop_table('retrofit_buildings')
