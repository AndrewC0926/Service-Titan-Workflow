"""stage observations

Append-only ledger of every stage a project has ever been observed at, with
its date and source signal — what Project.stage (a single forward-only
column) always lacked. See StageObservation's docstring in app/models.py.

Revision ID: a1c3f7e92b58
Revises: fc46871532b3
Create Date: 2026-08-07 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa
import sqlmodel


revision = 'a1c3f7e92b58'
down_revision = 'fc46871532b3'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('stage_observations',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('project_id', sa.Integer(), nullable=False),
    sa.Column('stage', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
    sa.Column('observed_at', sa.DateTime(), nullable=False),
    sa.Column('from_event', sa.Boolean(), nullable=False),
    sa.Column('signal_id', sa.Integer(), nullable=True),
    sa.Column('source', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['project_id'], ['projects.id'], ),
    sa.ForeignKeyConstraint(['signal_id'], ['signals.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('signal_id', name='uq_stage_observation_signal')
    )
    op.create_index(op.f('ix_stage_observations_project_id'), 'stage_observations', ['project_id'], unique=False)
    op.create_index(op.f('ix_stage_observations_stage'), 'stage_observations', ['stage'], unique=False)
    op.create_index(op.f('ix_stage_observations_observed_at'), 'stage_observations', ['observed_at'], unique=False)
    op.create_index(op.f('ix_stage_observations_signal_id'), 'stage_observations', ['signal_id'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_stage_observations_signal_id'), table_name='stage_observations')
    op.drop_index(op.f('ix_stage_observations_observed_at'), table_name='stage_observations')
    op.drop_index(op.f('ix_stage_observations_stage'), table_name='stage_observations')
    op.drop_index(op.f('ix_stage_observations_project_id'), table_name='stage_observations')
    op.drop_table('stage_observations')
