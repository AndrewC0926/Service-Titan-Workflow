"""room_questions (Block 4C Item 4: "Ask the room")

One row per posted question. anchor_type/anchor_id name the account/
contractor/project/building the question is about; answered_note_id is
set once someone answers (the real answer lives as a DecisionNote,
note_type=intel -- this row just tracks open/answered). Never deleted.

Verified rollback: downgrade() drops the table, checked by running
upgrade -> downgrade -> upgrade against the local DB before this
migration was committed.

Revision ID: f7fd3159dd22
Revises: f2d0b396066f
Create Date: 2026-09-13 11:01:55.395475
"""
from alembic import op
import sqlalchemy as sa
import sqlmodel


revision = 'f7fd3159dd22'
down_revision = 'f2d0b396066f'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'room_questions',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('anchor_type', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('anchor_id', sa.Integer(), nullable=False),
        sa.Column('text', sa.Text(), nullable=False),
        sa.Column('author', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('answered_note_id', sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(['answered_note_id'], ['decision_notes.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_room_questions_anchor_type'), 'room_questions', ['anchor_type'], unique=False)
    op.create_index(op.f('ix_room_questions_anchor_id'), 'room_questions', ['anchor_id'], unique=False)
    op.create_index(op.f('ix_room_questions_author'), 'room_questions', ['author'], unique=False)
    op.create_index(op.f('ix_room_questions_created_at'), 'room_questions', ['created_at'], unique=False)
    op.create_index(op.f('ix_room_questions_answered_note_id'), 'room_questions', ['answered_note_id'], unique=False)


def downgrade() -> None:
    op.drop_table('room_questions')
