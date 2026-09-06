"""manual corrections and pinned field conflicts

Two new tables for the RATCHET OVERRIDE (see app/pipeline/corrections.py's
module docstring and the RATCHET BUG diagnosis, both 2026-09-06): the one
path allowed to move Project.stage backward, shrink mw_it/mw_total, or
replace an already-stated delivery_method, and the review queue for a
signal that arrives after a correction and would still move the pinned
field.

Nothing here touches any existing table -- autogenerate also proposed
several unrelated changes against pre-existing drift (a stage_observations.
stage VARCHAR->Enum conversion, two covering-index/unique-constraint
tweaks on contractors/retrofit_buildings/selection_tools) that predate
this change and are deliberately NOT included here; a schema change to a
live, populated table needs its own reviewed migration, not a rider on an
unrelated one.

Revision ID: 943d8c5ea067
Revises: 9d2f6a1c4e83
Create Date: 2026-09-06 10:51:41.250189
"""
from alembic import op
import sqlalchemy as sa
import sqlmodel


revision = '943d8c5ea067'
down_revision = '9d2f6a1c4e83'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('manual_corrections',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('project_id', sa.Integer(), nullable=False),
    sa.Column('field', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
    sa.Column('old_value', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
    sa.Column('new_value', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
    sa.Column('reason', sa.Text(), nullable=False),
    sa.Column('corrected_by', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
    sa.Column('corrected_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['project_id'], ['projects.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_manual_corrections_corrected_at'), 'manual_corrections', ['corrected_at'], unique=False)
    op.create_index(op.f('ix_manual_corrections_field'), 'manual_corrections', ['field'], unique=False)
    op.create_index('ix_manual_corrections_project_field_at', 'manual_corrections', ['project_id', 'field', 'corrected_at'], unique=False)
    op.create_index(op.f('ix_manual_corrections_project_id'), 'manual_corrections', ['project_id'], unique=False)
    op.create_table('pinned_field_conflicts',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('project_id', sa.Integer(), nullable=False),
    sa.Column('field', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
    sa.Column('signal_id', sa.Integer(), nullable=False),
    sa.Column('correction_id', sa.Integer(), nullable=False),
    sa.Column('pinned_value', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
    sa.Column('candidate_value', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
    sa.Column('status', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('resolved_at', sa.DateTime(), nullable=True),
    sa.Column('resolved_by', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
    sa.ForeignKeyConstraint(['correction_id'], ['manual_corrections.id'], ),
    sa.ForeignKeyConstraint(['project_id'], ['projects.id'], ),
    sa.ForeignKeyConstraint(['signal_id'], ['signals.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_pinned_field_conflicts_correction_id'), 'pinned_field_conflicts', ['correction_id'], unique=False)
    op.create_index(op.f('ix_pinned_field_conflicts_field'), 'pinned_field_conflicts', ['field'], unique=False)
    op.create_index(op.f('ix_pinned_field_conflicts_project_id'), 'pinned_field_conflicts', ['project_id'], unique=False)
    op.create_index(op.f('ix_pinned_field_conflicts_signal_id'), 'pinned_field_conflicts', ['signal_id'], unique=False)
    op.create_index(op.f('ix_pinned_field_conflicts_status'), 'pinned_field_conflicts', ['status'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_pinned_field_conflicts_status'), table_name='pinned_field_conflicts')
    op.drop_index(op.f('ix_pinned_field_conflicts_signal_id'), table_name='pinned_field_conflicts')
    op.drop_index(op.f('ix_pinned_field_conflicts_project_id'), table_name='pinned_field_conflicts')
    op.drop_index(op.f('ix_pinned_field_conflicts_field'), table_name='pinned_field_conflicts')
    op.drop_index(op.f('ix_pinned_field_conflicts_correction_id'), table_name='pinned_field_conflicts')
    op.drop_table('pinned_field_conflicts')
    op.drop_index(op.f('ix_manual_corrections_project_id'), table_name='manual_corrections')
    op.drop_index('ix_manual_corrections_project_field_at', table_name='manual_corrections')
    op.drop_index(op.f('ix_manual_corrections_field'), table_name='manual_corrections')
    op.drop_index(op.f('ix_manual_corrections_corrected_at'), table_name='manual_corrections')
    op.drop_table('manual_corrections')
