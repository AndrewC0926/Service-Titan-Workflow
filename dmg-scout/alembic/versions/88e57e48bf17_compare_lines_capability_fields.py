"""compare_lines capability fields

Adds four MODEL-level capability facts to product_lines, same discipline as
heat_rejection_mode: latent_load_capability, corrosion_resistance,
redundancy_capable, rigging_constrained_capable -- each with a _verified
boolean and a _basis text field. All null/unverified until someone has
actually stated it and it's confirmed with the factory. Built for
app.compare:compare_lines -- see that module's docstring.

Revision ID: 88e57e48bf17
Revises: a2f9c4e83b17
Create Date: 2026-08-11 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = '88e57e48bf17'
down_revision = '4f454e8b3a42'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('product_lines', sa.Column('latent_load_capability', sa.String(), nullable=True))
    op.add_column('product_lines', sa.Column(
        'latent_load_capability_verified', sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column('product_lines', sa.Column('latent_load_capability_basis', sa.String(), nullable=True))

    op.add_column('product_lines', sa.Column('corrosion_resistance', sa.String(), nullable=True))
    op.add_column('product_lines', sa.Column(
        'corrosion_resistance_verified', sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column('product_lines', sa.Column('corrosion_resistance_basis', sa.String(), nullable=True))

    op.add_column('product_lines', sa.Column('redundancy_capable', sa.Boolean(), nullable=True))
    op.add_column('product_lines', sa.Column(
        'redundancy_capable_verified', sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column('product_lines', sa.Column('redundancy_capable_basis', sa.String(), nullable=True))

    op.add_column('product_lines', sa.Column('rigging_constrained_capable', sa.Boolean(), nullable=True))
    op.add_column('product_lines', sa.Column(
        'rigging_constrained_capable_verified', sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column('product_lines', sa.Column('rigging_constrained_capable_basis', sa.String(), nullable=True))


def downgrade() -> None:
    for col in (
        'rigging_constrained_capable_basis', 'rigging_constrained_capable_verified', 'rigging_constrained_capable',
        'redundancy_capable_basis', 'redundancy_capable_verified', 'redundancy_capable',
        'corrosion_resistance_basis', 'corrosion_resistance_verified', 'corrosion_resistance',
        'latent_load_capability_basis', 'latent_load_capability_verified', 'latent_load_capability',
    ):
        op.drop_column('product_lines', col)
