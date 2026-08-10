"""markets_served_source

Adds product_lines.markets_served_source -- distinguishes "researched"
(config.yaml `markets:` key, cited in markets_served_basis) from
"legacy_guess" (the unsourced MARKETS_BY_LINE fallback table) so the two
never render identically. Nullable, no inferred default; a null source
with an empty markets_served means genuinely unmapped. See
app/models.py's ProductLine docstring and app/accounts.py:seed_product_lines.

Revision ID: 4f454e8b3a42
Revises: a2f9c4e83b17
Create Date: 2026-08-09 20:24:58.716000
"""
from alembic import op
import sqlalchemy as sa


revision = '4f454e8b3a42'
down_revision = 'a2f9c4e83b17'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('product_lines', sa.Column('markets_served_source', sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column('product_lines', 'markets_served_source')
