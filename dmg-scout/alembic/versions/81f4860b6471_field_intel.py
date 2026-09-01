"""field_intel

Human-sourced project intelligence -- a GC/engineer/owner's own word, in
conversation, before any of it is a public document. Deliberately a new,
separate table, not a column on `projects`: see app.models.FieldIntel's
own docstring for why every existing Project invariant (project_evidence's
signal requirement, resolve's auto-merge, duplicates' dedup, brief's
signal-only timeline) assumes a document chain this table never has.

`stage` reuses the existing native `stage` enum type (projects.stage) --
create_type=False, since CREATE TYPE would fail against a type this
schema already has.

Revision ID: 81f4860b6471
Revises: 7204be8d14de
Create Date: 2026-08-25 00:00:00.000000
"""
import sqlalchemy as sa
import sqlmodel
from sqlalchemy.dialects import postgresql

from alembic import op

revision = '81f4860b6471'
down_revision = '7204be8d14de'
branch_labels = None
depends_on = None

STAGE_ENUM = postgresql.ENUM(
    'concept', 'entitlement', 'design', 'permitting', 'procurement', 'construction',
    'operating', 'unknown', name='stage', create_type=False,
)


def upgrade() -> None:
    op.create_table(
        'field_intel',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('reported_by', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('reported_at', sa.DateTime(), nullable=False),
        sa.Column('source_notes', sa.Text(), nullable=False),
        sa.Column('owner', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column('location', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column('size_scope', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column('stage', STAGE_ENUM, nullable=False),
        sa.Column('expected_timing', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column('engineer_name', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column('engineer_firm_id', sa.Integer(), nullable=True),
        sa.Column('engineer_account_id', sa.Integer(), nullable=True),
        sa.Column('mech_contractor_name', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column('mech_contractor_firm_id', sa.Integer(), nullable=True),
        sa.Column('mech_contractor_account_id', sa.Integer(), nullable=True),
        sa.Column('confirmed_project_id', sa.Integer(), nullable=True),
        sa.Column('confirmed_at', sa.DateTime(), nullable=True),
        sa.Column('confirmed_by', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column('status', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['engineer_firm_id'], ['firms.id']),
        sa.ForeignKeyConstraint(['engineer_account_id'], ['accounts.id']),
        sa.ForeignKeyConstraint(['mech_contractor_firm_id'], ['firms.id']),
        sa.ForeignKeyConstraint(['mech_contractor_account_id'], ['accounts.id']),
        sa.ForeignKeyConstraint(['confirmed_project_id'], ['projects.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_field_intel_reported_by'), 'field_intel', ['reported_by'])
    op.create_index(op.f('ix_field_intel_reported_at'), 'field_intel', ['reported_at'])
    op.create_index(op.f('ix_field_intel_confirmed_project_id'), 'field_intel', ['confirmed_project_id'])
    op.create_index(op.f('ix_field_intel_status'), 'field_intel', ['status'])
    op.create_index(op.f('ix_field_intel_created_at'), 'field_intel', ['created_at'])


def downgrade() -> None:
    op.drop_index(op.f('ix_field_intel_created_at'), table_name='field_intel')
    op.drop_index(op.f('ix_field_intel_status'), table_name='field_intel')
    op.drop_index(op.f('ix_field_intel_confirmed_project_id'), table_name='field_intel')
    op.drop_index(op.f('ix_field_intel_reported_at'), table_name='field_intel')
    op.drop_index(op.f('ix_field_intel_reported_by'), table_name='field_intel')
    op.drop_table('field_intel')
