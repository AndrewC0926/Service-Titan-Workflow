"""contractors bounding-box covering index

nearest_mechanical_contractor_bulk() (app/contractors.py), used by the
/retrofit board's mobile card view, was profiled at 1.19s of the route's
2.83s total. Its own bounding-box fetch (latitude/longitude range query
against `contractors`) accounted for 0.64s of that -- the largest single
piece -- because `contractors` has no index on latitude/longitude at all,
so the query is a sequential scan (confirmed via EXPLAIN ANALYZE BUFFERS,
2026-08-18: 5,037 buffers touched, ~19,800 of 47,572 rows filtered out row
by row after a full heap read).

Same fix as retrofit_buildings' aggregate query (c2e9a4f61b7d): a covering
index on (latitude, longitude) INCLUDE (business_name, business_phone,
classifications) -- the only columns this query actually reads -- lets
Postgres answer it as an index-only scan instead of a full-table scan.
Built CONCURRENTLY; contractors is read on every /retrofit and
/contractors page load.

Revision ID: d84f7a2c1e63
Revises: c2e9a4f61b7d
Create Date: 2026-08-18 00:00:00.000000
"""
from alembic import op


revision = 'd84f7a2c1e63'
down_revision = 'c2e9a4f61b7d'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.create_index(
            'ix_contractors_bbox_covering',
            'contractors',
            ['latitude', 'longitude'],
            postgresql_include=['business_name', 'business_phone', 'classifications'],
            postgresql_concurrently=True,
            if_not_exists=True,
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.drop_index(
            'ix_contractors_bbox_covering',
            table_name='contractors',
            postgresql_concurrently=True,
            if_exists=True,
        )
