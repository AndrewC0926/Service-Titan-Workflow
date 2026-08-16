"""review queue and capture audio

Adds review_queue (generic agent-proposal table, reused by every later
agent, not just voice_capture -- see ReviewQueue's docstring in
app/models.py) and capture_audio (raw voice-note bytes so the review card
can replay the original recording next to the transcript). See
app/pipeline/voice_capture.py.

Revision ID: a1c7e9f2b384
Revises: f4c8a2e6b913
Create Date: 2026-08-16 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = 'a1c7e9f2b384'
down_revision = 'f4c8a2e6b913'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'capture_audio',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('content_type', sa.String(), nullable=False),
        sa.Column('data', sa.LargeBinary(), nullable=False),
        sa.Column('size_bytes', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
    )
    op.create_index('ix_capture_audio_created_at', 'capture_audio', ['created_at'])

    op.create_table(
        'review_queue',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('agent', sa.String(), nullable=False),
        sa.Column('trace_id', sa.String(), nullable=False),
        sa.Column('entity_type', sa.String(), nullable=False),
        sa.Column('proposed_payload', sa.JSON(), nullable=False),
        sa.Column('provenance', sa.JSON(), nullable=False),
        sa.Column('confidence', sa.Float(), nullable=True),
        sa.Column('status', sa.String(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('decided_at', sa.DateTime(), nullable=True),
    )
    op.create_index('ix_review_queue_agent', 'review_queue', ['agent'])
    op.create_index('ix_review_queue_trace_id', 'review_queue', ['trace_id'])
    op.create_index('ix_review_queue_entity_type', 'review_queue', ['entity_type'])
    op.create_index('ix_review_queue_status', 'review_queue', ['status'])
    op.create_index('ix_review_queue_created_at', 'review_queue', ['created_at'])


def downgrade() -> None:
    op.drop_table('review_queue')
    op.drop_table('capture_audio')
