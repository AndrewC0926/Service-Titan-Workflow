"""product line branches

Adds product_line_branches -- which DMG/ToroAire branch actually carries
a given line. Absence of a row means unknown; see app/models.py's
ProductLineBranch docstring.

Revision ID: a3f7e91c5d02
Revises: 2ca46f384f94
Create Date: 2026-08-19 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = 'a3f7e91c5d02'
down_revision = '2ca46f384f94'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'product_line_branches',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('product_line_id', sa.Integer(), sa.ForeignKey('product_lines.id'), nullable=False),
        sa.Column('branch', sa.String(), nullable=False),
        sa.Column('status', sa.String(), nullable=False),
        sa.Column('verified', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('source_pdf', sa.String(), nullable=True),
        sa.Column('source_pdf_revision_date', sa.String(), nullable=True),
        sa.Column('source_detail', sa.Text(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
    )
    op.create_index('ix_product_line_branches_product_line_id', 'product_line_branches', ['product_line_id'])
    op.create_index('ix_product_line_branches_branch', 'product_line_branches', ['branch'])
    op.create_index('ix_product_line_branches_status', 'product_line_branches', ['status'])
    op.create_index('ix_product_line_branches_verified', 'product_line_branches', ['verified'])


def downgrade() -> None:
    op.drop_table('product_line_branches')
