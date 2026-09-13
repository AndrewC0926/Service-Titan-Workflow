"""weekly_briefs (Block 4B Item 3)

Archive of every generated Weekly Sales Intelligence Brief, immutable
once written -- payload is the full computed content, frozen at
generation time (see app.models.WeeklyBrief's own docstring for why).

Verified rollback: downgrade() drops the table, checked by running
upgrade -> downgrade -> upgrade against the local DB before this
migration was committed.

Revision ID: 6fec08eb6113
Revises: f2e00a774632
Create Date: 2026-09-13 12:00:00.000000
"""
from alembic import op
import sqlalchemy as sa

revision = '6fec08eb6113'
down_revision = 'f2e00a774632'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'weekly_briefs',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('snapshot_date', sa.DateTime(), nullable=False),
        sa.Column('generated_at', sa.DateTime(), nullable=False),
        sa.Column('generated_by', sa.String(), nullable=False),
        sa.Column('week_start', sa.DateTime(), nullable=False),
        sa.Column('week_end', sa.DateTime(), nullable=False),
        sa.Column('payload', sa.JSON(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_weekly_briefs_snapshot_date'), 'weekly_briefs', ['snapshot_date'], unique=False)
    op.create_index(op.f('ix_weekly_briefs_generated_at'), 'weekly_briefs', ['generated_at'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_weekly_briefs_generated_at'), table_name='weekly_briefs')
    op.drop_index(op.f('ix_weekly_briefs_snapshot_date'), table_name='weekly_briefs')
    op.drop_table('weekly_briefs')
