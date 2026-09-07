"""Add `school_facility_funding` to the signaltype enum.

Missed in the original OPSC migration (4ae0d1db0080), which created
opsc_projects/opsc_workload but never touched signals.signal_type's own
Postgres enum type -- caught in production when `scout fetch-opsc-projects`
aborted every signal insert with `invalid input value for enum signaltype`,
which rolled back the WHOLE transaction (Postgres aborts a transaction on
any statement error, including the un-committed opsc_projects rows from
the same run -- there is no partial commit here).

Same fix, same constraint, as d3e6a9c42b57 (category esco): **ALTER
TYPE ... ADD VALUE cannot run inside a transaction block**, and Alembic
wraps migrations in one, so this commits the surrounding transaction first
and runs the ALTER on its own. Not reversible -- Postgres has no DROP
VALUE. See downgrade().

Revision ID: a77ddb56a5a1
Revises: 4ae0d1db0080
Create Date: 2026-09-07 14:42:32.387976
"""
from alembic import op

revision = 'a77ddb56a5a1'
down_revision = '4ae0d1db0080'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("COMMIT")
    op.execute("ALTER TYPE signaltype ADD VALUE IF NOT EXISTS 'school_facility_funding'")


def downgrade() -> None:
    """Deliberately a no-op, not a lie -- see d3e6a9c42b57's downgrade for why:
    Postgres cannot remove an enum value, and faking it by recreating the type
    would have to decide what happens to rows already carrying this value."""
