"""source_rows_seen.changed_at

New nullable column on SourceRowSeen, set only on diff_source's genuine
fingerprint-change branch -- see app.models.SourceRowSeen's own docstring
for why last_seen_at alone cannot answer "did this row change" (it
advances for every present row every night, changed or not). Needed for
the daily brief's "new or changed since yesterday" section
(app.pipeline.notify.new_or_changed_since_yesterday).

Revision ID: ae1f913b9bb6
Revises: bc754089367e
Create Date: 2026-09-11 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = 'ae1f913b9bb6'
down_revision = 'bc754089367e'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('source_rows_seen', sa.Column('changed_at', sa.DateTime(), nullable=True))
    op.create_index(op.f('ix_source_rows_seen_changed_at'), 'source_rows_seen', ['changed_at'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_source_rows_seen_changed_at'), table_name='source_rows_seen')
    op.drop_column('source_rows_seen', 'changed_at')
