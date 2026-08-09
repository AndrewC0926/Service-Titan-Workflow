"""water source risk fields

Adds water-source facts to signals (stated at extraction) and projects
(rolled up in app.pipeline.resolve._absorb): water_source_stated,
water_reclaimed_identified, water_use_efficiency_stated,
water_opposition_stated. Projects also get the derived confidence read,
water_risk_flag/water_risk_basis -- see app/pipeline/waterrisk.py.
Deliberately NOT a binary "water contested" flag; the public objection is
to potable water, not water, and a project on identified recycled/
reclaimed supply is a different risk than one with none on file.

Revision ID: 3f7a9d2c15e8
Revises: 8d3c6f1a92be
Create Date: 2026-08-09 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = '3f7a9d2c15e8'
down_revision = '8d3c6f1a92be'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('signals', sa.Column('water_source_stated', sa.String(), nullable=True))
    op.add_column('signals', sa.Column('water_reclaimed_identified', sa.Boolean(), nullable=True))
    op.add_column('signals', sa.Column('water_use_efficiency_stated', sa.String(), nullable=True))
    op.add_column('signals', sa.Column('water_opposition_stated', sa.Boolean(), nullable=True))

    op.add_column('projects', sa.Column('water_source_stated', sa.String(), nullable=True))
    op.add_column('projects', sa.Column('water_reclaimed_identified', sa.Boolean(), nullable=True))
    op.add_column('projects', sa.Column('water_use_efficiency_stated', sa.String(), nullable=True))
    op.add_column('projects', sa.Column('water_opposition_stated', sa.Boolean(), nullable=True))
    op.add_column('projects', sa.Column('water_risk_flag', sa.String(), nullable=True))
    op.add_column('projects', sa.Column('water_risk_basis', sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column('projects', 'water_risk_basis')
    op.drop_column('projects', 'water_risk_flag')
    op.drop_column('projects', 'water_opposition_stated')
    op.drop_column('projects', 'water_use_efficiency_stated')
    op.drop_column('projects', 'water_reclaimed_identified')
    op.drop_column('projects', 'water_source_stated')

    op.drop_column('signals', 'water_opposition_stated')
    op.drop_column('signals', 'water_use_efficiency_stated')
    op.drop_column('signals', 'water_reclaimed_identified')
    op.drop_column('signals', 'water_source_stated')
