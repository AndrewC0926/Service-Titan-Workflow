"""scaqmd_facilities source column and composite unique key

Adds `source` ("aer_facilities_notified" or "carb" -- see
app.models.ScaqmdFacility's own docstring) so the same real facility can
have one row per source instead of a single merged row. Existing rows
(all from the AER XLSX so far) backfill to 'aer_facilities_notified' via
the column's own server_default. Replaces the single-column unique
constraint/index on facility_id with a composite one on
(facility_id, source), and a plain (non-unique) index on facility_id for
lookups across sources.

Revision ID: 3112b0365da2
Revises: 466ab34aa0aa
Create Date: 2026-09-08 12:46:06.946362
"""
from alembic import op
import sqlalchemy as sa
import sqlmodel


revision = '3112b0365da2'
down_revision = '466ab34aa0aa'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('scaqmd_facilities', sa.Column(
        'source', sqlmodel.sql.sqltypes.AutoString(), nullable=False,
        server_default='aer_facilities_notified'))
    op.create_index(op.f('ix_scaqmd_facilities_source'), 'scaqmd_facilities', ['source'], unique=False)

    op.drop_constraint('scaqmd_facilities_facility_id_key', 'scaqmd_facilities', type_='unique')
    op.drop_index(op.f('ix_scaqmd_facilities_facility_id'), table_name='scaqmd_facilities')
    op.create_index(op.f('ix_scaqmd_facilities_facility_id'), 'scaqmd_facilities', ['facility_id'], unique=False)
    op.create_unique_constraint('uq_scaqmd_facility_id_source', 'scaqmd_facilities', ['facility_id', 'source'])


def downgrade() -> None:
    op.drop_constraint('uq_scaqmd_facility_id_source', 'scaqmd_facilities', type_='unique')
    op.drop_index(op.f('ix_scaqmd_facilities_facility_id'), table_name='scaqmd_facilities')
    op.create_index(op.f('ix_scaqmd_facilities_facility_id'), 'scaqmd_facilities', ['facility_id'], unique=True)
    op.create_unique_constraint('scaqmd_facilities_facility_id_key', 'scaqmd_facilities', ['facility_id'])

    op.drop_index(op.f('ix_scaqmd_facilities_source'), table_name='scaqmd_facilities')
    op.drop_column('scaqmd_facilities', 'source')
