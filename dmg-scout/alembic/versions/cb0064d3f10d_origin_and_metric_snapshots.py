"""opportunities.origin + metric_snapshots table (Block 4A Item 4)

Master Plan v3.6 section 32: "Every Opportunity... carries an origin."
Section 30: "A metric_snapshot table... written by the nightly cron,
append-only." Additive only. Verified rollback: downgrade() drops the
metric_snapshots table, the opportunities.origin column, and the two new
enum types, checked by actually running upgrade -> downgrade -> upgrade
against the local DB before this migration was committed.

Revision ID: cb0064d3f10d
Revises: e99344983e3e
Create Date: 2026-09-13 03:00:00.000000
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = 'cb0064d3f10d'
down_revision = 'e99344983e3e'
branch_labels = None
depends_on = None

ORIGIN = postgresql.ENUM(
    'scout_signal', 'relationship_intro', 'rep_originated', 'inbound', 'inside_sales', name='origin')
ORIGIN_COL = postgresql.ENUM(
    'scout_signal', 'relationship_intro', 'rep_originated', 'inbound', 'inside_sales',
    name='origin', create_type=False)


def upgrade() -> None:
    bind = op.get_bind()
    ORIGIN.create(bind, checkfirst=True)

    op.add_column('opportunities', sa.Column('origin', ORIGIN_COL, nullable=False,
                                             server_default='scout_signal'))
    op.create_index(op.f('ix_opportunities_origin'), 'opportunities', ['origin'], unique=False)
    op.alter_column('opportunities', 'origin', server_default=None)

    op.create_table(
        'metric_snapshots',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('snapshot_date', sa.DateTime(), nullable=False),
        sa.Column('metric_key', sa.String(), nullable=False),
        sa.Column('dimensions', sa.JSON(), nullable=False, server_default='{}'),
        sa.Column('value', sa.Float(), nullable=False, server_default='0'),
        sa.Column('computed_by', sa.String(), nullable=False, server_default='pipeline'),
    )
    for col in ('snapshot_date', 'metric_key'):
        op.create_index(op.f(f'ix_metric_snapshots_{col}'), 'metric_snapshots', [col], unique=False)
    op.alter_column('metric_snapshots', 'dimensions', server_default=None)
    op.alter_column('metric_snapshots', 'value', server_default=None)
    op.alter_column('metric_snapshots', 'computed_by', server_default=None)


def downgrade() -> None:
    op.drop_index(op.f('ix_metric_snapshots_metric_key'), table_name='metric_snapshots')
    op.drop_index(op.f('ix_metric_snapshots_snapshot_date'), table_name='metric_snapshots')
    op.drop_table('metric_snapshots')

    op.drop_index(op.f('ix_opportunities_origin'), table_name='opportunities')
    op.drop_column('opportunities', 'origin')

    bind = op.get_bind()
    ORIGIN.drop(bind, checkfirst=True)
