"""pen_state on signals and opportunities (Block 4A Item 1, Master Plan v3.6)

New `penstate` enum type (not_moved, moving, moved, ABSTAIN) plus a
pen_state column, default ABSTAIN, on both `signals` and `opportunities` --
section 12b: "every Signal and Opportunity carries pen_state."

Additive only. Verified rollback: downgrade() drops both columns then the
enum type, checked by actually running upgrade -> downgrade -> upgrade
against the local DB before this migration was committed -- same
discipline as d2e8f4a91b56, same postgresql.ENUM(..., create_type=False)
fix that migration needed (op.add_column's own implicit CREATE TYPE would
otherwise collide with the explicit pre-create below).

Revision ID: 067c0ef15e70
Revises: d2e8f4a91b56
Create Date: 2026-09-13 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = '067c0ef15e70'
down_revision = 'd2e8f4a91b56'
branch_labels = None
depends_on = None

PEN_STATE = postgresql.ENUM('not_moved', 'moving', 'moved', 'ABSTAIN', name='penstate')
PEN_STATE_COL = postgresql.ENUM('not_moved', 'moving', 'moved', 'ABSTAIN', name='penstate', create_type=False)


def upgrade() -> None:
    bind = op.get_bind()
    PEN_STATE.create(bind, checkfirst=True)

    op.add_column('signals', sa.Column('pen_state', PEN_STATE_COL, nullable=False, server_default='ABSTAIN'))
    op.create_index(op.f('ix_signals_pen_state'), 'signals', ['pen_state'], unique=False)
    op.alter_column('signals', 'pen_state', server_default=None)

    op.add_column('opportunities', sa.Column('pen_state', PEN_STATE_COL, nullable=False, server_default='ABSTAIN'))
    op.create_index(op.f('ix_opportunities_pen_state'), 'opportunities', ['pen_state'], unique=False)
    op.alter_column('opportunities', 'pen_state', server_default=None)


def downgrade() -> None:
    op.drop_index(op.f('ix_opportunities_pen_state'), table_name='opportunities')
    op.drop_column('opportunities', 'pen_state')

    op.drop_index(op.f('ix_signals_pen_state'), table_name='signals')
    op.drop_column('signals', 'pen_state')

    bind = op.get_bind()
    PEN_STATE.drop(bind, checkfirst=True)
