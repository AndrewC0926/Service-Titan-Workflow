"""pipeline_run heartbeat

Adds pipeline_run.heartbeat_at. An external hard kill (OOM -- confirmed
real, the cron container was killed by Render's OOM killer mid-fetch on
2026-08-13) never reaches finish_pipeline_run(), leaving a row stuck at
status="running" forever, indistinguishable from a genuinely live run.
heartbeat_at, touched periodically during a run, is what
app.pipeline_health.reap_stale_runs() uses to tell the two apart. See
PipelineRun's docstring in app/models.py.

Revision ID: 66142e80aa58
Revises: ec17711c0e53
Create Date: 2026-08-13 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = '66142e80aa58'
down_revision = 'ec17711c0e53'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('pipeline_run', sa.Column('heartbeat_at', sa.DateTime(), nullable=True))
    op.create_index('ix_pipeline_run_heartbeat_at', 'pipeline_run', ['heartbeat_at'])


def downgrade() -> None:
    op.drop_index('ix_pipeline_run_heartbeat_at', table_name='pipeline_run')
    op.drop_column('pipeline_run', 'heartbeat_at')
