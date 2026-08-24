"""outreach.account_id

Outreach was project_id-only: every row had to name a live Scout project.
That's right for the project-pipeline side (voice capture, Fathom sync,
the project page's own log) but wrong for a contractor/GC account -- a rep
calls an account about their business generally, not about one specific
job, and most accounts (see app.contractors.match_account_to_cslb) have no
live Scout project naming them at all. The account detail page
(app.web.main:/account/{id}) needs somewhere to log that outreach.

Adds nullable outreach.account_id (FK to accounts.id) and relaxes
outreach.project_id to nullable so an account-only row doesn't need a
fabricated project. app.outreach.log_outreach() enforces "at least one of
project_id/account_id" at the application layer -- the same place
"project_id is required: Outreach has no unresolved-entity concept" was
already enforced (app/web/main.py's capture_confirm), not a new DB
constraint; this schema carries no CHECK constraints anywhere and this
migration doesn't start.

Revision ID: 4151c97f6ca6
Revises: ce6394cf68a6
Create Date: 2026-08-24 00:00:00.000000
"""
import sqlalchemy as sa

from alembic import op

revision = '4151c97f6ca6'
down_revision = 'ce6394cf68a6'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('outreach', sa.Column('account_id', sa.Integer(), nullable=True))
    op.create_index(op.f('ix_outreach_account_id'), 'outreach', ['account_id'], unique=False)
    op.create_foreign_key('fk_outreach_account_id_accounts', 'outreach', 'accounts', ['account_id'], ['id'])
    op.alter_column('outreach', 'project_id', existing_type=sa.Integer(), nullable=True)


def downgrade() -> None:
    op.alter_column('outreach', 'project_id', existing_type=sa.Integer(), nullable=False)
    op.drop_constraint('fk_outreach_account_id_accounts', 'outreach', type_='foreignkey')
    op.drop_index(op.f('ix_outreach_account_id'), table_name='outreach')
    op.drop_column('outreach', 'account_id')
