"""portfolio same_block discriminator

Adds retrofit_buildings.portfolio_same_block -- whether every member of a
detected portfolio group shares the same APN book/page prefix (one
physical property recorded as multiple assessor parcels) vs spans more
than one block (a candidate genuine multi-property transaction). See
app/portfolios.py's APN_BLOCK_PREFIX_LEN and app/models.py's
RetrofitBuilding.portfolio_same_block docstrings for the 2026-08-19
spot-check that established this discriminator.

Revision ID: f61c8d3e9a72
Revises: e3a9c15f7b04
Create Date: 2026-08-19 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = 'f61c8d3e9a72'
down_revision = 'e3a9c15f7b04'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('retrofit_buildings', sa.Column('portfolio_same_block', sa.Boolean(), nullable=True))


def downgrade() -> None:
    op.drop_column('retrofit_buildings', 'portfolio_same_block')
