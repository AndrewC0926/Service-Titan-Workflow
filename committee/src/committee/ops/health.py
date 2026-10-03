"""Health checks: journal chain, order-blocking flags, data freshness, disk, open P1s."""

from __future__ import annotations

import datetime as dt
import shutil
from dataclasses import dataclass
from pathlib import Path

from committee.journal.store import Journal
from committee.ops.flags import Flags


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str


def check_health(journal: Journal, flags: Flags, var_dir: Path, now: dt.datetime) -> list[Check]:
    out: list[Check] = []
    rep = journal.verify()
    out.append(
        Check(
            "journal", rep.ok, f"{rep.entries} entries" if rep.ok else (rep.first_break or "broken")
        )
    )
    blocked = flags.orders_blocked()
    out.append(Check("orders_enabled", blocked is None, blocked or "submission allowed"))
    last = journal.latest("ingest_summary")
    if last is None:
        out.append(Check("ingest_fresh", False, "no ingest has run"))
    else:
        age = now - last.created_at
        out.append(
            Check(
                "ingest_fresh",
                age <= dt.timedelta(days=2),
                f"last ingest {age.days}d {age.seconds // 3600}h ago",
            )
        )
    since = now - dt.timedelta(days=60)
    p1 = [
        e
        for e in journal.entries("incident")
        if e.payload.get("level") == "P1" and e.created_at >= since
    ]
    out.append(Check("no_p1_60d", not p1, f"{len(p1)} P1 incidents in 60 days"))
    var_dir.mkdir(parents=True, exist_ok=True)
    free = shutil.disk_usage(var_dir).free
    out.append(Check("disk", free > 1 << 30, f"{free / (1 << 30):.1f} GiB free"))
    return out
