"""hospital buildings

Adds hospital_buildings: per-building SB 1953 seismic ratings (SPC/NPC)
from HCAI's public CHHS Open Data CSV "Seismic Ratings and Collapse
Probabilities of California Hospitals" -- see app/pipeline/hcai.py for the
import (manual, `scout import-hcai-seismic`) and the deadline derivation,
and app/assumptions.py's "Hospital seismic compliance" group for the CHHS
Terms of Use findings.

A separate population from projects and retrofit_buildings, on purpose --
see HospitalBuilding's own docstring in app/models.py.

Revision ID: 4954b1c4df3b
Revises: b6d4a91f3c58
Create Date: 2026-08-17 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = '4954b1c4df3b'
down_revision = 'b6d4a91f3c58'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'hospital_buildings',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('perm_id', sa.String(), nullable=False),
        sa.Column('building_nbr', sa.String(), nullable=False),
        sa.Column('facility_name', sa.String(), nullable=False),
        sa.Column('building_name', sa.String(), nullable=True),
        sa.Column('building_status', sa.String(), nullable=True),
        sa.Column('city', sa.String(), nullable=True),
        sa.Column('county', sa.String(), nullable=False),
        sa.Column('state', sa.String(), nullable=False, server_default='CA'),
        sa.Column('spc_rating', sa.String(), nullable=True),
        sa.Column('npc_rating', sa.String(), nullable=True),
        sa.Column('hazus_2010_pct', sa.Float(), nullable=True),
        sa.Column('ab1882_notice', sa.String(), nullable=True),
        sa.Column('latitude', sa.Float(), nullable=True),
        sa.Column('longitude', sa.Float(), nullable=True),
        sa.Column('spc_deadline_year', sa.Integer(), nullable=True),
        sa.Column('npc_deadline_year', sa.Integer(), nullable=True),
        sa.Column('meets_2030_standard', sa.Boolean(), nullable=True),
        sa.Column('has_filed_extension', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('snapshot_date', sa.DateTime(), nullable=False),
        sa.Column('source_url', sa.String(), nullable=False),
        sa.Column('imported_at', sa.DateTime(), nullable=False),
        sa.UniqueConstraint('perm_id', 'building_nbr', name='uq_hospital_building'),
    )
    op.create_index('ix_hospital_buildings_perm_id', 'hospital_buildings', ['perm_id'])
    op.create_index('ix_hospital_buildings_building_nbr', 'hospital_buildings', ['building_nbr'])
    op.create_index('ix_hospital_buildings_facility_name', 'hospital_buildings', ['facility_name'])
    op.create_index('ix_hospital_buildings_county', 'hospital_buildings', ['county'])
    op.create_index('ix_hospital_buildings_state', 'hospital_buildings', ['state'])
    op.create_index('ix_hospital_buildings_spc_rating', 'hospital_buildings', ['spc_rating'])
    op.create_index('ix_hospital_buildings_npc_rating', 'hospital_buildings', ['npc_rating'])
    op.create_index('ix_hospital_buildings_has_filed_extension', 'hospital_buildings', ['has_filed_extension'])
    op.create_index('ix_hospital_buildings_snapshot_date', 'hospital_buildings', ['snapshot_date'])
    op.create_index('ix_hospital_buildings_imported_at', 'hospital_buildings', ['imported_at'])


def downgrade() -> None:
    op.drop_table('hospital_buildings')
