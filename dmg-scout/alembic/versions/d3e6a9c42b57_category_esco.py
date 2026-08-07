"""Add `esco` to the category enum.

An ESCO award is a public agency selecting an Energy Services Company for a
performance contract on buildings it already owns. Equipment gets bought in 12 to
24 months and — unlike an architect or CM award — the ESCO actually selects it.

This is a category rather than a keyword tag for exactly the reason industrial
was: triage keeps only what it can name. Every label outside the enum is
classified `other`, `other` means discarded, and detection that ends in discard
captures nothing. The keyword gate was already storing these documents; without
this they were being stored and then thrown away one stage later.

No backfill. Every existing row was classified by a triage prompt that had no
`esco` option, so stamping any of them retroactively would be inventing a verdict
nobody made. Existing rows keep the category they were actually given.

**ALTER TYPE ... ADD VALUE cannot run inside a transaction block**, and Alembic
wraps migrations in one. Postgres 12+ permits it only when the new value is not
used in the same transaction, which is fragile to rely on — so this commits the
surrounding transaction first and runs the ALTER on its own. That also means this
migration is not reversible: Postgres has no DROP VALUE. See downgrade().

Revision ID: d3e6a9c42b57
Revises: a4b8e2c15d93
Create Date: 2026-08-07 02:05:00.000000
"""
from alembic import op

revision = 'd3e6a9c42b57'
down_revision = 'a4b8e2c15d93'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # IF NOT EXISTS makes a re-run harmless; the enum is shared by signals.category
    # and projects.category, so this one statement covers both.
    op.execute("COMMIT")
    op.execute("ALTER TYPE category ADD VALUE IF NOT EXISTS 'esco'")


def downgrade() -> None:
    """Deliberately a no-op, not a lie.

    Postgres cannot remove a value from an enum. Faking it by recreating the type
    would have to decide what happens to rows already carrying `esco`, and
    silently rewriting classified rows to `other` during a rollback is worse than
    leaving an unused label in the type. The label is inert if nothing writes it.
    """
