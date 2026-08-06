"""One signal per raw document, enforced by the database

Extract already intended this — it looks for an existing signal on the document
and updates it rather than inserting a second. But intent in application code is
not a guarantee: the lookup and the insert straddle a Sonnet call, so two
concurrent extract runs can both find nothing and both insert. That is exactly
how signal 503 became projects #961 and #963 one stage downstream, and extract
had the same gap with no constraint to catch it.

fetch has uq_source_uid and notify has uq_digest_item. Signals were the one link
in the chain with a natural key and no constraint on it.

Revision ID: 7f3a2d81c4e6
Revises: 019c1612f4f7
"""
from alembic import op

revision = "7f3a2d81c4e6"
down_revision = "019c1612f4f7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # raw_document_id is nullable (manual signals carry none), and Postgres treats
    # NULLs as distinct in a unique index, so hand-entered signals are unaffected.
    op.create_unique_constraint("uq_signal_raw_document", "signals", ["raw_document_id"])


def downgrade() -> None:
    op.drop_constraint("uq_signal_raw_document", "signals", type_="unique")
