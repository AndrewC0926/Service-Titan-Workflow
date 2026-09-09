"""HCAI Facilities Development Division projects (hcai_projects)

New table -- see app.models.HcaiProject and app/pipeline/hcai_projects.py's
module docstring for the full design (record_no as the primary key,
idempotent upsert, the 23-value Status-to-stage collapse, the regex-only
is_mechanical flag, report_date carried per row).

Revision ID: 2b348780d519
Revises: ef1fc6ddf065
Create Date: 2026-09-09 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa
import sqlmodel


revision = '2b348780d519'
down_revision = 'ef1fc6ddf065'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('hcai_projects',
    sa.Column('record_no', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
    sa.Column('parent_no', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
    sa.Column('facility_id', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
    sa.Column('facility_name', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
    sa.Column('facility_address', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
    sa.Column('county', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
    sa.Column('scope_text', sa.Text(), nullable=False),
    sa.Column('date_in', sa.DateTime(), nullable=True),
    sa.Column('cost_est', sa.Float(), nullable=True),
    sa.Column('pct_complete', sa.Float(), nullable=True),
    sa.Column('status_raw', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
    sa.Column('stage', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
    sa.Column('is_mechanical', sa.Boolean(), nullable=False),
    sa.Column('report_date', sa.DateTime(), nullable=False),
    sa.Column('imported_at', sa.DateTime(), nullable=False),
    sa.PrimaryKeyConstraint('record_no')
    )
    op.create_index(op.f('ix_hcai_projects_parent_no'), 'hcai_projects', ['parent_no'], unique=False)
    op.create_index(op.f('ix_hcai_projects_facility_id'), 'hcai_projects', ['facility_id'], unique=False)
    op.create_index(op.f('ix_hcai_projects_county'), 'hcai_projects', ['county'], unique=False)
    op.create_index(op.f('ix_hcai_projects_status_raw'), 'hcai_projects', ['status_raw'], unique=False)
    op.create_index(op.f('ix_hcai_projects_stage'), 'hcai_projects', ['stage'], unique=False)
    op.create_index(op.f('ix_hcai_projects_is_mechanical'), 'hcai_projects', ['is_mechanical'], unique=False)
    op.create_index(op.f('ix_hcai_projects_report_date'), 'hcai_projects', ['report_date'], unique=False)
    op.create_index(op.f('ix_hcai_projects_imported_at'), 'hcai_projects', ['imported_at'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_hcai_projects_imported_at'), table_name='hcai_projects')
    op.drop_index(op.f('ix_hcai_projects_report_date'), table_name='hcai_projects')
    op.drop_index(op.f('ix_hcai_projects_is_mechanical'), table_name='hcai_projects')
    op.drop_index(op.f('ix_hcai_projects_stage'), table_name='hcai_projects')
    op.drop_index(op.f('ix_hcai_projects_status_raw'), table_name='hcai_projects')
    op.drop_index(op.f('ix_hcai_projects_county'), table_name='hcai_projects')
    op.drop_index(op.f('ix_hcai_projects_facility_id'), table_name='hcai_projects')
    op.drop_index(op.f('ix_hcai_projects_parent_no'), table_name='hcai_projects')
    op.drop_table('hcai_projects')
