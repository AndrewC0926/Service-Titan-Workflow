"""generator critical/house split

Some filings (CEC power filings routinely) state the generator fleet as two
classes: units dedicated to data center critical/IT load, and house units
backing everything else. Stated directly in MW per unit, so these are new
fields rather than reusing generator_hp_each/generator_kw_each — the existing
no-conversion extraction rule would otherwise force either a fabricated hp/kW
figure or a dropped value. See app/pipeline/sizing.py's "gensets_critical"
basis and app/models.py's Signal docstring at these fields.

Revision ID: f2a6c918d3e7
Revises: a1c3f7e92b58
Create Date: 2026-08-07 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = 'f2a6c918d3e7'
down_revision = 'a1c3f7e92b58'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('signals', sa.Column('generator_critical_count', sa.Integer(), nullable=True))
    op.add_column('signals', sa.Column('generator_critical_mw_each', sa.Float(), nullable=True))
    op.add_column('signals', sa.Column('generator_house_count', sa.Integer(), nullable=True))
    op.add_column('signals', sa.Column('generator_house_mw_each', sa.Float(), nullable=True))


def downgrade() -> None:
    op.drop_column('signals', 'generator_house_mw_each')
    op.drop_column('signals', 'generator_house_count')
    op.drop_column('signals', 'generator_critical_mw_each')
    op.drop_column('signals', 'generator_critical_count')
