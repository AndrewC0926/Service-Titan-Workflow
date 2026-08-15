"""pipeline_run peak memory + per-stage peak memory

Adds pipeline_run.peak_rss_bytes (resource.getrusage(RUSAGE_SELF).ru_maxrss
at the end of `scout pipeline`, normalized to bytes) and a new
pipeline_stage_run table (one row per stage per run, written right where
stage success/failure already is -- see app.cli:pipeline's stage loop).
Render exposes no instance memory metrics for one-off cron jobs (confirmed
2026-08-15), so this is the only way to see memory on a scheduled run --
and the only way a future OOM points at the stage that caused it rather
than the run that died, since ru_maxrss never decreases and the last
completed stage's row survives an external kill that the run's own
finished_at/peak_rss_bytes never will. See PipelineRun and PipelineStageRun
docstrings in app/models.py.

Revision ID: a1c4e7f9b2d3
Revises: c8560da99fec
Create Date: 2026-08-15 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = 'a1c4e7f9b2d3'
down_revision = 'c8560da99fec'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('pipeline_run', sa.Column('peak_rss_bytes', sa.BigInteger(), nullable=True))
    op.create_table(
        'pipeline_stage_run',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('pipeline_run_id', sa.Integer(), sa.ForeignKey('pipeline_run.id'), nullable=False),
        sa.Column('stage', sa.String(), nullable=False),
        sa.Column('peak_rss_bytes', sa.BigInteger(), nullable=False),
        sa.Column('recorded_at', sa.DateTime(), nullable=False),
    )
    op.create_index('ix_pipeline_stage_run_pipeline_run_id', 'pipeline_stage_run', ['pipeline_run_id'])


def downgrade() -> None:
    op.drop_index('ix_pipeline_stage_run_pipeline_run_id', table_name='pipeline_stage_run')
    op.drop_table('pipeline_stage_run')
    op.drop_column('pipeline_run', 'peak_rss_bytes')
