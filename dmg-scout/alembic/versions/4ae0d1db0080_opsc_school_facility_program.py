"""OPSC School Facility Program (opsc_projects, opsc_workload)

Two new tables -- see app.models.OpscProject/OpscWorkload and
app.pipeline.opsc's module docstring for the full design (bulk-CSV-only
compliance, monthly full-replace, the 18-month/status-changed Signal gate,
and the SAB workload-list PDF parsing with its own measured success rate).

Revision ID: 4ae0d1db0080
Revises: d752ba32526f
Create Date: 2026-09-08 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa
import sqlmodel


revision = '4ae0d1db0080'
down_revision = 'd752ba32526f'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('opsc_projects',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('county', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
    sa.Column('district', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
    sa.Column('school_name', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
    sa.Column('program', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
    sa.Column('application_number', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
    sa.Column('applicant', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
    sa.Column('preliminary_grant_application', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
    sa.Column('full_grant_application', sa.Float(), nullable=True),
    sa.Column('site_and_design_application', sa.Float(), nullable=True),
    sa.Column('site_only_application', sa.Float(), nullable=True),
    sa.Column('design_only_application', sa.Float(), nullable=True),
    sa.Column('environmental_hardship_application', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
    sa.Column('reduced_to_costs_incurred', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
    sa.Column('number_of_elementary_school_pupil_grants_requested', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
    sa.Column('number_of_middle_school_pupil_grants_requested', sa.Float(), nullable=True),
    sa.Column('number_of_high_school_pupil_grants_requested', sa.Float(), nullable=True),
    sa.Column('number_of_non_severe_school_pupil_grants_requested', sa.Float(), nullable=True),
    sa.Column('number_of_severe_school_pupil_grants_requested', sa.Float(), nullable=True),
    sa.Column('grade_level_of_project', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
    sa.Column('state_share_of_funding', sa.Float(), nullable=True),
    sa.Column('site_acquisition', sa.Float(), nullable=True),
    sa.Column('financial_hardship', sa.Float(), nullable=True),
    sa.Column('csfa_lease_amount', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
    sa.Column('ctefp_loan_amount', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
    sa.Column('type_of_joint_use_facility', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
    sa.Column('type_of_joint_use_partner', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
    sa.Column('industry_sector', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
    sa.Column('portables_replaced', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
    sa.Column('last_sab_date', sa.DateTime(), nullable=True),
    sa.Column('status', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
    sa.Column('in_territory', sa.Boolean(), nullable=False),
    sa.Column('source_url', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
    sa.Column('imported_at', sa.DateTime(), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('application_number')
    )
    op.create_index(op.f('ix_opsc_projects_county'), 'opsc_projects', ['county'], unique=False)
    op.create_index(op.f('ix_opsc_projects_district'), 'opsc_projects', ['district'], unique=False)
    op.create_index(op.f('ix_opsc_projects_program'), 'opsc_projects', ['program'], unique=False)
    op.create_index(op.f('ix_opsc_projects_application_number'), 'opsc_projects', ['application_number'], unique=False)
    op.create_index(op.f('ix_opsc_projects_last_sab_date'), 'opsc_projects', ['last_sab_date'], unique=False)
    op.create_index(op.f('ix_opsc_projects_status'), 'opsc_projects', ['status'], unique=False)
    op.create_index(op.f('ix_opsc_projects_in_territory'), 'opsc_projects', ['in_territory'], unique=False)
    op.create_index(op.f('ix_opsc_projects_imported_at'), 'opsc_projects', ['imported_at'], unique=False)

    op.create_table('opsc_workload',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('program', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
    sa.Column('district', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
    sa.Column('school_name', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
    sa.Column('application_number', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
    sa.Column('label', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
    sa.Column('raw_row_text', sa.Text(), nullable=False),
    sa.Column('source_url', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
    sa.Column('imported_at', sa.DateTime(), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_opsc_workload_program'), 'opsc_workload', ['program'], unique=False)
    op.create_index(op.f('ix_opsc_workload_application_number'), 'opsc_workload', ['application_number'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_opsc_workload_application_number'), table_name='opsc_workload')
    op.drop_index(op.f('ix_opsc_workload_program'), table_name='opsc_workload')
    op.drop_table('opsc_workload')

    op.drop_index(op.f('ix_opsc_projects_imported_at'), table_name='opsc_projects')
    op.drop_index(op.f('ix_opsc_projects_in_territory'), table_name='opsc_projects')
    op.drop_index(op.f('ix_opsc_projects_status'), table_name='opsc_projects')
    op.drop_index(op.f('ix_opsc_projects_last_sab_date'), table_name='opsc_projects')
    op.drop_index(op.f('ix_opsc_projects_application_number'), table_name='opsc_projects')
    op.drop_index(op.f('ix_opsc_projects_program'), table_name='opsc_projects')
    op.drop_index(op.f('ix_opsc_projects_district'), table_name='opsc_projects')
    op.drop_index(op.f('ix_opsc_projects_county'), table_name='opsc_projects')
    op.drop_table('opsc_projects')
