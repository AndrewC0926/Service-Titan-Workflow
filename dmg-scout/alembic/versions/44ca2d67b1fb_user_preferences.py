"""user_preferences (Block 4C Item 5: "Mode is a preference, not a role")

One row per username that has ever set a preference -- a user who never
has defaults to mode="guide" in application code, never a row inserted
for a default nobody chose. last_radar_visit_at powers Radar's own "what
changed since last visit" panel.

Verified rollback: downgrade() drops the table, checked by running
upgrade -> downgrade -> upgrade against the local DB before this
migration was committed.

Revision ID: 44ca2d67b1fb
Revises: f7fd3159dd22
Create Date: 2026-09-13 11:14:56.901279
"""
from alembic import op
import sqlalchemy as sa
import sqlmodel


revision = '44ca2d67b1fb'
down_revision = 'f7fd3159dd22'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'user_preferences',
        sa.Column('username', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('mode', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('last_radar_visit_at', sa.DateTime(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('username'),
    )
    op.create_index(op.f('ix_user_preferences_mode'), 'user_preferences', ['mode'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_user_preferences_mode'), table_name='user_preferences')
    op.drop_table('user_preferences')
