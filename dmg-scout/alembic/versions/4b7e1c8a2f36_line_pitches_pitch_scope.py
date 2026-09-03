"""line_pitches.pitch_scope

'full' (fetched a real page, LLM-written, grounded) vs 'line_row_only'
(no page fetched, deterministic role/branch-only template, no LLM call,
no capability claims) -- see app.models.LinePitch's own docstring for why:
a fetch_failed line previously still got a full LLM-written pitch with
every claim then dropped by grounding, leaving an unlabeled, vague
fragment (the Aldes row, this feature's own first production run).

Revision ID: 4b7e1c8a2f36
Revises: 7c2e8a4f915b
Create Date: 2026-09-03 12:00:00.000000
"""
import sqlalchemy as sa
import sqlmodel

from alembic import op

revision = '4b7e1c8a2f36'
down_revision = '7c2e8a4f915b'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('line_pitches',
                  sa.Column('pitch_scope', sqlmodel.sql.sqltypes.AutoString(),
                            nullable=False, server_default='full'))
    op.create_index(op.f('ix_line_pitches_pitch_scope'), 'line_pitches', ['pitch_scope'])


def downgrade() -> None:
    op.drop_index(op.f('ix_line_pitches_pitch_scope'), table_name='line_pitches')
    op.drop_column('line_pitches', 'pitch_scope')
