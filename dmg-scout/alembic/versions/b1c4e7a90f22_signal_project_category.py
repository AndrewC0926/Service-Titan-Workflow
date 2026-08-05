"""category on signals and projects: data_center | industrial | other

Two boards, one pipeline. Triage now classifies instead of filtering to a
boolean, so a new industrial building is kept and routed rather than dropped.

Backfill choice: existing rows predate the classifier and every one of them was
admitted by a data-center-only triage, so they are stamped `data_center`. That is
true of the surviving corpus at the time of this migration. New rows get their
category from triage.

Revision ID: b1c4e7a90f22
Revises: 309c1285d582
Create Date: 2026-08-05 09:04:11.000000
"""
from alembic import op
import sqlalchemy as sa

revision = 'b1c4e7a90f22'
down_revision = '309c1285d582'
branch_labels = None
depends_on = None

CATEGORY = sa.Enum('data_center', 'industrial', 'other', name='category')


def upgrade() -> None:
    bind = op.get_bind()
    CATEGORY.create(bind, checkfirst=True)

    # server_default lets the ALTER fill existing rows without a table rewrite;
    # it is dropped afterwards so the application default is the only default.
    op.add_column('signals', sa.Column(
        'category', CATEGORY, nullable=False, server_default='data_center'))
    op.add_column('projects', sa.Column(
        'category', CATEGORY, nullable=False, server_default='data_center'))
    op.create_index(op.f('ix_signals_category'), 'signals', ['category'], unique=False)
    op.create_index(op.f('ix_projects_category'), 'projects', ['category'], unique=False)
    op.alter_column('signals', 'category', server_default=None)
    op.alter_column('projects', 'category', server_default=None)


def downgrade() -> None:
    op.drop_index(op.f('ix_projects_category'), table_name='projects')
    op.drop_index(op.f('ix_signals_category'), table_name='signals')
    op.drop_column('projects', 'category')
    op.drop_column('signals', 'category')
    CATEGORY.drop(op.get_bind(), checkfirst=True)
