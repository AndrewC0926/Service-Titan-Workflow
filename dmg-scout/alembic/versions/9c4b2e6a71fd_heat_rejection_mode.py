"""heat rejection mode on product lines

Adds product_lines.heat_rejection_mode/heat_rejection_mode_verified/
heat_rejection_mode_basis. Model-level fact (evaporative, adiabatic/
hybrid, dry/air-cooled, closed-loop), never inferred from equipment_type
or brand reputation -- unverified until confirmed with the factory. See
config.yaml's line_card comment and app/models.py's ProductLine
docstring.

Revision ID: 9c4b2e6a71fd
Revises: 3f7a9d2c15e8
Create Date: 2026-08-09 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = '9c4b2e6a71fd'
down_revision = '3f7a9d2c15e8'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('product_lines', sa.Column('heat_rejection_mode', sa.String(), nullable=True))
    op.add_column('product_lines', sa.Column(
        'heat_rejection_mode_verified', sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column('product_lines', sa.Column('heat_rejection_mode_basis', sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column('product_lines', 'heat_rejection_mode_basis')
    op.drop_column('product_lines', 'heat_rejection_mode_verified')
    op.drop_column('product_lines', 'heat_rejection_mode')
