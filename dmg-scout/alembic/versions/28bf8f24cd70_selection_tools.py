"""selection_tools registry

One row per ProductLine (70 total): which selection software a rep uses to
spec that line. firm is deliberately NOT a column -- read via join to
product_lines.firm, the existing single source of truth. See
app/models.py's SelectionTool docstring and app.accounts.seed_selection_tools.

Revision ID: 28bf8f24cd70
Revises: 2f7cdd7ffbb6
Create Date: 2026-08-11 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = '28bf8f24cd70'
down_revision = '2f7cdd7ffbb6'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'selection_tools',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('product_line_id', sa.Integer(), sa.ForeignKey('product_lines.id'), nullable=False),
        sa.Column('tool_name', sa.String(), nullable=True),
        sa.Column('vendor_url', sa.String(), nullable=True),
        sa.Column('access_level', sa.String(), nullable=True),
        sa.Column('what_it_outputs', sa.String(), nullable=True),
        sa.Column('produces_submittal_docs', sa.Boolean(), nullable=True),
        sa.Column('verified_by', sa.String(), nullable=True),
        sa.Column('verified_date', sa.DateTime(), nullable=True),
        sa.Column('verification_status', sa.String(), nullable=False, server_default='unchecked'),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
    )
    op.create_index('ix_selection_tools_product_line_id', 'selection_tools', ['product_line_id'])
    op.create_unique_constraint('uq_selection_tool_line', 'selection_tools', ['product_line_id'])
    op.create_index('ix_selection_tools_verification_status', 'selection_tools', ['verification_status'])


def downgrade() -> None:
    op.drop_index('ix_selection_tools_verification_status', table_name='selection_tools')
    op.drop_constraint('uq_selection_tool_line', 'selection_tools', type_='unique')
    op.drop_index('ix_selection_tools_product_line_id', table_name='selection_tools')
    op.drop_table('selection_tools')
