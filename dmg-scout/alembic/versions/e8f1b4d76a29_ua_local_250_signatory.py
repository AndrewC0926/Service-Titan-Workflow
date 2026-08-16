"""ua local 250 signatory status on contractors

Adds ua_local_250_signatory (and matched-name/checked-at audit fields) to
contractors -- an attribute of the contractor, never a ranking term, see
app/pipeline/local250.py.

Revision ID: e8f1b4d76a29
Revises: d3e6a9c2f157
Create Date: 2026-08-16 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = 'e8f1b4d76a29'
down_revision = 'd3e6a9c2f157'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('contractors', sa.Column('ua_local_250_signatory', sa.Boolean(),
                                           nullable=False, server_default=sa.false()))
    op.add_column('contractors', sa.Column('ua_local_250_matched_name', sa.String(), nullable=True))
    op.add_column('contractors', sa.Column('ua_local_250_checked_at', sa.DateTime(), nullable=True))
    op.create_index('ix_contractors_ua_local_250_signatory', 'contractors', ['ua_local_250_signatory'])


def downgrade() -> None:
    op.drop_index('ix_contractors_ua_local_250_signatory', table_name='contractors')
    op.drop_column('contractors', 'ua_local_250_checked_at')
    op.drop_column('contractors', 'ua_local_250_matched_name')
    op.drop_column('contractors', 'ua_local_250_signatory')
