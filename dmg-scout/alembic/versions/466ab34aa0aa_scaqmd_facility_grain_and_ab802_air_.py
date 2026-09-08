"""SCAQMD facility grain (scaqmd_facilities) and the AB 802 air-permit join

New table scaqmd_facilities -- see app.models.ScaqmdFacility and
app.pipeline.scaqmd's module docstring for the full design (FIND and
Public Document Search both robots.txt-blocked in full, so this is built
from South Coast AQMD's own bulk "Facilities Notified" XLSX instead,
facility grain only). Two new columns on ab802_buildings
(air_permit_facility_id, air_permit_match_method) carry the denormalized
normalized-address join onto that table -- see
app.pipeline.scaqmd._link_ab802. The AB 869 side of this join is computed
live, not stored (see that same module docstring), so no schema change is
needed there.

Revision ID: 466ab34aa0aa
Revises: a77ddb56a5a1
Create Date: 2026-09-08 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa
import sqlmodel


revision = '466ab34aa0aa'
down_revision = 'a77ddb56a5a1'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('scaqmd_facilities',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('facility_id', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
    sa.Column('facility_name', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
    sa.Column('address', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
    sa.Column('city', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
    sa.Column('zip_code', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
    sa.Column('ab_2588', sa.Boolean(), nullable=False),
    sa.Column('meets_ctr_threshold', sa.Boolean(), nullable=False),
    sa.Column('core_ctr_facility', sa.Boolean(), nullable=False),
    sa.Column('ctr_phase_3', sa.Boolean(), nullable=False),
    sa.Column('rule_317_1', sa.Boolean(), nullable=False),
    sa.Column('in_territory', sa.Boolean(), nullable=False),
    sa.Column('source_url', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
    sa.Column('imported_at', sa.DateTime(), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('facility_id')
    )
    op.create_index(op.f('ix_scaqmd_facilities_facility_id'), 'scaqmd_facilities', ['facility_id'], unique=True)
    op.create_index(op.f('ix_scaqmd_facilities_city'), 'scaqmd_facilities', ['city'], unique=False)
    op.create_index(op.f('ix_scaqmd_facilities_in_territory'), 'scaqmd_facilities', ['in_territory'], unique=False)
    op.create_index(op.f('ix_scaqmd_facilities_imported_at'), 'scaqmd_facilities', ['imported_at'], unique=False)

    op.add_column('ab802_buildings', sa.Column('air_permit_facility_id', sqlmodel.sql.sqltypes.AutoString(), nullable=True))
    op.add_column('ab802_buildings', sa.Column('air_permit_match_method', sqlmodel.sql.sqltypes.AutoString(), nullable=True))
    op.create_index(op.f('ix_ab802_buildings_air_permit_facility_id'), 'ab802_buildings', ['air_permit_facility_id'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_ab802_buildings_air_permit_facility_id'), table_name='ab802_buildings')
    op.drop_column('ab802_buildings', 'air_permit_match_method')
    op.drop_column('ab802_buildings', 'air_permit_facility_id')

    op.drop_index(op.f('ix_scaqmd_facilities_imported_at'), table_name='scaqmd_facilities')
    op.drop_index(op.f('ix_scaqmd_facilities_in_territory'), table_name='scaqmd_facilities')
    op.drop_index(op.f('ix_scaqmd_facilities_city'), table_name='scaqmd_facilities')
    op.drop_index(op.f('ix_scaqmd_facilities_facility_id'), table_name='scaqmd_facilities')
    op.drop_table('scaqmd_facilities')
