"""hcai county activity

One table: hcai_county_activity. A county-level AGGREGATE healthcare
construction layer, same shape as iepr_forward_loads -- imported by hand
from a downloaded CHHS Open Data CSV (see app/pipeline/hcai.py), never a
project-level source.

Revision ID: 2a79a9306050
Revises: bf8c57d735e2
Create Date: 2026-08-08 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa
import sqlmodel


revision = '2a79a9306050'
down_revision = 'bf8c57d735e2'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'hcai_county_activity',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('county', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('state', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('status', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('total_cost', sa.Float(), nullable=True),
        sa.Column('project_count', sa.Integer(), nullable=True),
        sa.Column('snapshot_date', sa.DateTime(), nullable=False),
        sa.Column('source_url', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('imported_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('county', 'status', name='uq_hcai_county_activity'),
    )
    op.create_index(op.f('ix_hcai_county_activity_county'), 'hcai_county_activity', ['county'])
    op.create_index(op.f('ix_hcai_county_activity_state'), 'hcai_county_activity', ['state'])
    op.create_index(op.f('ix_hcai_county_activity_status'), 'hcai_county_activity', ['status'])
    op.create_index(op.f('ix_hcai_county_activity_snapshot_date'), 'hcai_county_activity', ['snapshot_date'])
    op.create_index(op.f('ix_hcai_county_activity_imported_at'), 'hcai_county_activity', ['imported_at'])


def downgrade() -> None:
    op.drop_table('hcai_county_activity')
