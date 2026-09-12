"""Create outcomes table (Block 4A Item 2, Master Plan v3.6 sections 15/31)

Additive only. Verified rollback: downgrade() drops the table and its
three new enum types, checked by actually running upgrade -> downgrade ->
upgrade against the local DB before this migration was committed -- same
create_type=False fix d2e8f4a91b56/067c0ef15e70 needed (op.create_table's
own implicit CREATE TYPE would otherwise collide with the explicit
pre-create below).

Revision ID: 3a3f9fc8ba41
Revises: 7ad64088810f
Create Date: 2026-09-13 01:00:00.000000
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = '3a3f9fc8ba41'
down_revision = '7ad64088810f'
branch_labels = None
depends_on = None

DISPOSITION = postgresql.ENUM(
    'connected', 'left_voicemail', 'no_answer', 'bad_number_wrong_contact',
    'meeting_set', 'not_now', 'won', 'lost', name='disposition')
LOST_REASON_CODE = postgresql.ENUM(
    'price', 'lost_to_competitor', 'no_decision_budget', 'timing_deferred',
    'specd_out_not_our_line', 'wrong_contact_no_reach', 'not_eligible_osp_ahri', 'other',
    name='lostreasoncode')
OUTCOME_SOURCE = postgresql.ENUM('web', 'capture', name='outcomesource')

DISPOSITION_COL = postgresql.ENUM(
    'connected', 'left_voicemail', 'no_answer', 'bad_number_wrong_contact',
    'meeting_set', 'not_now', 'won', 'lost', name='disposition', create_type=False)
LOST_REASON_CODE_COL = postgresql.ENUM(
    'price', 'lost_to_competitor', 'no_decision_budget', 'timing_deferred',
    'specd_out_not_our_line', 'wrong_contact_no_reach', 'not_eligible_osp_ahri', 'other',
    name='lostreasoncode', create_type=False)
OUTCOME_SOURCE_COL = postgresql.ENUM('web', 'capture', name='outcomesource', create_type=False)


def upgrade() -> None:
    bind = op.get_bind()
    DISPOSITION.create(bind, checkfirst=True)
    LOST_REASON_CODE.create(bind, checkfirst=True)
    OUTCOME_SOURCE.create(bind, checkfirst=True)

    op.create_table(
        'outcomes',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('opportunity_id', sa.Integer(), sa.ForeignKey('opportunities.id'), nullable=False),
        sa.Column('user', sa.String(), nullable=False),
        sa.Column('disposition', DISPOSITION_COL, nullable=False),
        sa.Column('reason_code', LOST_REASON_CODE_COL, nullable=True),
        sa.Column('competitor', sa.String(), nullable=True),
        sa.Column('note', sa.Text(), nullable=False, server_default=''),
        sa.Column('source', OUTCOME_SOURCE_COL, nullable=False, server_default='web'),
        sa.Column('created_at', sa.DateTime(), nullable=False),
    )
    for col in ('opportunity_id', 'user', 'disposition', 'reason_code', 'source', 'created_at'):
        op.create_index(op.f(f'ix_outcomes_{col}'), 'outcomes', [col], unique=False)
    op.alter_column('outcomes', 'source', server_default=None)
    op.alter_column('outcomes', 'note', server_default=None)


def downgrade() -> None:
    for col in ('created_at', 'source', 'reason_code', 'disposition', 'user', 'opportunity_id'):
        op.drop_index(op.f(f'ix_outcomes_{col}'), table_name='outcomes')
    op.drop_table('outcomes')

    bind = op.get_bind()
    OUTCOME_SOURCE.drop(bind, checkfirst=True)
    LOST_REASON_CODE.drop(bind, checkfirst=True)
    DISPOSITION.drop(bind, checkfirst=True)
