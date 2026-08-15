"""days-to-estimated-bid confidence interval

Adds project.days_to_estimated_bid_low/high -- 95% CI bounds around the
existing point figure, populated only for a stage with a real measured
interval (currently just entitlement, fit from CEQAnet NOP->NOD spreads;
see config.yaml's scoring.days_to_bid_by_stage comment and
app/assumptions.py for sample size, date range, and method). Null for
every stage still on an invented placeholder -- see Project's docstring
in app/models.py.

Revision ID: e2a9c4f7b1d8
Revises: b7f3d1e9a4c6
Create Date: 2026-08-16 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = 'e2a9c4f7b1d8'
down_revision = 'b7f3d1e9a4c6'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('projects', sa.Column('days_to_estimated_bid_low', sa.Integer(), nullable=True))
    op.add_column('projects', sa.Column('days_to_estimated_bid_high', sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column('projects', 'days_to_estimated_bid_high')
    op.drop_column('projects', 'days_to_estimated_bid_low')
