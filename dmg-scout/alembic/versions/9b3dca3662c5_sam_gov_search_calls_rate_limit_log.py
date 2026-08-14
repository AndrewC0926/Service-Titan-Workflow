"""sam_gov_search_calls rate limit log

Revision ID: 9b3dca3662c5
Revises: 77944578462a
Create Date: 2026-08-13 16:18:27.129306
"""
from alembic import op
import sqlalchemy as sa
import sqlmodel


revision = '9b3dca3662c5'
down_revision = '77944578462a'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "sam_gov_search_calls",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("called_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_sam_gov_search_calls_called_at", "sam_gov_search_calls", ["called_at"])


def downgrade() -> None:
    op.drop_table("sam_gov_search_calls")
