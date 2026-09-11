"""projects.delivery_method -> delivery_method_llm_hint (WS3.1 conflict decision)

The human decision WS3.1's own commit flagged as open: delivery_method_class
and pen_holder_role are now the ONLY delivery fields any rule may read (see
app.call_target's R2 and the guard test in tests/test_call_target.py). The
legacy `delivery_method` column (plain string, LLM-extracted from any
triaged document including entitlement filings) is renamed to
`delivery_method_llm_hint` and kept display-only -- every scoring/
call-target read of it is removed in this same change (app/call_target.py,
app/pipeline/resolve.py), see each file's own diff for the specifics.

A plain rename, not a drop-and-recreate: the LLM-extraction pipeline
(app.pipeline.extract, app.llm) and the ManualCorrection pin system
(app.pipeline.corrections) both still populate/correct this column under
its new name -- it becomes purely informational, not deleted. Local DB
checked directly before writing this: 0 rows in manual_corrections and
pinned_field_conflicts reference the old "delivery_method" field key, so
no data migration is needed for those tables' `field` string column.

Revision ID: a3d719c04b5e
Revises: f4b8c1a92e07
Create Date: 2026-09-12 00:00:00.000000
"""
from alembic import op


revision = 'a3d719c04b5e'
down_revision = 'f4b8c1a92e07'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column('projects', 'delivery_method', new_column_name='delivery_method_llm_hint')


def downgrade() -> None:
    op.alter_column('projects', 'delivery_method_llm_hint', new_column_name='delivery_method')
