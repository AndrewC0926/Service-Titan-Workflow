"""retrofit_buildings.is_active (Hotfix: rebuild upsert, not delete-and-
reinsert)

build_retrofit_buildings/find_replacement_candidates used to DELETE every
row in a population and reinsert fresh autoincrement ids on every run,
silently orphaning Opportunity.building_id/DecisionNote.building_id FKs
anchored on a building that still existed, just under a new id. Both
rebuilds now upsert keyed on apn, preserving id; is_active=False is what
happens to a row whose apn genuinely stopped appearing, instead of a
DELETE that would either violate the FK outright or (worse) silently
repoint it at whatever unrelated row Postgres handed the reused id to
next.

Every existing row backfills to is_active=True (server_default true) --
every row on disk today survived to the very next rebuild by construction
(this migration ships alongside the fix, so nothing has been deactivated
yet); there is no historical "this used to be deleted" state to recover.

Verified rollback: downgrade() drops the column, checked by running
upgrade -> downgrade -> upgrade against a real schema before this
migration was committed.

Revision ID: a9c47e2b1f83
Revises: 62e0b21b7630
Create Date: 2026-09-13 23:05:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = 'a9c47e2b1f83'
down_revision = '62e0b21b7630'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('retrofit_buildings', sa.Column('is_active', sa.Boolean(), nullable=False,
                                                   server_default=sa.true()))
    op.create_index(op.f('ix_retrofit_buildings_is_active'), 'retrofit_buildings', ['is_active'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_retrofit_buildings_is_active'), table_name='retrofit_buildings')
    op.drop_column('retrofit_buildings', 'is_active')
