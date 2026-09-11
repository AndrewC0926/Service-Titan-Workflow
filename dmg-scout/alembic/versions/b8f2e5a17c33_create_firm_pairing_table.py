"""Create firm_pairing table (WS2, Build Plan v2.1)

Schema only -- no data. Row shape: design_builder_firm_id, partner_firm_id,
partner_role, source, source_url, observed_date, confidence -- see
app.models.FirmPairing's own docstring for the full field-by-field
explanation. Seeding (the five verified public pairings plus what WS3.2's
document sample surfaced) is done separately by
app.pipeline.firm_pairing.seed_verified_firm_pairings / `scout
seed-firm-pairings`, never from inside a migration -- this repo's other
migrations never import app.* (checked before writing 003d0085e1ae; still
true here), and finding-or-creating a Firm row needs
app.normalize.normalize_company_name, which a migration has no business
importing.

Revision ID: b8f2e5a17c33
Revises: a3d719c04b5e
Create Date: 2026-09-12 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = 'b8f2e5a17c33'
down_revision = 'a3d719c04b5e'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'firm_pairing',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('design_builder_firm_id', sa.Integer(),
                 sa.ForeignKey('firms.id'), nullable=False),
        sa.Column('partner_firm_id', sa.Integer(),
                 sa.ForeignKey('firms.id'), nullable=False),
        sa.Column('partner_role', sa.String(), nullable=False),
        sa.Column('source', sa.String(), nullable=False),
        sa.Column('source_url', sa.String(), nullable=True),
        sa.Column('observed_date', sa.DateTime(), nullable=True),
        sa.Column('confidence', sa.Float(), nullable=False, server_default='1.0'),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.UniqueConstraint('design_builder_firm_id', 'partner_firm_id', 'partner_role',
                            name='uq_firm_pairing'),
    )
    op.create_index(op.f('ix_firm_pairing_design_builder_firm_id'), 'firm_pairing',
                    ['design_builder_firm_id'], unique=False)
    op.create_index(op.f('ix_firm_pairing_partner_firm_id'), 'firm_pairing',
                    ['partner_firm_id'], unique=False)
    op.create_index(op.f('ix_firm_pairing_partner_role'), 'firm_pairing',
                    ['partner_role'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_firm_pairing_partner_role'), table_name='firm_pairing')
    op.drop_index(op.f('ix_firm_pairing_partner_firm_id'), table_name='firm_pairing')
    op.drop_index(op.f('ix_firm_pairing_design_builder_firm_id'), table_name='firm_pairing')
    op.drop_table('firm_pairing')
