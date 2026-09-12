"""Create opportunities and reason_blocks tables (Block 3, Master Plan v3.2)

Additive only -- no rename, no touch, of any existing table. Schema only,
no data. See app.models.Opportunity/ReasonBlock for the full field-by-field
explanation, and docs/BUILD-PLAN.md's Block 3 entry for the object-model
mapping report this pair of tables was built from.

Verified rollback: downgrade() drops both tables (reason_blocks first, its
own FK depends on opportunities) and both enum types, checked by actually
running upgrade -> downgrade -> upgrade against the local DB before this
migration was committed -- not merely written by the same pattern as
its siblings.

Revision ID: d2e8f4a91b56
Revises: c1a4f6d2e9b0
Create Date: 2026-09-14 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = 'd2e8f4a91b56'
down_revision = 'c1a4f6d2e9b0'
branch_labels = None
depends_on = None

# postgresql.ENUM, not generic sa.Enum -- create_type=False on a generic
# sa.Enum does not survive dialect adaptation to the postgres impl (hit this
# directly: it still emitted "CREATE TYPE ... AS ENUM ()" during
# op.create_table and collided with the explicit .create() call below).
# postgresql.ENUM(..., create_type=False) is the precedent already used by
# 81f4860b6471_field_intel.py for the same reuse-an-existing-type case.
OPPORTUNITY_STAGE = postgresql.ENUM(
    'identified', 'contacted', 'engaged', 'quoted', 'won', 'lost',
    name='opportunitystage')

WHY_KIND = postgresql.ENUM('them', 'now', 'win', name='whykind')

REASON_STRENGTH = postgresql.ENUM('Strong', 'Weak', 'ABSTAIN', name='reasonstrength')

OPPORTUNITY_STAGE_COL = postgresql.ENUM(
    'identified', 'contacted', 'engaged', 'quoted', 'won', 'lost',
    name='opportunitystage', create_type=False)
WHY_KIND_COL = postgresql.ENUM('them', 'now', 'win', name='whykind', create_type=False)
REASON_STRENGTH_COL = postgresql.ENUM('Strong', 'Weak', 'ABSTAIN', name='reasonstrength', create_type=False)
PEN_HOLDER_ROLE_COL = postgresql.ENUM(
    'consulting_me', 'design_builder', 'owner_standards', 'unknown', 'ABSTAIN',
    name='penholderrole', create_type=False)


def upgrade() -> None:
    bind = op.get_bind()
    OPPORTUNITY_STAGE.create(bind, checkfirst=True)
    WHY_KIND.create(bind, checkfirst=True)
    REASON_STRENGTH.create(bind, checkfirst=True)

    op.create_table(
        'opportunities',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('account_id', sa.Integer(), sa.ForeignKey('accounts.id'), nullable=True),
        sa.Column('building_id', sa.Integer(), sa.ForeignKey('retrofit_buildings.id'), nullable=True),
        sa.Column('contact_id', sa.Integer(), sa.ForeignKey('contacts.id'), nullable=True),
        sa.Column('signal_id', sa.Integer(), sa.ForeignKey('signals.id'), nullable=False),
        sa.Column('line_id', sa.Integer(), sa.ForeignKey('product_lines.id'), nullable=True),
        # *_COL variants (create_type=False): the type is created explicitly
        # above (OPPORTUNITY_STAGE.create/etc, checkfirst=True) --
        # op.create_table's own implicit CREATE TYPE would otherwise collide
        # with that explicit create ("type already exists").
        sa.Column('stage', OPPORTUNITY_STAGE_COL,
                  nullable=False, server_default='identified'),
        # penholderrole already exists (WS3.1, f4b8c1a92e07) -- reused, not recreated.
        sa.Column('pen_holder', PEN_HOLDER_ROLE_COL,
                  nullable=False, server_default='ABSTAIN'),
        sa.Column('next_action', sa.String(), nullable=True),
        sa.Column('last_touch', sa.DateTime(), nullable=True),
        sa.Column('netsuite_opportunity_id', sa.Integer(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
    )
    for col in ('account_id', 'building_id', 'contact_id', 'signal_id', 'line_id',
               'stage', 'pen_holder', 'last_touch', 'netsuite_opportunity_id'):
        op.create_index(op.f(f'ix_opportunities_{col}'), 'opportunities', [col], unique=False)
    op.alter_column('opportunities', 'stage', server_default=None)
    op.alter_column('opportunities', 'pen_holder', server_default=None)

    op.create_table(
        'reason_blocks',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('opportunity_id', sa.Integer(), sa.ForeignKey('opportunities.id'), nullable=False),
        sa.Column('why_kind', WHY_KIND_COL, nullable=False),
        sa.Column('strength', REASON_STRENGTH_COL,
                  nullable=False, server_default='ABSTAIN'),
        sa.Column('evidence', sa.Text(), nullable=False, server_default=''),
        sa.Column('source', sa.String(), nullable=True),
        sa.Column('source_url', sa.String(), nullable=True),
        sa.Column('computed_at', sa.DateTime(), nullable=False),
        sa.Column('do_person', sa.String(), nullable=True),
        sa.Column('do_ask', sa.String(), nullable=True),
        sa.Column('one_sentence', sa.String(), nullable=True),
        sa.UniqueConstraint('opportunity_id', 'why_kind', name='uq_reason_block_opportunity_why'),
    )
    for col in ('opportunity_id', 'why_kind', 'strength'):
        op.create_index(op.f(f'ix_reason_blocks_{col}'), 'reason_blocks', [col], unique=False)
    op.alter_column('reason_blocks', 'strength', server_default=None)
    op.alter_column('reason_blocks', 'evidence', server_default=None)


def downgrade() -> None:
    op.drop_index(op.f('ix_reason_blocks_strength'), table_name='reason_blocks')
    op.drop_index(op.f('ix_reason_blocks_why_kind'), table_name='reason_blocks')
    op.drop_index(op.f('ix_reason_blocks_opportunity_id'), table_name='reason_blocks')
    op.drop_table('reason_blocks')

    for col in ('netsuite_opportunity_id', 'last_touch', 'pen_holder', 'stage', 'line_id',
               'signal_id', 'contact_id', 'building_id', 'account_id'):
        op.drop_index(op.f(f'ix_opportunities_{col}'), table_name='opportunities')
    op.drop_table('opportunities')

    bind = op.get_bind()
    REASON_STRENGTH.drop(bind, checkfirst=True)
    WHY_KIND.drop(bind, checkfirst=True)
    OPPORTUNITY_STAGE.drop(bind, checkfirst=True)
