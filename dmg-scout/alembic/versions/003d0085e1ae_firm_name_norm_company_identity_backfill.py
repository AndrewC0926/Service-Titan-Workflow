"""firms name_norm company-identity backfill

Re-keys the 27 of 451 Firm rows (as of 2026-09-11) whose name_norm changes
under app.normalize.normalize_company_name (added same session as the fix
this backfills -- see that function's own docstring and
docs/BACKTEST-2026-09-10.md's sibling report for the measured Firm-table
blast radius normalize_name caused). app.firms/app.accounts/app.developer_team
already compute the NEW value going forward as of that same commit; this
migration only re-keys the 27 EXISTING rows that predate it, so
Firm.name_norm matches what those call sites now compute for every row, not
just new ones.

Every (id, old, new) triple below was computed directly from the real 451-row
firms table (2026-09-11) -- verified id-for-id against a local restore of
production before writing this migration (see checked values in each UPDATE's
own WHERE clause: an id/old-name_norm pair that doesn't match exactly is
skipped, not guessed at). No app code is imported here, deliberately --
this repo's other migrations never import app.* (checked: none do), so this
one doesn't start; the 27 target values are the literal, already-computed
data instead. Verified zero new name_norm collisions are introduced (checked
directly, live, before writing this: recomputing normalize_company_name for
all 451 current names produces 27 changed values and 0 collisions), so the
firms_name_norm_key unique constraint is expected to hold with no special
handling.

Revision ID: 003d0085e1ae
Revises: a1f278b35dac
Create Date: 2026-09-11 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = '003d0085e1ae'
down_revision = 'a1f278b35dac'
branch_labels = None
depends_on = None

# (id, old name_norm, new name_norm) -- all 27, in id order.
_REKEY = [
    (45,  "investments",                               "v22 investments"),
    (79,  "construction",                               "lm construction"),
    (128, "stratcap digital infrastructure advisors",   "stratcap digital infrastructure advisors ii"),
    (132, "394 pacific portland domestic reit",         "394 pacific dc portland domestic reit"),
    (133, "blue owl digital infrastructure fund",       "blue owl digital infrastructure fund i"),
    (142, "pr",                                         "pr tx 1"),
    (197, "ai violet and ai violet",                    "ai violet and ai violet b2"),
    (202, "glc sfs",                                     "glc sfs ii"),
    (205, "bloomington gateway distribution center",    "iv5 bloomington gateway distribution center"),
    (214, "fgfw o synergy consulting",                  "fgfw iv c o synergy consulting"),
    (224, "acoustics",                                   "md acoustics"),
    (234, "von alton",                                   "von alton i"),
    (237, "dpif4 irvine",                                "dpif4 ca 44 irvine ii"),
    (304, "agua mansa",                                  "idi agua mansa"),
    (309, "btc commerce center",                        "btc iii commerce center"),
    (313, "ch realty riverside perris airport center",  "ch realty ix mc riverside perris airport center"),
    (323, "industrial realty",                           "ldc industrial realty"),
    (324, "logistics",                                   "idi logistics"),
    (342, "imperial irrigation district",               "imperial irrigation district iid"),
    (370, "tmg real estate investments",                 "tmg mv real estate investments"),
    (371, "industrial enterprises",                      "industrial vi enterprises"),
    (376, "riverside legacy nichols road",               "riverside legacy iv nichols road"),
    (382, "ipt menifee",                                 "ipt menifee cc lcc"),
    (412, "downing",                                     "downing iv"),
    (421, "sierra pacific power b a nv energy",          "sierra pacific power d b a nv energy"),
    (425, "connect",                                     "cci connect"),
    (449, "warm springs cp",                             "lv warm springs cp"),
]

firms = sa.table(
    "firms",
    sa.column("id", sa.Integer),
    sa.column("name_norm", sa.String),
)


def upgrade() -> None:
    conn = op.get_bind()
    for firm_id, old_norm, new_norm in _REKEY:
        conn.execute(
            firms.update()
            .where(firms.c.id == firm_id, firms.c.name_norm == old_norm)
            .values(name_norm=new_norm)
        )


def downgrade() -> None:
    conn = op.get_bind()
    for firm_id, old_norm, new_norm in _REKEY:
        conn.execute(
            firms.update()
            .where(firms.c.id == firm_id, firms.c.name_norm == new_norm)
            .values(name_norm=old_norm)
        )
