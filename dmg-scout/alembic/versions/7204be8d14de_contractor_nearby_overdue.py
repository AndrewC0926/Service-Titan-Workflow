"""contractors.nearby_overdue_count

The owner-direct replacement-lead board (app.web.main:/replacement-leads)
needs "how many OVERDUE buildings sit near this contractor" -- a different
question from the existing nearby_replacement_candidates/nearby_urgency_score
(the general replacement_candidate population, at whatever radius
match_contractors was last run with). Separate columns, not a repurposed
meaning for the existing ones, computed by the new
app.contractors.match_contractors_overdue at contractors.ranking_radius_miles
by default -- see that field's own docstring in app/models.py for why the
wider default_radius_miles is the wrong radius for this board.

Revision ID: 7204be8d14de
Revises: 4151c97f6ca6
Create Date: 2026-08-24 00:00:00.000000
"""
import sqlalchemy as sa

from alembic import op

revision = '7204be8d14de'
down_revision = '4151c97f6ca6'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('contractors', sa.Column('nearby_overdue_count', sa.Integer(), nullable=True))
    op.add_column('contractors', sa.Column('nearby_overdue_radius_miles', sa.Float(), nullable=True))
    op.add_column('contractors', sa.Column('nearby_overdue_computed_at', sa.DateTime(), nullable=True))


def downgrade() -> None:
    op.drop_column('contractors', 'nearby_overdue_computed_at')
    op.drop_column('contractors', 'nearby_overdue_radius_miles')
    op.drop_column('contractors', 'nearby_overdue_count')
