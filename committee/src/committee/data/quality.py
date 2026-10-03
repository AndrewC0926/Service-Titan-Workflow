"""Data-quality suite (DESIGN 5), run after every ingest and by ``committee data check``.

Checks:
- row count and freshness per table and source: stale if the last ingest is more
  than the table's allowance in business days before the as-of time (1 for daily
  sources); for prices, also the latest bar date. Weekends are skipped; exchange
  holidays are not modelled, so a run right after a holiday may need a re-run.
- price sanity: no zero/negative prices; a |daily move| > 50% needs a matching
  corporate action on that date, otherwise it is flagged for manual review.
- Form 4 completeness: every Form 4 / 4/A has insider_txns rows, is
  holdings-only (``filings.n_txns == 0``) or is in the dead-letter table.

Every run is journaled as ``dq_report``; on failures the ``notify`` hook (email in
production, a no-op stub by default) receives the report.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

import duckdb
import numpy as np
import pandas as pd

from committee.data.common import to_date, utcnow
from committee.data.lake import Lake
from committee.data.pit import PIT, to_utc
from committee.journal.store import Journal

Status = Literal["pass", "fail", "info"]

# Allowed staleness in business days per table; tables not listed are not checked.
FRESHNESS_BDAYS: dict[str, int] = {
    "security_master": 5,
    "ticker_history": 5,
    "prices_daily": 1,
    "corporate_actions": 1,
    "filings": 1,
    "insider_txns": 1,
    "filing_sections": 1,
    "fundamentals": 1,
    "macro_series": 1,
    "news": 1,
    "factor_returns": 23,
    "risk_indexes": 23,
}
MAX_ABS_MOVE = 0.5
MAX_ITEMS = 50


@dataclass(frozen=True)
class TableStat:
    table: str
    source: str
    rows: int
    last_ingest: str | None
    latest_known_time: str | None
    stale_bdays: int | None
    allowed_bdays: int


@dataclass(frozen=True)
class Check:
    name: str
    status: Status
    detail: str
    items: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class DQReport:
    asof: str
    tables: list[TableStat]
    checks: list[Check]

    @property
    def failures(self) -> list[Check]:
        return [c for c in self.checks if c.status == "fail"]

    @property
    def ok(self) -> bool:
        return not self.failures

    def payload(self) -> dict[str, Any]:
        return {
            "asof": self.asof,
            "ok": self.ok,
            "failures": [c.name for c in self.failures],
            "tables": [asdict(t) for t in self.tables],
            "checks": [{**asdict(c), "items": c.items[:MAX_ITEMS]} for c in self.checks],
        }


Notify = Callable[[DQReport], None]


def no_email(report: DQReport) -> None:
    """Default notify hook: a stub until the email digest is wired up."""


def _ingest_time(ingest_id: str) -> dt.datetime | None:
    try:
        return dt.datetime.strptime(ingest_id[:15], "%Y%m%dT%H%M%S").replace(tzinfo=dt.UTC)
    except ValueError:
        return None


def _day(v: Any) -> dt.date:
    d = to_date(v)
    if d is None:
        raise ValueError("null date")
    return d


def _iso(v: Any) -> str | None:
    return None if v is None or pd.isna(v) else pd.Timestamp(str(v)).isoformat()


def last_runs(journal: Journal) -> dict[str, dt.datetime]:
    """Last successful (or partial) ingest run per source, from ``ingest_summary`` entries."""
    out: dict[str, dt.datetime] = {}
    for e in journal.entries("ingest_summary"):
        p = e.payload
        if isinstance(p, dict) and p.get("status") != "failed" and p.get("source"):
            out[str(p["source"])] = e.created_at
    return out


def _bdays(start: dt.date, end: dt.date) -> int:
    return int(np.busday_count(start, end)) if end > start else 0


def table_stats(
    pit: PIT, asof: dt.datetime, runs: dict[str, dt.datetime] | None = None
) -> list[TableStat]:
    """Rows and last ingest per table and source. A journaled run that wrote nothing
    new still counts as fresh (``runs``), so quiet sources are not flagged stale."""
    runs = runs or {}
    out = []
    for table, allowed in FRESHNESS_BDAYS.items():
        if not pit.has(table):
            continue
        df = pit.query(
            f"SELECT source, count(*) AS n, max(ingest_id) AS last_ingest, "
            f"max(known_time) AS kt FROM {table} WHERE as_of(known_time, $asof) "
            "GROUP BY source ORDER BY source",
            asof,
        )
        for r in df.itertuples(index=False):
            last = _ingest_time(str(r.last_ingest))
            run = runs.get(str(r.source))
            if run is not None and run <= asof and (last is None or run > last):
                last = run
            out.append(
                TableStat(
                    table=table,
                    source=str(r.source),
                    rows=int(str(r.n)),
                    last_ingest=last.isoformat() if last else None,
                    latest_known_time=_iso(r.kt),
                    stale_bdays=_bdays(last.date(), asof.date()) if last else None,
                    allowed_bdays=allowed,
                )
            )
    return out


def check_freshness(stats: list[TableStat], pit: PIT, asof: dt.datetime) -> Check:
    stale = [
        f"{s.table}/{s.source}: last ingest {s.last_ingest} is {s.stale_bdays} business days old"
        f" (allowed {s.allowed_bdays})"
        for s in stats
        if s.stale_bdays is None or s.stale_bdays > s.allowed_bdays
    ]
    if pit.has("prices_daily"):
        df = pit.query(
            "SELECT max(date) AS d FROM prices_daily WHERE as_of(known_time, $asof)", asof
        )
        if pd.notna(df["d"][0]):
            last = _day(df["d"][0])
            gap = _bdays(last, asof.date())
            if gap > FRESHNESS_BDAYS["prices_daily"]:
                stale.append(f"prices_daily: latest bar {last} is {gap} business days old")
    if not stats:
        return Check("freshness", "info", "no tables ingested yet")
    if stale:
        return Check("freshness", "fail", f"{len(stale)} stale source(s)", stale)
    return Check("freshness", "pass", f"{len(stats)} table/source pairs fresh")


def check_row_counts(stats: list[TableStat]) -> Check:
    empty = [f"{s.table}/{s.source}" for s in stats if s.rows == 0]
    if empty:
        return Check("row_counts", "fail", "tables with zero rows", empty)
    return Check("row_counts", "pass", f"{sum(s.rows for s in stats)} rows in {len(stats)} groups")


def check_prices(pit: PIT, asof: dt.datetime) -> Check:
    px = pit.latest("prices_daily", asof)
    if px.empty:
        return Check("price_sanity", "info", "no prices")
    acts = pit.latest("corporate_actions", asof)
    action_days = (
        {(str(r.security_id), _day(r.ex_date)) for r in acts.itertuples()}
        if not acts.empty
        else set()
    )
    items = []
    bad = px[(px[["open", "high", "low", "close"]] <= 0).any(axis=1)]
    items += [f"{r.security_id} {_day(r.date)}: non-positive price" for r in bad.itertuples()]
    px = px.sort_values(["security_id", "date"])
    px["ret"] = px.groupby("security_id")["close"].pct_change()
    for r in px[px["ret"].abs() > MAX_ABS_MOVE].itertuples():
        d = _day(r.date)
        if (str(r.security_id), d) not in action_days:
            items.append(
                f"{r.security_id} {d}: move {r.ret:+.1%} without a corporate action; manual review"
            )
    if items:
        return Check("price_sanity", "fail", f"{len(items)} price issue(s)", items)
    return Check("price_sanity", "pass", f"{len(px)} bars checked")


def dead_letter_refs(lake: Lake) -> set[str]:
    if not lake.has_table("dead_letter"):
        return set()
    con = duckdb.connect()
    try:
        df = con.execute(
            "SELECT DISTINCT ref FROM read_parquet(?, hive_partitioning=true, union_by_name=true)",  # lookahead-ok: admin read of dead letters
            [lake.glob("dead_letter")],
        ).df()
    finally:
        con.close()
    return set(df["ref"].astype(str))


def check_form4(pit: PIT, lake: Lake, asof: dt.datetime) -> Check:
    filings = pit.latest("filings", asof, where="form IN ('4', '4/A')")
    if filings.empty:
        return Check("form4_completeness", "info", "no Form 4 filings")
    parsed: set[str] = set()
    if pit.has("insider_txns"):
        df = pit.query(
            "SELECT DISTINCT accession FROM insider_txns WHERE as_of(known_time, $asof)", asof
        )
        parsed = set(df["accession"].astype(str))
    dead = dead_letter_refs(lake)
    n_txns = filings["n_txns"] if "n_txns" in filings.columns else pd.Series(pd.NA, filings.index)
    missing = [
        str(acc)
        for acc, n in zip(filings["accession"], n_txns, strict=True)
        if str(acc) not in parsed and str(acc) not in dead and not (pd.notna(n) and int(n) == 0)
    ]
    if missing:
        return Check(
            "form4_completeness",
            "fail",
            f"{len(missing)} Form 4 filing(s) neither parsed, holdings-only nor dead-lettered",
            missing,
        )
    return Check(
        "form4_completeness",
        "pass",
        f"{len(filings)} Form 4 filings accounted for ({len(dead & set(filings['accession']))} dead-lettered)",
    )


def run_quality(
    lake: Lake,
    *,
    asof: dt.datetime | dt.date | str | None = None,
    journal: Journal | None = None,
    notify: Notify = no_email,
) -> DQReport:
    t = to_utc(asof) if asof is not None else utcnow()
    pit = PIT(lake)
    try:
        stats = table_stats(pit, t, last_runs(journal) if journal is not None else None)
        checks = [
            check_row_counts(stats),
            check_freshness(stats, pit, t),
            check_prices(pit, t),
            check_form4(pit, lake, t),
        ]
    finally:
        pit.close()
    report = DQReport(asof=t.isoformat(), tables=stats, checks=checks)
    if journal is not None:
        journal.append("dq_report", report.payload())
    if not report.ok:
        notify(report)
    return report
