"""pipeline_run + staleness_alerts

pipeline_run: one row per `scout pipeline` invocation (id, started_at,
finished_at, status, records_processed, error). staleness_alerts: one row
per staleness-alarm email actually sent, rate-limiting that alert to at most
once per 24h. See app/pipeline_health.py.

Revision ID: ec17711c0e53
Revises: ddbcb6a149c2
Create Date: 2026-08-13 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = 'ec17711c0e53'
down_revision = 'ddbcb6a149c2'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'pipeline_run',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('started_at', sa.DateTime(), nullable=False),
        sa.Column('finished_at', sa.DateTime(), nullable=True),
        sa.Column('status', sa.String(), nullable=False, server_default='running'),
        sa.Column('records_processed', sa.Integer(), nullable=True),
        sa.Column('error', sa.Text(), nullable=True),
    )
    op.create_index('ix_pipeline_run_started_at', 'pipeline_run', ['started_at'])
    op.create_index('ix_pipeline_run_status', 'pipeline_run', ['status'])

    op.create_table(
        'staleness_alerts',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('sent_at', sa.DateTime(), nullable=False),
    )
    op.create_index('ix_staleness_alerts_sent_at', 'staleness_alerts', ['sent_at'])


def downgrade() -> None:
    op.drop_index('ix_staleness_alerts_sent_at', table_name='staleness_alerts')
    op.drop_table('staleness_alerts')
    op.drop_index('ix_pipeline_run_status', table_name='pipeline_run')
    op.drop_index('ix_pipeline_run_started_at', table_name='pipeline_run')
    op.drop_table('pipeline_run')
