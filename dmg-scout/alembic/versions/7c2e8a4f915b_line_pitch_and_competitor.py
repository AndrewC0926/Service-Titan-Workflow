"""line_pitch_and_competitor

Elevator-pitch training reference: line_pitches (one row per ProductLine)
and line_competitors (one row per product_line_id + competitor_name pair).
See app.models.LinePitch/LineCompetitor and app/pipeline/line_pitch.py's
module docstring for the full propose-never-assert generation method.

Revision ID: 7c2e8a4f915b
Revises: 3d9b6f2e1a47
Create Date: 2026-09-03 00:00:00.000000
"""
import sqlalchemy as sa
import sqlmodel
from sqlalchemy.dialects import postgresql

from alembic import op

revision = '7c2e8a4f915b'
down_revision = '3d9b6f2e1a47'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'line_pitches',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('product_line_id', sa.Integer(), nullable=False),
        sa.Column('what_it_is', sa.Text(), nullable=True),
        sa.Column('where_it_fits', sa.Text(), nullable=True),
        sa.Column('typical_project_types', sa.Text(), nullable=True),
        sa.Column('elevator_pitch', sa.Text(), nullable=True),
        sa.Column('differentiators', postgresql.JSON(astext_type=sa.Text()), nullable=False),
        sa.Column('engineer_questions', postgresql.JSON(astext_type=sa.Text()), nullable=False),
        sa.Column('review_status', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('reviewed_by', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column('reviewed_at', sa.DateTime(), nullable=True),
        sa.Column('source_url', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column('source_fetch_status', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column('grounded_claim_count', sa.Integer(), nullable=False),
        sa.Column('dropped_claim_count', sa.Integer(), nullable=False),
        sa.Column('model', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column('generated_at', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['product_line_id'], ['product_lines.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('product_line_id', name='uq_line_pitch_product_line'),
    )
    op.create_index(op.f('ix_line_pitches_product_line_id'), 'line_pitches', ['product_line_id'])
    op.create_index(op.f('ix_line_pitches_review_status'), 'line_pitches', ['review_status'])

    op.create_table(
        'line_competitors',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('product_line_id', sa.Integer(), nullable=False),
        sa.Column('competitor_name', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('why_we_lose', sa.Text(), nullable=True),
        sa.Column('why_we_win', sa.Text(), nullable=True),
        sa.Column('evidence_url', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column('review_status', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('reviewed_by', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column('reviewed_at', sa.DateTime(), nullable=True),
        sa.Column('model', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column('generated_at', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['product_line_id'], ['product_lines.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('product_line_id', 'competitor_name', name='uq_line_competitor'),
    )
    op.create_index(op.f('ix_line_competitors_product_line_id'), 'line_competitors', ['product_line_id'])
    op.create_index(op.f('ix_line_competitors_competitor_name'), 'line_competitors', ['competitor_name'])
    op.create_index(op.f('ix_line_competitors_review_status'), 'line_competitors', ['review_status'])


def downgrade() -> None:
    op.drop_index(op.f('ix_line_competitors_review_status'), table_name='line_competitors')
    op.drop_index(op.f('ix_line_competitors_competitor_name'), table_name='line_competitors')
    op.drop_index(op.f('ix_line_competitors_product_line_id'), table_name='line_competitors')
    op.drop_table('line_competitors')

    op.drop_index(op.f('ix_line_pitches_review_status'), table_name='line_pitches')
    op.drop_index(op.f('ix_line_pitches_product_line_id'), table_name='line_pitches')
    op.drop_table('line_pitches')
