"""developer design team rollup

One new table: developer_design_team, a rollup of ProjectFirm + Project.developer
restricted to architect/mep_engineer/engineer_of_record, plus rep-entered manual
rows. See app.models.DeveloperDesignTeam and app.developer_team's module docstring.

Revision ID: 21978311fa2c
Revises: 943d8c5ea067
Create Date: 2026-09-06 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa
import sqlmodel


revision = '21978311fa2c'
down_revision = '943d8c5ea067'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('developer_design_team',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('developer', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
    sa.Column('developer_norm', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
    sa.Column('firm_id', sa.Integer(), nullable=False),
    sa.Column('role', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
    sa.Column('evidence_project_ids', sa.JSON(), nullable=False),
    sa.Column('source', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
    sa.Column('reason', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
    sa.Column('confirmed_by', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['firm_id'], ['firms.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('developer_norm', 'firm_id', 'role', name='uq_developer_design_team')
    )
    op.create_index(op.f('ix_developer_design_team_developer'), 'developer_design_team', ['developer'], unique=False)
    op.create_index(op.f('ix_developer_design_team_developer_norm'), 'developer_design_team', ['developer_norm'], unique=False)
    op.create_index(op.f('ix_developer_design_team_firm_id'), 'developer_design_team', ['firm_id'], unique=False)
    op.create_index(op.f('ix_developer_design_team_role'), 'developer_design_team', ['role'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_developer_design_team_role'), table_name='developer_design_team')
    op.drop_index(op.f('ix_developer_design_team_firm_id'), table_name='developer_design_team')
    op.drop_index(op.f('ix_developer_design_team_developer_norm'), table_name='developer_design_team')
    op.drop_index(op.f('ix_developer_design_team_developer'), table_name='developer_design_team')
    op.drop_table('developer_design_team')
