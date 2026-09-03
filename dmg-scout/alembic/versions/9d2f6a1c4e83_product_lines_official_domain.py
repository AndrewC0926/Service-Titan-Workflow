"""product_lines.official_domain(_source|_url)

The one exception to ProductLine's "seeded from config.yaml, edited there"
convention -- app.pipeline.line_pitch.discover_domain_via_web_search writes
these for a line whose own basis text names no plausible domain, so a real
web_search domain lookup only ever runs once per line, not once per run.
See app/models.py's ProductLine docstring for why.

Revision ID: 9d2f6a1c4e83
Revises: 4b7e1c8a2f36
Create Date: 2026-09-04 09:00:00.000000
"""
import sqlalchemy as sa
import sqlmodel

from alembic import op

revision = '9d2f6a1c4e83'
down_revision = '4b7e1c8a2f36'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('product_lines',
                  sa.Column('official_domain', sqlmodel.sql.sqltypes.AutoString(), nullable=True))
    op.add_column('product_lines',
                  sa.Column('official_domain_source', sqlmodel.sql.sqltypes.AutoString(), nullable=True))
    op.add_column('product_lines',
                  sa.Column('official_domain_url', sqlmodel.sql.sqltypes.AutoString(), nullable=True))


def downgrade() -> None:
    op.drop_column('product_lines', 'official_domain_url')
    op.drop_column('product_lines', 'official_domain_source')
    op.drop_column('product_lines', 'official_domain')
