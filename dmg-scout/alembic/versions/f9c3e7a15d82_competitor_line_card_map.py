"""competitor line card map

Adds rep_firms and competitor_lines -- the competing side of the line
card, see app/competitors.py for the hand-researched dataset and
app/models.py's RepFirm/CompetitorLine docstrings for the schema.

Revision ID: f9c3e7a15d82
Revises: e8f1b4d76a29
Create Date: 2026-08-16 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = 'f9c3e7a15d82'
down_revision = 'e8f1b4d76a29'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'rep_firms',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('name', sa.String(), nullable=False),
        sa.Column('website', sa.String(), nullable=True),
        sa.Column('territory_note', sa.String(), nullable=True),
    )
    op.create_index('ix_rep_firms_name', 'rep_firms', ['name'], unique=True)

    op.create_table(
        'competitor_lines',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('manufacturer', sa.String(), nullable=False),
        sa.Column('building_role', sa.String(), nullable=True),
        sa.Column('channel', sa.String(), nullable=False, server_default='rep_firm'),
        sa.Column('rep_firm_id', sa.Integer(), sa.ForeignKey('rep_firms.id'), nullable=True),
        sa.Column('status', sa.String(), nullable=False, server_default='confirmed'),
        sa.Column('conflict_note', sa.Text(), nullable=True),
        sa.Column('source_url', sa.String(), nullable=False),
        sa.Column('retrieved_at', sa.DateTime(), nullable=False),
    )
    op.create_index('ix_competitor_lines_manufacturer', 'competitor_lines', ['manufacturer'])
    op.create_index('ix_competitor_lines_building_role', 'competitor_lines', ['building_role'])
    op.create_index('ix_competitor_lines_channel', 'competitor_lines', ['channel'])
    op.create_index('ix_competitor_lines_rep_firm_id', 'competitor_lines', ['rep_firm_id'])
    op.create_index('ix_competitor_lines_status', 'competitor_lines', ['status'])


def downgrade() -> None:
    op.drop_table('competitor_lines')
    op.drop_table('rep_firms')
