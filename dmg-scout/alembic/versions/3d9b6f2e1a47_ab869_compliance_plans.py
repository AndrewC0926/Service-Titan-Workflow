"""ab869_compliance_plans

AB 869 seismic compliance plan roster: three tables, matching three real
grains, not flattened -- ab869_plans (one row per facility's filing),
ab869_buildings (one row per building in that filing's Compliance Method
table), ab869_milestones (many rows per building). See app.models.Ab869Plan/
Ab869Building/Ab869Milestone and app.pipeline.ab869's module docstring for
the full parsing/access investigation this is built on.

Revision ID: 3d9b6f2e1a47
Revises: 81f4860b6471
Create Date: 2026-09-02 00:00:00.000000
"""
import sqlalchemy as sa
import sqlmodel

from alembic import op

revision = '3d9b6f2e1a47'
down_revision = '81f4860b6471'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'ab869_plans',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('perm_id', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('county', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column('plan_status', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column('plan_status_source', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column('plan_status_paragraph', sa.Text(), nullable=True),
        sa.Column('plan_status_paragraph_reason', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column('delay_text', sa.Text(), nullable=True),
        sa.Column('delay_text_reason', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column('delay_requested', sa.Boolean(), nullable=True),
        sa.Column('ab869_letter_url', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column('owner_name', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column('owner_type', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column('manager_name', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column('manager_type', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column('financially_responsible_party', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column('other_financial_contact', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column('other_financial_contact_reason', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column('source_pdf_path', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('source_pdf_hash', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('crosstab_path', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column('imported_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('perm_id', name='uq_ab869_plan_perm_id'),
    )
    op.create_index(op.f('ix_ab869_plans_perm_id'), 'ab869_plans', ['perm_id'])
    op.create_index(op.f('ix_ab869_plans_county'), 'ab869_plans', ['county'])
    op.create_index(op.f('ix_ab869_plans_plan_status'), 'ab869_plans', ['plan_status'])
    op.create_index(op.f('ix_ab869_plans_delay_requested'), 'ab869_plans', ['delay_requested'])
    op.create_index(op.f('ix_ab869_plans_financially_responsible_party'),
                    'ab869_plans', ['financially_responsible_party'])
    op.create_index(op.f('ix_ab869_plans_source_pdf_hash'), 'ab869_plans', ['source_pdf_hash'])
    op.create_index(op.f('ix_ab869_plans_imported_at'), 'ab869_plans', ['imported_at'])

    op.create_table(
        'ab869_buildings',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('perm_id', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('building_nbr', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('compliance_type', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column('narrative', sa.Text(), nullable=True),
        sa.Column('narrative_reason', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column('hcai_comment', sa.Text(), nullable=True),
        sa.Column('hcai_comment_reason', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column('has_missed_milestone', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('imported_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('perm_id', 'building_nbr', name='uq_ab869_building'),
    )
    op.create_index(op.f('ix_ab869_buildings_perm_id'), 'ab869_buildings', ['perm_id'])
    op.create_index(op.f('ix_ab869_buildings_building_nbr'), 'ab869_buildings', ['building_nbr'])
    op.create_index(op.f('ix_ab869_buildings_compliance_type'), 'ab869_buildings', ['compliance_type'])
    op.create_index(op.f('ix_ab869_buildings_has_missed_milestone'), 'ab869_buildings', ['has_missed_milestone'])
    op.create_index(op.f('ix_ab869_buildings_imported_at'), 'ab869_buildings', ['imported_at'])

    op.create_table(
        'ab869_milestones',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('perm_id', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('building_nbr', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('milestone_type', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column('milestone_type_reason', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('description_reason', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column('completion_date_text', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column('completion_date', sa.DateTime(), nullable=True),
        sa.Column('completion_date_reason', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column('hcai_comment', sa.Text(), nullable=True),
        sa.Column('hcai_comment_reason', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column('met_by_hcai', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column('met_by_hcai_reason', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column('imported_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_ab869_milestones_perm_id'), 'ab869_milestones', ['perm_id'])
    op.create_index(op.f('ix_ab869_milestones_building_nbr'), 'ab869_milestones', ['building_nbr'])
    op.create_index(op.f('ix_ab869_milestones_milestone_type'), 'ab869_milestones', ['milestone_type'])
    op.create_index(op.f('ix_ab869_milestones_completion_date'), 'ab869_milestones', ['completion_date'])
    op.create_index(op.f('ix_ab869_milestones_imported_at'), 'ab869_milestones', ['imported_at'])


def downgrade() -> None:
    op.drop_index(op.f('ix_ab869_milestones_imported_at'), table_name='ab869_milestones')
    op.drop_index(op.f('ix_ab869_milestones_completion_date'), table_name='ab869_milestones')
    op.drop_index(op.f('ix_ab869_milestones_milestone_type'), table_name='ab869_milestones')
    op.drop_index(op.f('ix_ab869_milestones_building_nbr'), table_name='ab869_milestones')
    op.drop_index(op.f('ix_ab869_milestones_perm_id'), table_name='ab869_milestones')
    op.drop_table('ab869_milestones')

    op.drop_index(op.f('ix_ab869_buildings_imported_at'), table_name='ab869_buildings')
    op.drop_index(op.f('ix_ab869_buildings_has_missed_milestone'), table_name='ab869_buildings')
    op.drop_index(op.f('ix_ab869_buildings_compliance_type'), table_name='ab869_buildings')
    op.drop_index(op.f('ix_ab869_buildings_building_nbr'), table_name='ab869_buildings')
    op.drop_index(op.f('ix_ab869_buildings_perm_id'), table_name='ab869_buildings')
    op.drop_table('ab869_buildings')

    op.drop_index(op.f('ix_ab869_plans_imported_at'), table_name='ab869_plans')
    op.drop_index(op.f('ix_ab869_plans_source_pdf_hash'), table_name='ab869_plans')
    op.drop_index(op.f('ix_ab869_plans_financially_responsible_party'), table_name='ab869_plans')
    op.drop_index(op.f('ix_ab869_plans_delay_requested'), table_name='ab869_plans')
    op.drop_index(op.f('ix_ab869_plans_plan_status'), table_name='ab869_plans')
    op.drop_index(op.f('ix_ab869_plans_county'), table_name='ab869_plans')
    op.drop_index(op.f('ix_ab869_plans_perm_id'), table_name='ab869_plans')
    op.drop_table('ab869_plans')
