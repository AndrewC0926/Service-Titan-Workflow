"""BPELSG mechanical engineer roster (bpelsg_engineers)

New table -- see app.models.BpelsgEngineer and app/pipeline/bpelsg.py's
module docstring for the full design (in-territory Mechanical Engineer
licenses only, license_no as the primary key, idempotent upsert, no
firm/employer field because DCA's own file carries none).

Revision ID: ef1fc6ddf065
Revises: 3112b0365da2
Create Date: 2026-09-09 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa
import sqlmodel


revision = 'ef1fc6ddf065'
down_revision = '3112b0365da2'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('bpelsg_engineers',
    sa.Column('license_no', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
    sa.Column('name', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
    sa.Column('license_type', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
    sa.Column('city', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
    sa.Column('county', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
    sa.Column('status', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
    sa.Column('expiry', sa.DateTime(), nullable=True),
    sa.Column('file_date', sa.DateTime(), nullable=False),
    sa.Column('imported_at', sa.DateTime(), nullable=False),
    sa.PrimaryKeyConstraint('license_no')
    )
    op.create_index(op.f('ix_bpelsg_engineers_license_type'), 'bpelsg_engineers', ['license_type'], unique=False)
    op.create_index(op.f('ix_bpelsg_engineers_county'), 'bpelsg_engineers', ['county'], unique=False)
    op.create_index(op.f('ix_bpelsg_engineers_status'), 'bpelsg_engineers', ['status'], unique=False)
    op.create_index(op.f('ix_bpelsg_engineers_file_date'), 'bpelsg_engineers', ['file_date'], unique=False)
    op.create_index(op.f('ix_bpelsg_engineers_imported_at'), 'bpelsg_engineers', ['imported_at'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_bpelsg_engineers_imported_at'), table_name='bpelsg_engineers')
    op.drop_index(op.f('ix_bpelsg_engineers_file_date'), table_name='bpelsg_engineers')
    op.drop_index(op.f('ix_bpelsg_engineers_status'), table_name='bpelsg_engineers')
    op.drop_index(op.f('ix_bpelsg_engineers_county'), table_name='bpelsg_engineers')
    op.drop_index(op.f('ix_bpelsg_engineers_license_type'), table_name='bpelsg_engineers')
    op.drop_table('bpelsg_engineers')
