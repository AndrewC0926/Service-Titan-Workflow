"""rewrite manual_corrections/pinned_field_conflicts field='delivery_method' -> 'delivery_method_llm_hint'

Gap found in a3d719c04b5e (the projects.delivery_method column rename): that
migration only touched the `projects` table's column. It did NOT rewrite any
pre-existing manual_corrections/pinned_field_conflicts row whose `field`
column literally stores the STRING "delivery_method" -- app.pipeline
.corrections.FIELD_CONVERTERS was renamed to key on "delivery_method_llm_hint"
in the same commit as that migration, so a legacy-keyed row would silently
stop resolving (`apply_manual_correction`'s own `if field not in
FIELD_CONVERTERS: raise ValueError` on any human trying to act on it, and
`latest_correction(session, project_id, "delivery_method_llm_hint")` would
never find it either, since it's still stored under the old key).

Checked directly against the local restore (442 real projects) before
writing this: 0 rows in either table, delivery_method field or otherwise --
this restore's manual_corrections/pinned_field_conflicts tables are empty
regardless. The Build Plan's own WS9 note (11 manual corrections, 31 review
candidates) describes PRODUCTION, not what this restore snapshot carries, so
that count could not be re-confirmed here. This migration runs the rewrite
unconditionally and safely either way -- a plain UPDATE ... WHERE field =
'delivery_method' is a no-op on an empty or non-matching table, and becomes
load-bearing the moment it runs against a database that does have real
legacy-keyed rows (production, once a human decides to run this there).

Revision ID: c1a4f6d2e9b0
Revises: b8f2e5a17c33
Create Date: 2026-09-13 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = 'c1a4f6d2e9b0'
down_revision = 'b8f2e5a17c33'
branch_labels = None
depends_on = None

_OLD_FIELD = 'delivery_method'
_NEW_FIELD = 'delivery_method_llm_hint'

manual_corrections = sa.table('manual_corrections', sa.column('field', sa.String))
pinned_field_conflicts = sa.table('pinned_field_conflicts', sa.column('field', sa.String))


def upgrade() -> None:
    conn = op.get_bind()
    conn.execute(
        manual_corrections.update()
        .where(manual_corrections.c.field == _OLD_FIELD)
        .values(field=_NEW_FIELD)
    )
    conn.execute(
        pinned_field_conflicts.update()
        .where(pinned_field_conflicts.c.field == _OLD_FIELD)
        .values(field=_NEW_FIELD)
    )


def downgrade() -> None:
    conn = op.get_bind()
    conn.execute(
        manual_corrections.update()
        .where(manual_corrections.c.field == _NEW_FIELD)
        .values(field=_OLD_FIELD)
    )
    conn.execute(
        pinned_field_conflicts.update()
        .where(pinned_field_conflicts.c.field == _NEW_FIELD)
        .values(field=_OLD_FIELD)
    )
