"""portfolio transaction grouping

Adds portfolio_group_id/portfolio_member_count/portfolio_combined_sqft/
portfolio_members to retrofit_buildings -- see app/portfolios.py's module
docstring for the detection method and app/models.py's RetrofitBuilding
docstring for why this is a plain column (pure derived fact, recomputed
fresh every rebuild, no external fetch to survive).

Revision ID: b6d4a91f3c58
Revises: a4b8f2c96e17
Create Date: 2026-08-17 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = 'b6d4a91f3c58'
down_revision = 'a4b8f2c96e17'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('retrofit_buildings', sa.Column('portfolio_group_id', sa.String(), nullable=True))
    op.add_column('retrofit_buildings', sa.Column('portfolio_member_count', sa.Integer(), nullable=True))
    op.add_column('retrofit_buildings', sa.Column('portfolio_combined_sqft', sa.Float(), nullable=True))
    op.add_column('retrofit_buildings',
                  sa.Column('portfolio_members', sa.JSON(), nullable=False, server_default='[]'))
    op.create_index('ix_retrofit_buildings_portfolio_group_id', 'retrofit_buildings', ['portfolio_group_id'])


def downgrade() -> None:
    op.drop_index('ix_retrofit_buildings_portfolio_group_id', table_name='retrofit_buildings')
    op.drop_column('retrofit_buildings', 'portfolio_members')
    op.drop_column('retrofit_buildings', 'portfolio_combined_sqft')
    op.drop_column('retrofit_buildings', 'portfolio_member_count')
    op.drop_column('retrofit_buildings', 'portfolio_group_id')
