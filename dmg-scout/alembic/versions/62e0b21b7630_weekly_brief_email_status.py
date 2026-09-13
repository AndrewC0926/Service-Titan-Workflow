"""weekly_briefs.emailed / email_skip_reason (Block 4C Item 6: the
automated Friday per-user brief run archives regardless of whether it
emailed, then records what happened)

emailed defaults False and email_skip_reason defaults NULL -- every
existing row (the Block 4B Item 3 manual CLI path, which never emails)
backfills to "never emailed, no reason to give" rather than a guessed
skip reason it never actually had.

Verified rollback: downgrade() drops both columns, checked by running
upgrade -> downgrade -> upgrade against a real schema before this
migration was committed.

Revision ID: 62e0b21b7630
Revises: 44ca2d67b1fb
Create Date: 2026-09-13 12:40:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = '62e0b21b7630'
down_revision = '44ca2d67b1fb'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('weekly_briefs', sa.Column('emailed', sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column('weekly_briefs', sa.Column('email_skip_reason', sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column('weekly_briefs', 'email_skip_reason')
    op.drop_column('weekly_briefs', 'emailed')
