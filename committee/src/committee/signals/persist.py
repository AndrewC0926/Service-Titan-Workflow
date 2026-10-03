"""Side effects of a screen: signal rows to the lake, a journal entry, Lazy Prices diffs.

Signal rows (``signals`` table): one per universe member and signal, with
``asof`` = the as-of date and ``event_time`` = ``known_time`` = the as-of timestamp
(a signal computed as of T is knowable at T). Rows: every raw signal with its
within-sector z-score, plus ``composite`` (value = composite, zscore null),
``cluster_buy`` (1/0), ``volatility_1y`` and ``beta_1y`` (sizing inputs).
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from typing import Any

from committee.data.lake import Lake, new_ingest_id
from committee.journal.store import Journal, JournalEntry
from committee.signals.screen import ScreenResult, ScreenRow

SIGNALS_SOURCE = "committee.signals"


def signal_rows(result: ScreenResult) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    t, d = result.asof, result.asof_date

    def add(sid: str, name: str, value: float | None, z: float | None) -> None:
        rows.append(
            {
                "security_id": sid,
                "signal_name": name,
                "asof": d,
                "value": value,
                "zscore": z,
                "event_time": t,
                "known_time": t,
            }
        )

    for r in result.candidates:
        for name, v in r.signals.items():
            add(r.security_id, name, v, r.zscores.get(name))
        add(r.security_id, "composite", r.composite, None)
        add(r.security_id, "cluster_buy", 1.0 if r.flags["cluster_buy"] else 0.0, None)
        for m in ("volatility_1y", "beta_1y"):
            add(r.security_id, m, r.metrics.get(m), None)
    return rows


def persist_signals(lake: Lake, result: ScreenResult) -> int:
    rows = signal_rows(result)
    if not rows:
        return 0
    return lake.write("signals", rows, source=SIGNALS_SOURCE, ingest_id=new_ingest_id())


def write_lazy_prices_diffs(result: ScreenResult, out_dir: Path) -> list[Path]:
    """One JSON per security for the Filings agent: score, per-item similarity, diff."""
    paths: list[Path] = []
    base = out_dir / result.asof_date.isoformat()
    for sid, lp in sorted(result.lazy_prices.items()):
        base.mkdir(parents=True, exist_ok=True)
        p = base / f"{sid}.json"
        p.write_text(json.dumps(dataclasses.asdict(lp), indent=2, sort_keys=True))
        paths.append(p)
    return paths


def _brief(r: ScreenRow) -> dict[str, Any]:
    return {
        "security_id": r.security_id,
        "ticker": r.ticker,
        "sector": r.sector,
        "bucket": r.bucket,
        "composite": round(r.composite, 6),
        "reason": r.shortlist_reason,
        "contributions": {k: round(v, 6) for k, v in r.contributions.items() if v != 0.0},
        "flags": {
            k: r.flags[k] for k in ("cluster_buy", "recent_cluster_buy", "high_short_interest")
        },
    }


def journal_payload(result: ScreenResult, weights: dict[str, float]) -> dict[str, Any]:
    reasons: dict[str, int] = {}
    for why in result.excluded.values():
        key = why.split(":")[0].split(" (")[0]
        reasons[key] = reasons.get(key, 0) + 1
    return {
        "asof": result.asof.isoformat(),
        "universe_size": result.universe_size,
        "shortlist_size": len(result.shortlist),
        "shortlist": [_brief(r) for r in result.shortlist],
        "excluded_counts": dict(sorted(reasons.items())),
        "removed": sorted(
            sid
            for sid, why in result.excluded.items()
            if why in ("holding_under_review", "wash_sale_block")
        ),
        "weights": weights,
        "earnings_revision_enabled": result.earnings_revision_enabled,
    }


def journal_screen(
    journal: Journal, result: ScreenResult, weights: dict[str, float]
) -> JournalEntry:
    return journal.append("screen", journal_payload(result, weights))
