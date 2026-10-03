"""`committee tax ...` commands. They read the lot ledger from the state DB."""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Annotated, cast

import typer

from committee.config.loader import ConfigError
from committee.context import AppContext
from committee.domain import AccountKind
from committee.engines.tax.advice import long_term_warnings
from committee.engines.tax.equivalence import Equivalence
from committee.engines.tax.harvest import harvest_scan, journal_harvest_scan
from committee.engines.tax.labels import DISCLAIMER
from committee.engines.tax.rates import TaxRates
from committee.engines.tax.report import write_realized_csv
from committee.engines.tax.store import TaxLedgerStore, load_events_file
from committee.engines.tax.wash_sale import WashSaleGuard

app = typer.Typer(
    no_args_is_help=True,
    add_completion=False,
    help=f"Tax engine: lots, wash sales, harvesting. {DISCLAIMER}",
)

ROOT_OPT = typer.Option(None, "--root", help="Project root (defaults to nearest dir with config/).")
EQUIV_OPT = typer.Option(
    None, "--equivalence", help="JSON list of symbol groups treated as substantially identical."
)


def _date(s: str | None) -> dt.date:
    return dt.date.fromisoformat(s) if s else dt.date.today()


def _ctx(root: Path | None) -> AppContext:
    try:
        return AppContext.load(root)
    except ConfigError as e:
        typer.secho(str(e), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2) from None


def _store(ctx: AppContext, equivalence: Path | None) -> TaxLedgerStore:
    eq = Equivalence()
    if equivalence is not None:
        eq = Equivalence.from_lists(json.loads(equivalence.read_text(encoding="utf-8")))
    return TaxLedgerStore(ctx.state_db, ctx.config.tax.wash_sale_window_days, eq)


def _prices(path: Path) -> dict[str, float]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    return {str(k).upper(): float(v) for k, v in raw.items()}


@app.command("import-events")
def import_events(
    events: Annotated[Path, typer.Argument(help="JSON list of buy/sell events.")],
    root: Path | None = ROOT_OPT,
    equivalence: Path | None = EQUIV_OPT,
) -> None:
    """Append purchases and sales to the lot ledger (atomic; rejected if replay fails)."""
    ctx = _ctx(root)
    with _store(ctx, equivalence) as store:
        try:
            ledger = store.import_events(load_events_file(events))
        except ValueError as e:
            typer.secho(f"rejected: {e}", fg=typer.colors.RED, err=True)
            raise typer.Exit(code=1) from None
        typer.echo(DISCLAIMER)
        typer.echo(f"ledger: {store.event_count()} events, {len(ledger.open_lots())} open lots")


@app.command("wash-sale-blocks")
def wash_sale_blocks(
    asof: str | None = typer.Option(None, "--asof", help="Date (YYYY-MM-DD); default today."),
    root: Path | None = ROOT_OPT,
    equivalence: Path | None = EQUIV_OPT,
) -> None:
    """Symbols no account may buy on the date (shared with the screen)."""
    ctx = _ctx(root)
    on = _date(asof)
    with _store(ctx, equivalence) as store:
        blocked = sorted(WashSaleGuard(store.ledger()).block_list(on))
    typer.echo(DISCLAIMER)
    typer.echo(f"wash-sale block list as of {on}: " + (", ".join(blocked) or "(none)"))


@app.command("check-purchase")
def check_purchase_cmd(
    symbol: str = typer.Argument(...),
    account: str = typer.Option("taxable", "--account", help="taxable | ira | k401"),
    on: str | None = typer.Option(None, "--on", help="Date (YYYY-MM-DD); default today."),
    root: Path | None = ROOT_OPT,
    equivalence: Path | None = EQUIV_OPT,
) -> None:
    """Verdict (ALLOW / WARN / BLOCK) for buying SYMBOL in ACCOUNT."""
    if account not in ("taxable", "ira", "k401"):
        raise typer.BadParameter("account must be taxable, ira or k401")
    ctx = _ctx(root)
    with _store(ctx, equivalence) as store:
        v = WashSaleGuard(store.ledger()).check_purchase(
            symbol, cast(AccountKind, account), _date(on)
        )
    typer.echo(v.reason)
    if v.verdict == "BLOCK":
        raise typer.Exit(code=1)


@app.command("harvest-scan")
def harvest_scan_cmd(
    prices: Annotated[Path, typer.Option("--prices", help="JSON {symbol: price} marks.")],
    asof: str | None = typer.Option(None, "--asof", help="Date (YYYY-MM-DD); default today."),
    journal: bool = typer.Option(False, "--journal", help="Write harvest_proposal entries."),
    root: Path | None = ROOT_OPT,
    equivalence: Path | None = EQUIV_OPT,
) -> None:
    """Monthly scan of taxable lots for losses to harvest into mapped replacements."""
    ctx = _ctx(root)
    on = _date(asof)
    with _store(ctx, equivalence) as store:
        scan = harvest_scan(store.ledger(), _prices(prices), on, ctx.config.tax)
    typer.echo(f"{DISCLAIMER}\nHarvest scan as of {on}:")
    for p in scan.proposals:
        typer.echo(f"  {p.reason}")
    for s in scan.skipped:
        typer.echo(f"  {s.reason}")
    if not scan.proposals and not scan.skipped:
        typer.echo("  no taxable lots meet the harvest thresholds")
    if journal:
        with ctx.journal() as j:
            n = len(journal_harvest_scan(j, scan))
        typer.echo(f"journaled {n} harvest_proposal entries")


@app.command("lt-warnings")
def lt_warnings_cmd(
    prices: Annotated[Path, typer.Option("--prices", help="JSON {symbol: price} marks.")],
    asof: str | None = typer.Option(None, "--asof", help="Date (YYYY-MM-DD); default today."),
    thesis_broken: Annotated[
        list[str] | None, typer.Option("--thesis-broken", help="Symbol; repeatable.")
    ] = None,
    root: Path | None = ROOT_OPT,
    equivalence: Path | None = EQUIV_OPT,
) -> None:
    """Gains within the warning window of turning long-term (default: wait)."""
    ctx = _ctx(root)
    cfg = ctx.config.tax
    with _store(ctx, equivalence) as store:
        warnings = long_term_warnings(
            store.ledger().open_lots(account="taxable"),
            _prices(prices),
            _date(asof),
            TaxRates.from_config(cfg),
            cfg.long_term_warning_days,
            [s.upper() for s in thesis_broken or []],
        )
    typer.echo(DISCLAIMER)
    for w in warnings:
        typer.echo(f"  {w.reason}")
    if not warnings:
        typer.echo("  no lots within the long-term warning window")


@app.command("realized-report")
def realized_report(
    year: Annotated[int, typer.Option("--year")],
    out: Annotated[Path, typer.Option("--out", help="CSV path to write.")],
    root: Path | None = ROOT_OPT,
    equivalence: Path | None = EQUIV_OPT,
) -> None:
    """Annual realized gains and losses CSV for the CPA."""
    ctx = _ctx(root)
    with _store(ctx, equivalence) as store:
        summary = write_realized_csv(store.ledger().realized(), year, out)
    typer.echo(summary.text())
    typer.echo(f"wrote {out}")
