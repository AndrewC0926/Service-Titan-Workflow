"""source_row_seen_diff_table

New table -- see app.models.SourceRowSeen and app/pipeline/diffs.py's module
docstring. Item 1 of docs/DAILY-BRIEF-DESIGN.md, built alone: the nightly
diff's own (source, natural_key) -> fingerprint memory for the five
Pipeline B tables with no integer id (hcai_projects, ab869_plans,
ab802_buildings, opsc_projects, scaqmd_facilities). No brief, Opportunity
table, or UI change lands with this migration.

Revision ID: abc952f1fcde
Revises: 2b348780d519
Create Date: 2026-09-09 10:42:19.491839
"""
from alembic import op
import sqlalchemy as sa
import sqlmodel


revision = 'abc952f1fcde'
down_revision = '2b348780d519'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('source_rows_seen',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('source', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
    sa.Column('natural_key', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
    sa.Column('fingerprint', sa.Text(), nullable=False),
    sa.Column('first_seen_at', sa.DateTime(), nullable=False),
    sa.Column('last_seen_at', sa.DateTime(), nullable=False),
    sa.Column('removed_at', sa.DateTime(), nullable=True),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('source', 'natural_key', name='uq_source_row_seen')
    )
    op.create_index(op.f('ix_source_rows_seen_source'), 'source_rows_seen', ['source'], unique=False)
    op.create_index(op.f('ix_source_rows_seen_natural_key'), 'source_rows_seen', ['natural_key'], unique=False)
    op.create_index(op.f('ix_source_rows_seen_first_seen_at'), 'source_rows_seen', ['first_seen_at'], unique=False)
    op.create_index(op.f('ix_source_rows_seen_last_seen_at'), 'source_rows_seen', ['last_seen_at'], unique=False)
    op.create_index(op.f('ix_source_rows_seen_removed_at'), 'source_rows_seen', ['removed_at'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_source_rows_seen_removed_at'), table_name='source_rows_seen')
    op.drop_index(op.f('ix_source_rows_seen_last_seen_at'), table_name='source_rows_seen')
    op.drop_index(op.f('ix_source_rows_seen_first_seen_at'), table_name='source_rows_seen')
    op.drop_index(op.f('ix_source_rows_seen_natural_key'), table_name='source_rows_seen')
    op.drop_index(op.f('ix_source_rows_seen_source'), table_name='source_rows_seen')
    op.drop_table('source_rows_seen')
