"""product_lines.existence_verified

Adds product_lines.existence_verified (default true) and its basis --
distinct from every other *_verified flag on this model, which caveat one
FIELD on a line whose existence was never in question. VU Flow
Environmental is the one line seeded false: no real company by this name
could be located across OSP, AHRI, country-of-manufacture, or general web
research (2026-08-09). See ProductLine's own docstring.

Revision ID: e8f1b3d6a904
Revises: d4c1a9e7f253
Create Date: 2026-08-21 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = 'e8f1b3d6a904'
down_revision = 'd4c1a9e7f253'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('product_lines', sa.Column(
        'existence_verified', sa.Boolean(), nullable=False, server_default=sa.true()))
    op.add_column('product_lines', sa.Column('existence_verified_basis', sa.String(), nullable=True))
    op.create_index('ix_product_lines_existence_verified', 'product_lines', ['existence_verified'])


def downgrade() -> None:
    op.drop_index('ix_product_lines_existence_verified', table_name='product_lines')
    op.drop_column('product_lines', 'existence_verified_basis')
    op.drop_column('product_lines', 'existence_verified')
