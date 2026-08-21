"""product_lines.existence_verified: unknown becomes NULL, not True

`existence_verified` was added (e8f1b3d6a904) with `default true` on the
premise that existence is categorically different from every other
per-field *_verified flag on this model -- but a default is still an
inference, and this one silently marked 69 of 70 lines "confirmed to exist"
when nobody had confirmed anything about them; only VU Flow Environmental
was ever actually researched. That is exactly the discipline this app
enforces everywhere else (see CompetitorLine.covered_counties: "empty list
means not researched, not covers everywhere") applied backwards on this one
column.

NULL now means unknown/never researched. True means confirmed to exist,
and per app.accounts.seed_product_lines is only ever written alongside an
existence_verified_basis explaining how. False is unchanged: confirmed NOT
to exist, e.g. VU Flow Environmental. Existing NOT NULL/true rows are
backfilled to NULL wherever they carry no basis, so the migration doesn't
just relax the constraint, it corrects the data that constraint produced.

Revision ID: 9fa98312017c
Revises: 2ecc399b640a
Create Date: 2026-08-21 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = '9fa98312017c'
down_revision = '2ecc399b640a'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column('product_lines', 'existence_verified',
                     existing_type=sa.Boolean(), nullable=True, server_default=None)
    op.execute(
        "UPDATE product_lines SET existence_verified = NULL "
        "WHERE existence_verified = true AND existence_verified_basis IS NULL"
    )


def downgrade() -> None:
    op.execute("UPDATE product_lines SET existence_verified = true WHERE existence_verified IS NULL")
    op.alter_column('product_lines', 'existence_verified',
                     existing_type=sa.Boolean(), nullable=False, server_default=sa.true())
