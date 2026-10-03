"""``committee ingest ...`` and ``committee data ...`` sub-commands.

Register with ``app.add_typer(ingest_app, name="ingest")`` and
``app.add_typer(data_app, name="data")``. Missing secrets, unreachable networks
and other run-level failures print one clear line and exit 2 (no traceback);
``data check`` exits 1 when any DQ check fails.
"""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import typer

from committee.config.loader import ConfigError
from committee.context import AppContext
from committee.data import ingest
from committee.data.common import IngestError
from committee.data.http import FetchError
from committee.data.macro import DEFAULT_SERIES
from committee.data.quality import DQReport

ingest_app = typer.Typer(no_args_is_help=True, help="Ingest data into the point-in-time lake.")
data_app = typer.Typer(no_args_is_help=True, help="Data lake status and quality checks.")
# Convenience group holding both (``committee-data ingest ...`` / ``... data check``).
app = typer.Typer(no_args_is_help=True, help="Data ingestion and quality.")
app.add_typer(ingest_app, name="ingest")
app.add_typer(data_app, name="data")

ROOT_OPT = typer.Option(None, "--root", help="Project root (defaults to nearest dir with config/).")
TICKERS_OPT = typer.Option(..., "--tickers", help="Comma-separated tickers, e.g. AAPL,MSFT,JPM.")
SINCE_OPT = typer.Option(None, "--since", help="Start date YYYY-MM-DD.")


def _ctx(root: Path | None) -> AppContext:
    try:
        return AppContext.load(root)
    except ConfigError as e:
        typer.secho(str(e), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2) from None


@contextmanager
def _guard() -> Iterator[None]:
    try:
        yield
    except IngestError as e:
        typer.secho(f"ingest failed: {e}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2) from None
    except FetchError as e:
        hint = " (network unreachable?)" if e.status is None else ""
        typer.secho(f"fetch failed{hint}: {e}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2) from None


def _tickers(s: str) -> list[str]:
    out = [t.strip().upper() for t in s.split(",") if t.strip()]
    if not out:
        raise typer.BadParameter("give at least one ticker", param_hint="--tickers")
    return out


def _since(s: str | None, default_days: int) -> dt.date:
    if s is None:
        return dt.date.today() - dt.timedelta(days=default_days)
    try:
        return dt.date.fromisoformat(s)
    except ValueError:
        raise typer.BadParameter(f"not a date: {s!r}", param_hint="--since") from None


def _report(summary: dict[str, Any]) -> None:
    status = summary.get("status", "ok")
    color = typer.colors.GREEN if status == "ok" else typer.colors.YELLOW
    typer.secho(
        f"{summary['source']}: {status} in {summary['duration_s']}s; "
        f"rows {json.dumps(summary['rows_written'])}",
        fg=color,
    )
    for k, v in summary["counts"].items():
        typer.echo(f"  {k:28s} {v}")
    for e in summary["errors"][:10]:
        typer.secho(f"  ! {e}", fg=typer.colors.YELLOW)


def _run(root: Path | None, fn: Callable[[AppContext], dict[str, Any]]) -> None:
    ctx = _ctx(root)
    with _guard():
        _report(fn(ctx))
        rep = ingest.run_check(ctx)
    typer.echo(f"DQ: {'pass' if rep.ok else 'FAIL ' + ', '.join(c.name for c in rep.failures)}")


@ingest_app.command("securities")
def ingest_securities(root: Path | None = ROOT_OPT) -> None:
    """Seed / refresh the security master from SEC company tickers."""
    _run(root, lambda ctx: ingest.run_securities(ctx))


@ingest_app.command("edgar")
def ingest_edgar_cmd(
    tickers: str = TICKERS_OPT, since: str | None = SINCE_OPT, root: Path | None = ROOT_OPT
) -> None:
    """Filings (10-K/10-Q/8-K/3/4/5), Form 4 transactions, sections and XBRL facts."""
    tk, sd = _tickers(tickers), _since(since, 730)
    _run(root, lambda ctx: ingest.run_edgar(ctx, tk, sd))


@ingest_app.command("prices")
def ingest_prices_cmd(
    tickers: str = TICKERS_OPT, since: str | None = SINCE_OPT, root: Path | None = ROOT_OPT
) -> None:
    """Daily bars, splits and dividends (Massive; Finnhub fallback)."""
    tk, sd = _tickers(tickers), _since(since, 5 * 365)
    _run(root, lambda ctx: ingest.run_prices(ctx, tk, sd))


@ingest_app.command("macro")
def ingest_macro_cmd(
    since: str | None = SINCE_OPT,
    series: str | None = typer.Option(None, "--series", help="Comma-separated FRED ids."),
    root: Path | None = ROOT_OPT,
) -> None:
    """FRED/ALFRED series with vintages."""
    sd = _since(since, 5 * 365)
    ids = [s.strip().upper() for s in series.split(",")] if series else None
    _run(root, lambda ctx: ingest.run_macro(ctx, sd, series=ids or DEFAULT_SERIES))


@ingest_app.command("news")
def ingest_news_cmd(
    tickers: str = TICKERS_OPT,
    days: int = typer.Option(30, "--days", help="Look-back window in days."),
    root: Path | None = ROOT_OPT,
) -> None:
    """Finnhub company news (stored as untrusted text)."""
    tk = _tickers(tickers)
    _run(root, lambda ctx: ingest.run_news(ctx, tk, days=days))


@ingest_app.command("factors")
def ingest_factors_cmd(root: Path | None = ROOT_OPT) -> None:
    """Ken French 5-factor and momentum returns, daily and monthly."""
    _run(root, lambda ctx: ingest.run_factors(ctx))


@ingest_app.command("risk-indexes")
def ingest_risk_indexes_cmd(root: Path | None = ROOT_OPT) -> None:
    """Caldara-Iacoviello GPR (monthly, daily) and U.S. EPU."""
    _run(root, lambda ctx: ingest.run_risk_indexes(ctx))


def print_report(rep: DQReport) -> None:
    typer.echo(f"data check as of {rep.asof}")
    if not rep.tables:
        typer.echo("  (lake is empty)")
    for t in rep.tables:
        stale = "?" if t.stale_bdays is None else str(t.stale_bdays)
        typer.echo(
            f"  {t.table:18s} {t.source:14s} {t.rows:>9d} rows  last ingest "
            f"{(t.last_ingest or '-')[:19]}  age {stale}/{t.allowed_bdays} bd"
        )
    for c in rep.checks:
        color = {"pass": typer.colors.GREEN, "fail": typer.colors.RED}.get(c.status)
        typer.secho(f"  [{c.status.upper():4s}] {c.name}: {c.detail}", fg=color)
        for item in c.items[:10]:
            typer.secho(f"         - {item}", fg=color)
        if len(c.items) > 10:
            typer.echo(f"         ... {len(c.items) - 10} more")


@data_app.command("check")
def data_check(
    asof: str | None = typer.Option(None, "--asof", help="As-of date/time (default now)."),
    root: Path | None = ROOT_OPT,
) -> None:
    """Row counts and freshness per table plus DQ checks; exit 1 on any failure."""
    ctx = _ctx(root)
    with _guard():
        rep = ingest.run_check(ctx, asof=asof)
    print_report(rep)
    if not rep.ok:
        typer.secho(
            f"DQ FAILED: {', '.join(c.name for c in rep.failures)}", fg=typer.colors.RED, err=True
        )
        raise typer.Exit(code=1)
