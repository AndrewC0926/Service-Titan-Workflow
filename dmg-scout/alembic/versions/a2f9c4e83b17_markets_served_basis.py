"""markets_served_basis on product lines

Adds product_lines.markets_served_basis -- a citation (URL + retrieval
date) for markets_served, required once that field is populated from real
manufacturer literature rather than the earlier text-inference-only
version. Nullable, no inferred default -- see app/models.py's ProductLine
docstring.

Revision ID: a2f9c4e83b17
Revises: 66623c2f0191
Create Date: 2026-08-10 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = 'a2f9c4e83b17'
down_revision = '66623c2f0191'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('product_lines', sa.Column('markets_served_basis', sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column('product_lines', 'markets_served_basis')
