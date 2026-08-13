"""access_log

Dashboard hit logging: id, username, path, method, ip, user_agent, created_at.
Written by app/access_log.py's middleware, not a route handler -- see that
module and AccessLog's docstring in app/models.py.

Revision ID: ddbcb6a149c2
Revises: 28bf8f24cd70
Create Date: 2026-08-13 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = 'ddbcb6a149c2'
down_revision = '28bf8f24cd70'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'access_log',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('username', sa.Text(), nullable=True),
        sa.Column('path', sa.String(), nullable=False),
        sa.Column('method', sa.String(), nullable=False),
        sa.Column('ip', sa.String(), nullable=True),
        sa.Column('user_agent', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
    )
    op.create_index('ix_access_log_path', 'access_log', ['path'])
    op.create_index('ix_access_log_username_created_at', 'access_log', ['username', 'created_at'])


def downgrade() -> None:
    op.drop_index('ix_access_log_username_created_at', table_name='access_log')
    op.drop_index('ix_access_log_path', table_name='access_log')
    op.drop_table('access_log')
