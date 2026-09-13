"""signals_feed table (Hotfix: GET /signals measured 536MB peak RSS,
395MB above baseline, against the real local restore -- unified_signals()
materialized every FeedSignal from every source on every page load, on a
512MB web instance)

Persisted cache of app.pipeline.signals_feed.unified_signals()'s own
output, refreshed by `scout refresh-signals-feed` (called by the nightly
pipeline right after find-replacement-candidates). /signals now paginates
server-side (50/page) off this table instead of computing the feed live;
the four-part filter and POST /signals/promote read the same table.

trigger_type is a NEW native enum type here -- it was never persisted
before this table (see TriggerType's own docstring: "read-time, in-memory
shape... this has no migration"). pen_state and category reuse the
EXISTING native enum types created by earlier migrations
(067c0ef15e70_pen_state_on_signals_and_opportunities,
b1c4e7a90f22_signal_project_category) via create_type=False -- a second
CREATE TYPE for either would fail outright.

Verified rollback: downgrade() drops the table and the new trigger_type
enum, checked by running upgrade -> downgrade -> upgrade against a real
schema before this migration was committed.

Revision ID: b3f6a1d9c274
Revises: a9c47e2b1f83
Create Date: 2026-09-13 23:20:00.000000
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = 'b3f6a1d9c274'
down_revision = 'a9c47e2b1f83'
branch_labels = None
depends_on = None

# postgresql.ENUM, not generic sa.Enum -- create_type=False on a generic
# sa.Enum does not survive dialect adaptation to the postgres impl (hit
# this directly: it still emitted CREATE TYPE during op.create_table and
# collided with the explicit .create() call below). postgresql.ENUM(...,
# create_type=False) is the same precedent d2e8f4a91b56's OPPORTUNITY_
# STAGE/OPPORTUNITY_STAGE_COL split already uses. The bare one (create_
# type defaults True) is only ever used for the standalone .create()/
# .drop() calls; the _COL one is the only one ever handed to
# op.create_table.
TRIGGER_TYPE = postgresql.ENUM(
    'entitlement_milestone', 'permit_gap', 'permit_activity', 'deadline',
    'quiet_account', 'public_work', 'relationship_intro', name='triggertype',
)
TRIGGER_TYPE_COL = postgresql.ENUM(
    'entitlement_milestone', 'permit_gap', 'permit_activity', 'deadline',
    'quiet_account', 'public_work', 'relationship_intro', name='triggertype', create_type=False,
)
PEN_STATE_COL = postgresql.ENUM('not_moved', 'moving', 'moved', 'ABSTAIN', name='penstate', create_type=False)
CATEGORY_COL = postgresql.ENUM('data_center', 'industrial', 'other', name='category', create_type=False)


def upgrade() -> None:
    bind = op.get_bind()
    TRIGGER_TYPE.create(bind, checkfirst=True)
    op.create_table(
        'signals_feed',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('source', sa.String(), nullable=False),
        sa.Column('source_id', sa.String(), nullable=False),
        sa.Column('trigger_type', TRIGGER_TYPE_COL, nullable=False),
        sa.Column('trigger_date', sa.DateTime(), nullable=True),
        sa.Column('evidence', sa.String(), nullable=False),
        sa.Column('confidence', sa.Float(), nullable=True),
        sa.Column('project_id', sa.Integer(), nullable=True),
        sa.Column('building_id', sa.Integer(), nullable=True),
        sa.Column('facility_perm_id', sa.String(), nullable=True),
        sa.Column('account_id', sa.Integer(), nullable=True),
        sa.Column('category', CATEGORY_COL, nullable=True),
        sa.Column('pen_state', PEN_STATE_COL, nullable=False, server_default='ABSTAIN'),
        sa.Column('refreshed_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_signals_feed_source'), 'signals_feed', ['source'], unique=False)
    op.create_index(op.f('ix_signals_feed_source_id'), 'signals_feed', ['source_id'], unique=False)
    op.create_index(op.f('ix_signals_feed_trigger_type'), 'signals_feed', ['trigger_type'], unique=False)
    op.create_index(op.f('ix_signals_feed_trigger_date'), 'signals_feed', ['trigger_date'], unique=False)
    op.create_index(op.f('ix_signals_feed_refreshed_at'), 'signals_feed', ['refreshed_at'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_signals_feed_refreshed_at'), table_name='signals_feed')
    op.drop_index(op.f('ix_signals_feed_trigger_date'), table_name='signals_feed')
    op.drop_index(op.f('ix_signals_feed_trigger_type'), table_name='signals_feed')
    op.drop_index(op.f('ix_signals_feed_source_id'), table_name='signals_feed')
    op.drop_index(op.f('ix_signals_feed_source'), table_name='signals_feed')
    op.drop_table('signals_feed')
    TRIGGER_TYPE.drop(op.get_bind(), checkfirst=True)
