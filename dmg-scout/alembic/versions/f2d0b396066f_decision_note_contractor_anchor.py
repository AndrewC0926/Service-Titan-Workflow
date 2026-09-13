"""decision_note_contractor_anchor (Block 4C Item 4)

A note answering a contractor-anchored "ask the room" question needs a
real anchor of its own, not an unrelated Opportunity's account_id
borrowed as a stand-in -- DecisionNote's own docstring's "no unresolved-
entity concept" rule applies here exactly as it does to every other
anchor. Nullable, never backfilled -- no existing DecisionNote row has an
implicit contractor anchor to infer.

Verified rollback: downgrade() drops the column, checked by running
upgrade -> downgrade -> upgrade against the local DB before this
migration was committed.

Revision ID: f2d0b396066f
Revises: 7d917d4d4406
Create Date: 2026-09-13 11:01:48.642289
"""
from alembic import op
import sqlalchemy as sa
import sqlmodel


revision = 'f2d0b396066f'
down_revision = '7d917d4d4406'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('decision_notes', sa.Column('contractor_id', sa.Integer(), nullable=True))
    op.create_index(op.f('ix_decision_notes_contractor_id'), 'decision_notes', ['contractor_id'], unique=False)
    op.create_foreign_key('fk_decision_notes_contractor_id_contractors', 'decision_notes',
                          'contractors', ['contractor_id'], ['id'])


def downgrade() -> None:
    op.drop_constraint('fk_decision_notes_contractor_id_contractors', 'decision_notes', type_='foreignkey')
    op.drop_index(op.f('ix_decision_notes_contractor_id'), table_name='decision_notes')
    op.drop_column('decision_notes', 'contractor_id')
