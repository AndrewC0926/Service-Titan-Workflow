"""facility_type on signals: what the building does, at sizing resolution

Industrial tonnage comes from floor area, and sqft-per-ton varies 50x across
building types. Without this, every industrial row shared one band and a
1,000,000 sqft fulfillment centre was sized like light manufacturing.

Existing rows are stamped `unknown`, which is honest: they were extracted before
the field existed, so nothing about their type was ever read. `unknown` gets the
full 50-2500 span rather than a convenient middle.

Revision ID: c2d5f8b31a44
Revises: b1c4e7a90f22
Create Date: 2026-08-05 10:52:00.000000
"""
from alembic import op
import sqlalchemy as sa

revision = 'c2d5f8b31a44'
down_revision = 'b1c4e7a90f22'
branch_labels = None
depends_on = None

FACILITY_TYPE = sa.Enum(
    'distribution_fulfillment', 'warehouse_conditioned', 'light_manufacturing',
    'heavy_manufacturing', 'cleanroom', 'office_rnd', 'data_center', 'unknown',
    name='facilitytype')


def upgrade() -> None:
    FACILITY_TYPE.create(op.get_bind(), checkfirst=True)
    op.add_column('signals', sa.Column(
        'facility_type', FACILITY_TYPE, nullable=False, server_default='unknown'))
    op.create_index(op.f('ix_signals_facility_type'), 'signals', ['facility_type'],
                    unique=False)
    op.alter_column('signals', 'facility_type', server_default=None)


def downgrade() -> None:
    op.drop_index(op.f('ix_signals_facility_type'), table_name='signals')
    op.drop_column('signals', 'facility_type')
    FACILITY_TYPE.drop(op.get_bind(), checkfirst=True)
