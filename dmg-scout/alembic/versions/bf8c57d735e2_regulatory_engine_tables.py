"""regulatory engine tables

Two new tables for Phase 6: equipment_permits (mechanical permit records,
install-year evidence for SB 1206's R-410A inference, plus work-description-
mined equipment count/tonnage — see app/pipeline/permits.py) and
assessor_candidates (parcel-level CANDIDATES for a regulatory trigger by use
code or building size, never a confirmed filer list — see
app/pipeline/assessor.py). Neither touches the project pipeline's tables.

Revision ID: bf8c57d735e2
Revises: 45969785f49a
Create Date: 2026-08-08 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa
import sqlmodel


revision = 'bf8c57d735e2'
down_revision = '45969785f49a'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'equipment_permits',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('source', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('permit_nbr', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('apn', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column('address', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column('county', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('state', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('permit_type', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column('permit_sub_type', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column('status_desc', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column('issue_date', sa.DateTime(), nullable=True),
        sa.Column('work_desc', sa.Text(), nullable=False),
        sa.Column('equipment_count', sa.Integer(), nullable=True),
        sa.Column('tons_each', sa.Float(), nullable=True),
        sa.Column('inferred_refrigerant', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column('sb1206_trigger_status', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column('sb1206_detail', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column('source_url', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('imported_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('source', 'permit_nbr', name='uq_equipment_permit'),
    )
    op.create_index(op.f('ix_equipment_permits_source'), 'equipment_permits', ['source'])
    op.create_index(op.f('ix_equipment_permits_permit_nbr'), 'equipment_permits', ['permit_nbr'])
    op.create_index(op.f('ix_equipment_permits_apn'), 'equipment_permits', ['apn'])
    op.create_index(op.f('ix_equipment_permits_county'), 'equipment_permits', ['county'])
    op.create_index(op.f('ix_equipment_permits_state'), 'equipment_permits', ['state'])
    op.create_index(op.f('ix_equipment_permits_issue_date'), 'equipment_permits', ['issue_date'])
    op.create_index(op.f('ix_equipment_permits_inferred_refrigerant'), 'equipment_permits', ['inferred_refrigerant'])
    op.create_index(op.f('ix_equipment_permits_sb1206_trigger_status'), 'equipment_permits', ['sb1206_trigger_status'])
    op.create_index(op.f('ix_equipment_permits_imported_at'), 'equipment_permits', ['imported_at'])

    op.create_table(
        'assessor_candidates',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('source', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('ain', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('trigger_key', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('use_code', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column('use_desc', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column('address', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column('county', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('state', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('year_built', sa.Integer(), nullable=True),
        sa.Column('sqft', sa.Float(), nullable=True),
        sa.Column('source_url', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('imported_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('source', 'ain', 'trigger_key', name='uq_assessor_candidate'),
    )
    op.create_index(op.f('ix_assessor_candidates_source'), 'assessor_candidates', ['source'])
    op.create_index(op.f('ix_assessor_candidates_ain'), 'assessor_candidates', ['ain'])
    op.create_index(op.f('ix_assessor_candidates_trigger_key'), 'assessor_candidates', ['trigger_key'])
    op.create_index(op.f('ix_assessor_candidates_county'), 'assessor_candidates', ['county'])
    op.create_index(op.f('ix_assessor_candidates_state'), 'assessor_candidates', ['state'])
    op.create_index(op.f('ix_assessor_candidates_imported_at'), 'assessor_candidates', ['imported_at'])


def downgrade() -> None:
    op.drop_table('assessor_candidates')
    op.drop_table('equipment_permits')
