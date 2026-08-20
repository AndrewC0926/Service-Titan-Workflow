"""signal grounding version stamp

Revision ID: 2ca46f384f94
Revises: f61c8d3e9a72
Create Date: 2026-08-19 18:49:44.310528
"""
from alembic import op
import sqlalchemy as sa
import sqlmodel


revision = '2ca46f384f94'
down_revision = 'f61c8d3e9a72'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('signals', sa.Column('grounding_version', sa.Integer(), nullable=True))
    op.create_index(op.f('ix_signals_grounding_version'), 'signals', ['grounding_version'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_signals_grounding_version'), table_name='signals')
    op.drop_column('signals', 'grounding_version')
