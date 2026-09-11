#!/usr/bin/env python3
"""One-off verification for the firms.name_norm backfill (migration
003d0085e1ae): confirms uq_firm_norm holds on whatever DATABASE_URL this
process sees. A bare script path, not a `sh -c "..."` one-liner -- see
scripts/run-pipeline.sh's own header comment for why: Render's dockerCommand/
job startCommand tokenization does not preserve a multi-word quoted string
intact, so any inline command longer than one bare token fails with
"File name too long" before it ever reaches a shell.
"""
import os

from sqlalchemy import create_engine, text

engine = create_engine(os.environ["DATABASE_URL"])
with engine.connect() as conn:
    rows = conn.execute(text(
        "SELECT name_norm, count(*) FROM firms GROUP BY name_norm HAVING count(*) > 1"
    )).fetchall()

print(f"COLLISION_ROWS: {len(rows)}")
for row in rows:
    print(row)
