"""projects: delivery_method_class, pen_holder_role (WS3.1)

Two new enum columns, both ABSTAIN by default -- see
app.models.DeliveryMethodClass's own docstring for why this is a SEPARATE
column from the pre-existing `projects.delivery_method` (plain varchar,
LLM-extracted) rather than a rename or a type change on it: that column is
actively read by app.call_target's live R2 rule and populated by the
universal LLM extraction loop (app.pipeline.extract), and this migration
does not touch it. Existing rows get ABSTAIN, same "never attempted" meaning
new rows get before app.pipeline.procurement_delivery ever classifies them --
consistent with c2d5f8b31a44's own precedent (facility_type stamped
`unknown` on existing rows for the same "extracted before this field
existed" reason).

Revision ID: f4b8c1a92e07
Revises: 003d0085e1ae
Create Date: 2026-09-11 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa

revision = 'f4b8c1a92e07'
down_revision = '003d0085e1ae'
branch_labels = None
depends_on = None

DELIVERY_METHOD_CLASS = sa.Enum(
    'design_bid_build', 'design_build_gc', 'design_build_trade',
    'progressive_design_build', 'p3', 'cmar', 'unknown', 'ABSTAIN',
    name='deliverymethodclass')

PEN_HOLDER_ROLE = sa.Enum(
    'consulting_me', 'design_builder', 'owner_standards', 'unknown', 'ABSTAIN',
    name='penholderrole')


def upgrade() -> None:
    bind = op.get_bind()
    DELIVERY_METHOD_CLASS.create(bind, checkfirst=True)
    PEN_HOLDER_ROLE.create(bind, checkfirst=True)

    op.add_column('projects', sa.Column(
        'delivery_method_class', DELIVERY_METHOD_CLASS, nullable=False, server_default='ABSTAIN'))
    op.create_index(op.f('ix_projects_delivery_method_class'), 'projects',
                    ['delivery_method_class'], unique=False)
    op.alter_column('projects', 'delivery_method_class', server_default=None)

    op.add_column('projects', sa.Column(
        'pen_holder_role', PEN_HOLDER_ROLE, nullable=False, server_default='ABSTAIN'))
    op.create_index(op.f('ix_projects_pen_holder_role'), 'projects',
                    ['pen_holder_role'], unique=False)
    op.alter_column('projects', 'pen_holder_role', server_default=None)


def downgrade() -> None:
    op.drop_index(op.f('ix_projects_pen_holder_role'), table_name='projects')
    op.drop_column('projects', 'pen_holder_role')
    op.drop_index(op.f('ix_projects_delivery_method_class'), table_name='projects')
    op.drop_column('projects', 'delivery_method_class')

    bind = op.get_bind()
    PEN_HOLDER_ROLE.drop(bind, checkfirst=True)
    DELIVERY_METHOD_CLASS.drop(bind, checkfirst=True)
