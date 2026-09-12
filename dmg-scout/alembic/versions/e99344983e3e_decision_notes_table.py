"""Create decision_notes table (Block 4A Item 3, Master Plan v3.6 section 31)

Additive only. Reuses the existing lostreasoncode/outcomesource enum types
(create_type=False -- Item 2's own migration created them) rather than
duplicating either. Verified rollback: downgrade() drops the table and the
four new enum types this migration itself creates, checked by actually
running upgrade -> downgrade -> upgrade against the local DB before this
migration was committed.

Revision ID: e99344983e3e
Revises: 3a3f9fc8ba41
Create Date: 2026-09-13 02:00:00.000000
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = 'e99344983e3e'
down_revision = '3a3f9fc8ba41'
branch_labels = None
depends_on = None

NOTE_TYPE = postgresql.ENUM('won', 'lost', 'quote_lost', 'intel', 'decision', name='notetype')
NOTE_PEN_HOLDER = postgresql.ENUM('engineer', 'contractor', 'owner', 'gc', 'unknown', name='notepenholder')
BASIS_OF_DESIGN = postgresql.ENUM('ours', 'competitor_named', 'open', 'none', name='basisofdesign')
LEAD_SOURCE = postgresql.ENUM(
    'scout_signal', 'relationship', 'inbound', 'rep_originated', 'inside_sales', name='leadsource')
NETSUITE_REF_TYPE = postgresql.ENUM('opportunity', 'project', 'sales_order', name='netsuitereftype')

NOTE_TYPE_COL = postgresql.ENUM(
    'won', 'lost', 'quote_lost', 'intel', 'decision', name='notetype', create_type=False)
NOTE_PEN_HOLDER_COL = postgresql.ENUM(
    'engineer', 'contractor', 'owner', 'gc', 'unknown', name='notepenholder', create_type=False)
BASIS_OF_DESIGN_COL = postgresql.ENUM(
    'ours', 'competitor_named', 'open', 'none', name='basisofdesign', create_type=False)
LEAD_SOURCE_COL = postgresql.ENUM(
    'scout_signal', 'relationship', 'inbound', 'rep_originated', 'inside_sales',
    name='leadsource', create_type=False)
NETSUITE_REF_TYPE_COL = postgresql.ENUM(
    'opportunity', 'project', 'sales_order', name='netsuitereftype', create_type=False)
# Reused from Item 2's migration (3a3f9fc8ba41) -- never recreated.
LOST_REASON_CODE_COL = postgresql.ENUM(name='lostreasoncode', create_type=False)
OUTCOME_SOURCE_COL = postgresql.ENUM(name='outcomesource', create_type=False)


def upgrade() -> None:
    bind = op.get_bind()
    NOTE_TYPE.create(bind, checkfirst=True)
    NOTE_PEN_HOLDER.create(bind, checkfirst=True)
    BASIS_OF_DESIGN.create(bind, checkfirst=True)
    LEAD_SOURCE.create(bind, checkfirst=True)
    NETSUITE_REF_TYPE.create(bind, checkfirst=True)

    op.create_table(
        'decision_notes',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('opportunity_id', sa.Integer(), sa.ForeignKey('opportunities.id'), nullable=True),
        sa.Column('project_id', sa.Integer(), sa.ForeignKey('projects.id'), nullable=True),
        sa.Column('building_id', sa.Integer(), sa.ForeignKey('retrofit_buildings.id'), nullable=True),
        sa.Column('account_id', sa.Integer(), sa.ForeignKey('accounts.id'), nullable=True),
        sa.Column('signal_id', sa.Integer(), sa.ForeignKey('signals.id'), nullable=True),
        sa.Column('netsuite_ref_type', NETSUITE_REF_TYPE_COL, nullable=True),
        sa.Column('netsuite_ref', sa.String(), nullable=True),
        sa.Column('note_type', NOTE_TYPE_COL, nullable=False),
        sa.Column('pen_holder', NOTE_PEN_HOLDER_COL, nullable=False, server_default='unknown'),
        sa.Column('basis_of_design', BASIS_OF_DESIGN_COL, nullable=False, server_default='open'),
        sa.Column('reason_code', LOST_REASON_CODE_COL, nullable=True),
        sa.Column('lead_source', LEAD_SOURCE_COL, nullable=False),
        sa.Column('line', sa.String(), nullable=True),
        sa.Column('competitor_line', sa.String(), nullable=True),
        sa.Column('dollars', sa.Float(), nullable=True),
        sa.Column('free_text', sa.Text(), nullable=False, server_default=''),
        sa.Column('author', sa.String(), nullable=False),
        sa.Column('role', sa.String(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('source', OUTCOME_SOURCE_COL, nullable=False, server_default='web'),
    )
    for col in ('opportunity_id', 'project_id', 'building_id', 'account_id', 'signal_id',
               'netsuite_ref_type', 'netsuite_ref', 'note_type', 'pen_holder', 'basis_of_design',
               'reason_code', 'lead_source', 'author', 'created_at', 'source'):
        op.create_index(op.f(f'ix_decision_notes_{col}'), 'decision_notes', [col], unique=False)
    op.alter_column('decision_notes', 'pen_holder', server_default=None)
    op.alter_column('decision_notes', 'basis_of_design', server_default=None)
    op.alter_column('decision_notes', 'free_text', server_default=None)
    op.alter_column('decision_notes', 'source', server_default=None)


def downgrade() -> None:
    for col in ('source', 'created_at', 'author', 'lead_source', 'reason_code', 'basis_of_design',
               'pen_holder', 'note_type', 'netsuite_ref', 'netsuite_ref_type', 'signal_id',
               'account_id', 'building_id', 'project_id', 'opportunity_id'):
        op.drop_index(op.f(f'ix_decision_notes_{col}'), table_name='decision_notes')
    op.drop_table('decision_notes')

    bind = op.get_bind()
    NETSUITE_REF_TYPE.drop(bind, checkfirst=True)
    LEAD_SOURCE.drop(bind, checkfirst=True)
    BASIS_OF_DESIGN.drop(bind, checkfirst=True)
    NOTE_PEN_HOLDER.drop(bind, checkfirst=True)
    NOTE_TYPE.drop(bind, checkfirst=True)
