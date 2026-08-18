"""retrofit board aggregate covering index

The /retrofit board's single conditional-aggregation query (app/web/main.py's
retrofit_board(), the query that replaced 6 separate COUNT/MAX round trips on
2026-08-17) was re-profiled at 2.22s of the page's ~6s total. EXPLAIN
(ANALYZE, BUFFERS) on the live query showed a sequential scan of the entire
retrofit_buildings table (16,587 buffers touched, 130MB heap, ~62% read from
disk) even though the query only needs population/county to filter and
address/ebewe_matched/last_sale_date to aggregate -- the other ~50 columns on
every row (several wide basis/URL text fields, confirmed via pg_stats:
service_life_basis avg 492 bytes, estimated_tons_basis avg 205 bytes, etc.)
are dead weight for this specific query but still ride along on every heap
page a sequential scan touches.

This covering index lets Postgres answer the aggregate with an index-only
scan instead: the leading (population, county) columns match the query's
WHERE clause, and the INCLUDE columns hold everything the CASE expressions
reference, so no heap fetch is needed at all once the visibility map is
current (autoanalyze last ran today per pg_stat_user_tables).

Built CONCURRENTLY -- this is a live production table serving read traffic
continuously, and a plain CREATE INDEX would take an ACCESS EXCLUSIVE lock
for the duration of the build.

Revision ID: c2e9a4f61b7d
Revises: b6d4a91f3c58
Create Date: 2026-08-17 00:00:00.000000
"""
from alembic import op


revision = 'c2e9a4f61b7d'
down_revision = '4954b1c4df3b'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.create_index(
            'ix_retrofit_buildings_agg_covering',
            'retrofit_buildings',
            ['population', 'county'],
            postgresql_include=['address', 'ebewe_matched', 'last_sale_date'],
            postgresql_concurrently=True,
            if_not_exists=True,
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.drop_index(
            'ix_retrofit_buildings_agg_covering',
            table_name='retrofit_buildings',
            postgresql_concurrently=True,
            if_exists=True,
        )
