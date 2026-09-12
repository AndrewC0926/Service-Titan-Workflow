"""Add `retrofit_permit_gap`/`ab869_npc_deadline` to the signaltype enum

Block 4A Item 1 (Master Plan v3.6): "a Signal row is created for any
building or facility the moment it is promoted"
(app.pipeline.signals_feed.promote_to_opportunity) -- these two values
identify a Signal row as synthesized at promotion time, never from the
extraction pipeline.

Same fix, same constraint, as a77ddb56a5a1 (school_facility_funding) and
d3e6a9c42b57 (category esco): ALTER TYPE ... ADD VALUE cannot run inside
a transaction block, and Alembic wraps migrations in one, so this commits
the surrounding transaction first and runs the ALTER on its own. Not
reversible -- Postgres has no DROP VALUE. See downgrade().

Revision ID: 7ad64088810f
Revises: aebb7f6bd153
Create Date: 2026-09-13 00:10:00.000000
"""
from alembic import op

revision = '7ad64088810f'
down_revision = 'aebb7f6bd153'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("COMMIT")
    op.execute("ALTER TYPE signaltype ADD VALUE IF NOT EXISTS 'retrofit_permit_gap'")
    op.execute("COMMIT")
    op.execute("ALTER TYPE signaltype ADD VALUE IF NOT EXISTS 'ab869_npc_deadline'")


def downgrade() -> None:
    """Deliberately a no-op, not a lie -- see a77ddb56a5a1's downgrade for
    why: Postgres cannot remove an enum value, and faking it by recreating
    the type would have to decide what happens to rows already carrying
    this value."""
