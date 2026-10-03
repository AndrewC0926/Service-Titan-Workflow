"""`committee screen`: run the weekly screen as of a date and print the shortlist.

Register either the Typer app (``app``) or the command function (``screen_command``)
in the top-level CLI.
"""

from __future__ import annotations

from pathlib import Path

import typer

from committee.config.loader import ConfigError
from committee.context import AppContext
from committee.data.lake import Lake
from committee.data.pit import PIT
from committee.signals.persist import journal_screen, persist_signals, write_lazy_prices_diffs
from committee.signals.screen import ScreenResult, run_screen

app = typer.Typer(no_args_is_help=True, add_completion=False, help="Signals and weekly screen.")


@app.callback()
def _group() -> None:
    """Signals and weekly screen."""


ABBREV = {
    "insider_opportunistic": "ins",
    "lazy_prices": "lazy",
    "value": "val",
    "quality": "qual",
    "momentum": "mom",
    "low_risk": "lowrisk",
    "earnings_revision": "rev",
    "cluster_buy_bonus": "clus",
}


def lake_for(ctx: AppContext) -> Lake:
    """The PIT lake lives at the configured data_dir (``var/data``)."""
    return Lake(ctx.data_dir)


def _money(x: float) -> str:
    for unit, div in (("T", 1e12), ("B", 1e9), ("M", 1e6)):
        if abs(x) >= div:
            return f"{x / div:.1f}{unit}"
    return f"{x:,.0f}"


def format_shortlist(result: ScreenResult) -> str:
    lines = [
        f"Screen as of {result.asof_date}  universe={result.universe_size}  "
        f"shortlist={len(result.shortlist)}"
    ]
    if not result.shortlist:
        lines.append("  (no names)")
        return "\n".join(lines)
    lines.append(
        f"{'#':>3} {'ticker':<7} {'sector':<24} {'bucket':<15} {'mcap':>7} {'score':>7}  contributions / flags"
    )
    for i, r in enumerate(result.shortlist, 1):
        parts = [
            f"{ABBREV.get(k, k)}{v:+.3f}" for k, v in r.contributions.items() if abs(v) >= 5e-4
        ]
        flags = [
            k for k in ("recent_cluster_buy", "cluster_buy", "high_short_interest") if r.flags[k]
        ]
        if r.asymmetric_profile is not None:
            ap = r.asymmetric_profile
            flags.append(f"asym_profile={ap.score}/{ap.available}")
        if r.shortlist_reason == "cluster_buy":
            flags.append("added:cluster_buy")
        lines.append(
            f"{i:>3} {r.ticker:<7} {r.sector[:24]:<24} {r.bucket or '-':<15} "
            f"{_money(r.market_cap):>7} {r.composite:>+7.3f}  {' '.join(parts) or '-'}"
            + (f"  [{', '.join(flags)}]" if flags else "")
        )
    lottery = [(s, w) for s, w in result.excluded.items() if w.startswith("lottery_filter")]
    if lottery:
        tick = {r.security_id: r.ticker for r in result.candidates}
        lines.append(f"Lottery filter excluded {len(lottery)}:")
        lines += [f"  {tick.get(s, s)}: {w.removeprefix('lottery_filter: ')}" for s, w in lottery]
    return "\n".join(lines)


@app.command("screen")
def screen_command(
    asof: str = typer.Option(..., "--asof", help="As-of date (YYYY-MM-DD) or ISO timestamp."),
    root: Path | None = typer.Option(None, "--root", help="Project root."),
    holding: list[str] = typer.Option(
        [], "--holding-under-review", help="security_id or ticker to remove (repeatable)."
    ),
    wash_sale: list[str] = typer.Option(
        [], "--wash-sale-block", help="security_id or ticker on the wash-sale block list."
    ),
    mna: list[str] = typer.Option([], "--active-mna", help="security_id or ticker in an M&A deal."),
    persist: bool = typer.Option(True, "--persist/--no-persist", help="Write signals + journal."),
) -> None:
    """Run the weekly screen as of ASOF and print the shortlist with contributing signals."""
    try:
        ctx = AppContext.load(root)
    except ConfigError as e:
        typer.secho(str(e), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2) from None
    lake = lake_for(ctx)
    pit = PIT(lake)
    try:
        if not pit.has("security_master"):
            typer.secho(f"no security_master in lake at {lake.root}", fg=typer.colors.RED, err=True)
            raise typer.Exit(code=1)
        cfg = ctx.config
        result = run_screen(
            pit, asof, cfg.signals, cfg.universe,
            holdings_under_review=holding, wash_sale_blocked=wash_sale, active_mna=mna,
        )  # fmt: skip
    finally:
        pit.close()
    typer.echo(format_shortlist(result))
    if persist:
        n = persist_signals(lake, result)
        write_lazy_prices_diffs(result, ctx.data_dir / "artifacts" / "lazy_prices")
        with ctx.journal() as j:
            entry = journal_screen(j, result, dict(cfg.signals.weights))
        typer.echo(f"persisted {n} signal rows; journal seq {entry.seq}")
