"""mcp oauth tables

Three tables backing the single-user OAuth 2.1 authorization server for the
MCP connector (app/mcp_auth.py): registered clients (Dynamic Client
Registration), short-lived authorization codes, and access tokens. DB-backed
rather than in-memory so a Render restart doesn't silently log Claude out —
see app/mcp_auth.py's module docstring.

Revision ID: b3d7e1a94c62
Revises: f2a6c918d3e7
Create Date: 2026-08-07 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa
import sqlmodel


revision = 'b3d7e1a94c62'
down_revision = 'f2a6c918d3e7'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('mcp_oauth_clients',
    sa.Column('client_id', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
    sa.Column('data', sa.JSON(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.PrimaryKeyConstraint('client_id')
    )
    op.create_table('mcp_auth_codes',
    sa.Column('code', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
    sa.Column('data', sa.JSON(), nullable=False),
    sa.Column('expires_at', sa.Float(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.PrimaryKeyConstraint('code')
    )
    op.create_index(op.f('ix_mcp_auth_codes_expires_at'), 'mcp_auth_codes', ['expires_at'], unique=False)
    op.create_table('mcp_access_tokens',
    sa.Column('token', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
    sa.Column('data', sa.JSON(), nullable=False),
    sa.Column('expires_at', sa.Float(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.PrimaryKeyConstraint('token')
    )
    op.create_index(op.f('ix_mcp_access_tokens_expires_at'), 'mcp_access_tokens', ['expires_at'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_mcp_access_tokens_expires_at'), table_name='mcp_access_tokens')
    op.drop_table('mcp_access_tokens')
    op.drop_index(op.f('ix_mcp_auth_codes_expires_at'), table_name='mcp_auth_codes')
    op.drop_table('mcp_auth_codes')
    op.drop_table('mcp_oauth_clients')
