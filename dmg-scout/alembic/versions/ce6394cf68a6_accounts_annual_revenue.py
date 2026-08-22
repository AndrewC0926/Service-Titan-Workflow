"""accounts.annual_revenue

Adds accounts.annual_revenue (nullable, no default) for the account roster
importer (app.importers.account_roster_csv). NULL means never stated in the
source roster -- never 0, never guessed from anything else on the row. See
Account's own docstring on why nothing here gets an inferred default.

Revision ID: ce6394cf68a6
Revises: 9fa98312017c
Create Date: 2026-08-21 00:00:00.000000
"""
import sqlalchemy as sa

from alembic import op

revision = 'ce6394cf68a6'
down_revision = '9fa98312017c'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('accounts', sa.Column('annual_revenue', sa.Float(), nullable=True))


def downgrade() -> None:
    op.drop_column('accounts', 'annual_revenue')
