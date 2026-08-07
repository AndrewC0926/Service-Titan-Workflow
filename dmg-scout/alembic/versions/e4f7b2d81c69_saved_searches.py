"""Saved searches with alerts.

Standing questions about the board — "anything over 10 MW in Storey County",
"anything naming Southland or ACCO", "any flagged project that changes stage".

`criteria` and `last_seen` are JSON rather than columns. criteria because the set
of askable questions will keep growing and a migration per question is a tax on
asking them; last_seen because "changed" is a claim about two points in time and
the previous answer has to be stored somewhere to compare against.

Revision ID: e4f7b2d81c69
Revises: d3e6a9c42b57
Create Date: 2026-08-07 02:30:00.000000
"""
import sqlalchemy as sa
from alembic import op

revision = 'e4f7b2d81c69'
down_revision = 'd3e6a9c42b57'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'saved_searches',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('name', sa.String(), nullable=False),
        sa.Column('criteria', sa.JSON(), nullable=False),
        sa.Column('alert', sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column('alert_on_change', sa.Boolean(), nullable=False,
                  server_default=sa.false()),
        sa.Column('last_seen', sa.JSON(), nullable=False),
        sa.Column('last_run_at', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )


def downgrade() -> None:
    op.drop_table('saved_searches')
