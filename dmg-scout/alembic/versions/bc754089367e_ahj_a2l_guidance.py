"""AHJ A2L register (ahj_a2l_guidance)

New table -- see app.models.AhjA2lGuidance and app/pipeline/ahj_a2l.py's
module docstring for the full design (jurisdiction as the primary key,
status is a plain string -- deliberately not a native Postgres enum, see
that model's own docstring for why -- idempotent load on jurisdiction).

Revision ID: bc754089367e
Revises: abc952f1fcde
Create Date: 2026-09-11 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa
import sqlmodel


revision = 'bc754089367e'
down_revision = 'abc952f1fcde'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('ahj_a2l_guidance',
    sa.Column('jurisdiction', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
    sa.Column('jurisdiction_type', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
    sa.Column('county', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
    sa.Column('status', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
    sa.Column('ashrae_15_edition', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
    sa.Column('ashrae_15_2_edition', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
    sa.Column('ashrae_34_edition', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
    sa.Column('addendum_a_shaft_alt', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
    sa.Column('addenda_accepted', sa.Text(), nullable=True),
    sa.Column('edvc_regardless_of_charge', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
    sa.Column('a1_resubmittal_rule', sa.Text(), nullable=True),
    sa.Column('express_permit_note', sa.Text(), nullable=True),
    sa.Column('doc_title', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
    sa.Column('doc_number', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
    sa.Column('doc_date', sa.DateTime(), nullable=True),
    sa.Column('source_url', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
    sa.Column('checked_at', sa.DateTime(), nullable=True),
    sa.Column('notes', sa.Text(), nullable=True),
    sa.PrimaryKeyConstraint('jurisdiction')
    )
    op.create_index(op.f('ix_ahj_a2l_guidance_jurisdiction_type'), 'ahj_a2l_guidance', ['jurisdiction_type'], unique=False)
    op.create_index(op.f('ix_ahj_a2l_guidance_county'), 'ahj_a2l_guidance', ['county'], unique=False)
    op.create_index(op.f('ix_ahj_a2l_guidance_status'), 'ahj_a2l_guidance', ['status'], unique=False)
    op.create_index(op.f('ix_ahj_a2l_guidance_checked_at'), 'ahj_a2l_guidance', ['checked_at'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_ahj_a2l_guidance_checked_at'), table_name='ahj_a2l_guidance')
    op.drop_index(op.f('ix_ahj_a2l_guidance_status'), table_name='ahj_a2l_guidance')
    op.drop_index(op.f('ix_ahj_a2l_guidance_county'), table_name='ahj_a2l_guidance')
    op.drop_index(op.f('ix_ahj_a2l_guidance_jurisdiction_type'), table_name='ahj_a2l_guidance')
    op.drop_table('ahj_a2l_guidance')
