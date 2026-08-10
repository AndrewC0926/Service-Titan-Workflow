"""line card view fields on product lines

Adds product_lines.building_role/markets_served plus the fields the new
/lines and /line/{id} views need: competes_with, OSHPD OSP / UFC 4-010-06 /
AHRI / country-of-manufacture eligibility flags (each with its own basis
column), lead time (low/high weeks + basis), and limitations. All new
scalar/basis columns are nullable and default to null/unfilled -- the same
discipline heat_rejection_mode already established: no inferred default,
confirmed-by-name-and-date only. building_role gets a non-null server
default so existing rows resolve to something sane before the next
`scout seed-lines` reseeds them for real; markets_served defaults to an
empty JSON array for the same reason. See app/models.py's ProductLine
docstring and app/accounts.py's CATEGORY_TO_ROLE/MARKETS_BY_LINE.

Revision ID: 66623c2f0191
Revises: 9c4b2e6a71fd
Create Date: 2026-08-09 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = '66623c2f0191'
down_revision = '9c4b2e6a71fd'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('product_lines', sa.Column(
        'building_role', sa.String(), nullable=False, server_default='heating_specialty'))
    op.add_column('product_lines', sa.Column(
        'markets_served', sa.JSON(), nullable=False, server_default='[]'))

    op.add_column('product_lines', sa.Column('competes_with', sa.String(), nullable=True))
    op.add_column('product_lines', sa.Column('competes_with_basis', sa.String(), nullable=True))

    op.add_column('product_lines', sa.Column('oshpd_osp', sa.Boolean(), nullable=True))
    op.add_column('product_lines', sa.Column('oshpd_osp_basis', sa.String(), nullable=True))
    op.add_column('product_lines', sa.Column('ufc_4_010_06', sa.Boolean(), nullable=True))
    op.add_column('product_lines', sa.Column('ufc_4_010_06_basis', sa.String(), nullable=True))
    op.add_column('product_lines', sa.Column('ahri_certified', sa.Boolean(), nullable=True))
    op.add_column('product_lines', sa.Column('ahri_certified_basis', sa.String(), nullable=True))
    op.add_column('product_lines', sa.Column('country_of_manufacture', sa.String(), nullable=True))
    op.add_column('product_lines', sa.Column('country_of_manufacture_basis', sa.String(), nullable=True))

    op.add_column('product_lines', sa.Column('lead_time_weeks_low', sa.Integer(), nullable=True))
    op.add_column('product_lines', sa.Column('lead_time_weeks_high', sa.Integer(), nullable=True))
    op.add_column('product_lines', sa.Column('lead_time_basis', sa.String(), nullable=True))

    op.add_column('product_lines', sa.Column('limitations', sa.String(), nullable=True))

    op.create_index(op.f('ix_product_lines_building_role'), 'product_lines', ['building_role'])


def downgrade() -> None:
    op.drop_index(op.f('ix_product_lines_building_role'), table_name='product_lines')
    op.drop_column('product_lines', 'limitations')
    op.drop_column('product_lines', 'lead_time_basis')
    op.drop_column('product_lines', 'lead_time_weeks_high')
    op.drop_column('product_lines', 'lead_time_weeks_low')
    op.drop_column('product_lines', 'country_of_manufacture_basis')
    op.drop_column('product_lines', 'country_of_manufacture')
    op.drop_column('product_lines', 'ahri_certified_basis')
    op.drop_column('product_lines', 'ahri_certified')
    op.drop_column('product_lines', 'ufc_4_010_06_basis')
    op.drop_column('product_lines', 'ufc_4_010_06')
    op.drop_column('product_lines', 'oshpd_osp_basis')
    op.drop_column('product_lines', 'oshpd_osp')
    op.drop_column('product_lines', 'competes_with_basis')
    op.drop_column('product_lines', 'competes_with')
    op.drop_column('product_lines', 'markets_served')
    op.drop_column('product_lines', 'building_role')
