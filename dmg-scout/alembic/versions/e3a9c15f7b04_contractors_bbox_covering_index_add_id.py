"""contractors bbox covering index: add id

nearest_mechanical_contractor_bulk's SQL fast path (app/contractors.py)
needs each candidate's real `id` to feed into the nearest-per-building
query, but ix_contractors_bbox_covering (d84f7a2c1e63) only carries
business_name/business_phone/classifications in its INCLUDE list --
selecting id alongside those dropped straight back to a sequential scan
(confirmed via EXPLAIN ANALYZE BUFFERS, 2026-08-18: 300ms, same as before
that index existed), since Postgres can't return a column that isn't in
the index without a heap fetch per row.

Replaces the index with one that also includes id, so the candidate fetch
stays an index-only scan. Built CONCURRENTLY, old index dropped
CONCURRENTLY after the new one is live.

Revision ID: e3a9c15f7b04
Revises: d84f7a2c1e63
Create Date: 2026-08-18 00:00:00.000000
"""
from alembic import op


revision = 'e3a9c15f7b04'
down_revision = 'd84f7a2c1e63'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.create_index(
            'ix_contractors_bbox_covering_v2',
            'contractors',
            ['latitude', 'longitude'],
            postgresql_include=['id', 'business_name', 'business_phone', 'classifications'],
            postgresql_concurrently=True,
            if_not_exists=True,
        )
        op.drop_index(
            'ix_contractors_bbox_covering',
            table_name='contractors',
            postgresql_concurrently=True,
            if_exists=True,
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.create_index(
            'ix_contractors_bbox_covering',
            'contractors',
            ['latitude', 'longitude'],
            postgresql_include=['business_name', 'business_phone', 'classifications'],
            postgresql_concurrently=True,
            if_not_exists=True,
        )
        op.drop_index(
            'ix_contractors_bbox_covering_v2',
            table_name='contractors',
            postgresql_concurrently=True,
            if_exists=True,
        )
