"""project delivery method

Adds delivery_method to signals (extracted, null unless a filing states it)
and projects (rolled up from linked signals in app.pipeline.resolve._absorb)
-- see Project.delivery_method's docstring in app/models.py for the full
"who selects the equipment" reasoning.

Revision ID: d3e6a9c2f157
Revises: a1c7e9f2b384
Create Date: 2026-08-16 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = 'd3e6a9c2f157'
down_revision = 'a1c7e9f2b384'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('signals', sa.Column('delivery_method', sa.String(), nullable=True))
    op.add_column('projects', sa.Column('delivery_method', sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column('projects', 'delivery_method')
    op.drop_column('signals', 'delivery_method')
