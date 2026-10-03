"""Top-level workflow commands: screen, review, approvals, orders, core, eval,
digest, ops and gates. Each command builds what it needs from an AppContext."""

from __future__ import annotations

import datetime as dt
import json
import shlex
from collections.abc import Callable
from pathlib import Path
from typing import Any

import typer

from committee.broker.approval import ApprovalError, GateSettings, approve, expire_stale, reject
from committee.broker.factory import is_live, make_broker
from committee.broker.gateway import (
    Caps,
    OrderBlocked,
    OrderGateway,
    kill_switch,
    release_kill_switch,
)
from committee.broker.models import ApprovedLeg
from committee.config.loader import ConfigError
from committee.context import AppContext
from committee.core.drift import compute_drift
from committee.core.holdings import HoldingsStore, ImportError_
from committee.core.rebalance import propose_rebalance
from committee.domain import Bucket
from committee.engines.allocator.rules import AllocatorInputs, decide, journal_decision
from committee.engines.tax.rates import TaxRates
from committee.engines.tax.selection import select_lots
from committee.engines.tax.wash_sale import WashSaleGuard
from committee.evaluation.cohorts import ReviewOutcome, cohort_stats, overrides_underperform
from committee.evaluation.forecasts import Forecast, agent_weights, score_forecasts
from committee.evaluation.resolve import resolve_due
from committee.journal.store import Journal
from committee.ops.alerts import Alerter, smtp_sender

ROOT = typer.Option(None, "--root", help="Project root (defaults to nearest dir with config/).")

review_app = typer.Typer(no_args_is_help=True, help="Committee reviews.")
briefings_app = typer.Typer(no_args_is_help=True, help="Briefings awaiting a decision.")
orders_app = typer.Typer(
    no_args_is_help=True, help="Orders: place approved trades, reconcile fills."
)
core_app = typer.Typer(
    no_args_is_help=True, help="Core sleeve: holdings, drift, rebalance proposals."
)
eval_app = typer.Typer(no_args_is_help=True, help="Scorecards, cohorts, agent weights, allocator.")
ops_app = typer.Typer(no_args_is_help=True, help="Backups, health, scheduler.")
gate_app = typer.Typer(
    no_args_is_help=True, help="Live-trading gate check (never enables live trading)."
)
digest_app = typer.Typer(no_args_is_help=True, help="Daily digest.")


def ctx_of(root: Path | None) -> AppContext:
    try:
        return AppContext.load(root)
    except ConfigError as e:
        typer.secho(str(e), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2) from None


def fail(msg: str, code: int = 1) -> typer.Exit:
    typer.secho(msg, fg=typer.colors.RED, err=True)
    return typer.Exit(code=code)


def parse_day(s: str) -> dt.date:
    return dt.datetime.now(dt.UTC).date() if s == "today" else dt.date.fromisoformat(s)


def now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def alerter(ctx: AppContext) -> Alerter:
    senders = []
    smtp, to = ctx.secrets.get("SMTP_URL"), ctx.secrets.get("DIGEST_TO")
    if smtp and to:
        senders.append(smtp_sender(smtp, to))
    return Alerter(senders)


def under_review(journal: Journal) -> set[str]:
    from committee.broker.approval import decided

    return {
        str(e.payload["symbol"])
        for e in journal.entries("briefing")
        if not decided(journal, e.hash) and e.payload.get("symbol")
    }


def wash_blocks(ctx: AppContext, day: dt.date) -> set[str]:
    from committee import services

    with services.tax_store(ctx) as ts:
        return WashSaleGuard(ts.ledger()).block_list(day)


# ------------------------------------------------------------------- screen
def screen(asof: str = typer.Option("today", "--asof"), root: Path | None = ROOT) -> None:
    """Weekly screen; removes names under review and the wash-sale block list automatically."""
    from committee.signals.cli import screen_command

    ctx = ctx_of(root)
    day = parse_day(asof)
    with ctx.journal() as j:
        holding = sorted(under_review(j))
    screen_command(
        asof=day.isoformat(),
        root=ctx.root,
        holding=holding,
        wash_sale=sorted(wash_blocks(ctx, day)),
        mna=[],
        persist=True,
    )


# ------------------------------------------------------------------- review
def _bucket_for(ctx: AppContext, mcap: float, adv: float) -> str | None:
    from committee.signals.buckets import tag_bucket

    return tag_bucket(mcap, adv, ctx.config.universe)


def run_one_review(
    ctx: AppContext,
    j: Journal,
    symbol: str,
    day: dt.date,
    bucket: str | None,
    lottery: bool | None,
    origin: str,
) -> int:
    from committee import services

    p = services.pit(ctx)
    try:
        facts = services.name_facts(p, symbol, day)
        b = bucket or _bucket_for(ctx, facts.market_cap_usd, facts.adv_usd)
        if b not in ("core_pick", "asymmetric_bet"):
            typer.echo(
                f"{symbol}: not eligible for either satellite bucket (market cap / liquidity)"
            )
            return 1
        bucket_name: Bucket = "core_pick" if b == "core_pick" else "asymmetric_bet"
        inp, rep = services.build_review_inputs(
            ctx, j, p, symbol, day, bucket_name, lottery_pass=lottery, origin=origin
        )
        orch = services.orchestrator(ctx, j, rep)
        rid = f"{facts.security_id}-{day:%Y%m%d}"
        out = orch.review(inp, rid)
    finally:
        p.close()
    typer.echo(
        f"{symbol}: {out.state}"
        + (f" — {out.reason}" if out.reason else "")
        + f" (cost ${out.cost_usd:.2f})"
    )
    if out.briefing is not None:
        typer.echo(out.briefing.payload.get("markdown", ""))
        typer.echo(f"\nbriefing hash: {out.briefing.hash}")
    return 0 if out.state in ("AWAITING_APPROVAL", "VETOED") else 1


@review_app.command("run")
def review_run(
    symbol: str,
    asof: str = typer.Option("today", "--asof"),
    bucket: str | None = typer.Option(
        None, "--bucket", help="core_pick | asymmetric_bet (default: deterministic tag)"
    ),
    root: Path | None = ROOT,
) -> None:
    """Run the full committee on one ticker and print the briefing."""
    ctx = ctx_of(root)
    try:
        with ctx.journal() as j:
            code = run_one_review(
                ctx, j, symbol.upper(), parse_day(asof), bucket, None, "human request"
            )
    except (KeyError, RuntimeError) as e:
        raise fail(str(e)) from None
    raise typer.Exit(code=code)


@review_app.command("batch")
def review_batch(
    asof: str = typer.Option("today", "--asof"),
    rereview_only: bool = typer.Option(False, "--rereview-only"),
    root: Path | None = ROOT,
) -> None:
    """Review names from the latest screen (half the weekly allowance per session), or re-review triggered names."""
    from committee.agents.budget import BudgetGuard

    ctx = ctx_of(root)
    day = parse_day(asof)
    with ctx.journal() as j:
        allowed = max(1, BudgetGuard(j, ctx.config.models.budget).reviews_per_week_allowed() // 2)
        todo: list[tuple[str, str | None, bool | None, str]] = []
        if rereview_only:
            since = now() - dt.timedelta(days=7)
            todo = sorted(
                {
                    (str(e.payload["symbol"]), None, None, "re-review trigger")
                    for e in j.entries("note")
                    if e.payload.get("kind") == "rereview_trigger" and e.created_at >= since
                }
            )
        else:
            scr = j.latest("screen")
            if scr is None:
                raise fail("no screen in the journal; run `committee screen` first")
            todo = [
                (r["ticker"], r.get("bucket"), r.get("lottery_pass"), "weekly screen")
                for r in scr.payload["shortlist"]
            ]
            todo = [t for t in todo if t[0] not in under_review(j)]
        failures = 0
        for sym, b, lot, origin in todo[:allowed]:
            try:
                failures += run_one_review(ctx, j, sym, day, b, lot, origin)
            except (KeyError, RuntimeError) as e:
                typer.secho(f"{sym}: {e}", fg=typer.colors.YELLOW)
                failures += 1
    typer.echo(
        f"reviewed {min(len(todo), allowed)} of {len(todo)} candidates (allowance {allowed})"
    )
    raise typer.Exit(code=1 if failures and failures == min(len(todo), allowed) else 0)


@review_app.command("triggers")
def review_triggers(asof: str = typer.Option("today", "--asof"), root: Path | None = ROOT) -> None:
    """Journal new re-review triggers for approved positions."""
    from committee import services
    from committee.orchestration.triggers import find_triggers, journal_new, pit_8k_items

    ctx = ctx_of(root)
    day = parse_day(asof)
    p = services.pit(ctx)
    with ctx.journal() as j:
        new = journal_new(
            j, find_triggers(j, day, services.price_lookup(p, day), pit_8k_items(p, day))
        )
    for t in new:
        typer.echo(f"{t.symbol}: {t.reason}")
    typer.echo(f"{len(new)} new triggers")


# ---------------------------------------------------------------- briefings
@briefings_app.command("list")
def briefings_list(root: Path | None = ROOT) -> None:
    from committee.ui.views import approval_queue

    ctx = ctx_of(root)
    with ctx.journal() as j:
        for q in approval_queue(j, now(), ctx.config.app.approval.expiry_days):
            status = "ready" if q.ready else f"cooling-off {q.cooling_left}"
            typer.echo(
                f"{q.hash}  {q.label:<8} {q.recommendation:<9} {status}  expires {q.expires_at:%Y-%m-%d}"
            )


@briefings_app.command("show")
def briefings_show(briefing_hash: str, root: Path | None = ROOT) -> None:
    ctx = ctx_of(root)
    with ctx.journal() as j:
        e = j.find_by_hash(briefing_hash)
    if e is None:
        raise fail("no such entry")
    typer.echo(e.payload.get("markdown") or json.dumps(e.payload, indent=2))


@briefings_app.command("expire")
def briefings_expire(root: Path | None = ROOT) -> None:
    ctx = ctx_of(root)
    with ctx.journal() as j:
        n = expire_stale(j, now())
    typer.echo(f"expired {len(n)} briefings")


# ---------------------------------------------------------------- decisions
def approve_cmd(
    briefing_hash: str,
    reason: str = typer.Option(..., "--reason", help="One sentence: why you approve."),
    pct: list[float] = typer.Option(
        [],
        "--pct",
        help="Approved % of account per leg, in order (default: the recommended maximum).",
    ),
    justification: str | None = typer.Option(
        None, "--justification", help="Required when the Behavioral Auditor said STOP."
    ),
    account_value: float | None = typer.Option(
        None, "--account-value", help="Total account value (default: current holdings valuation)."
    ),
    root: Path | None = ROOT,
) -> None:
    """Approve a briefing or proposal (smaller or equal, never larger)."""
    from committee import services

    ctx = ctx_of(root)
    with ctx.journal() as j:
        e = j.find_by_hash(briefing_hash)
        if e is None:
            raise fail("no such briefing")
        legs = e.payload.get("legs", [])
        approved = [
            ApprovedLeg(
                symbol=leg["symbol"],
                side=leg["side"],
                account=leg["account"],
                pct_total=(pct[i] if i < len(pct) else leg["max_pct_total"]),
            )
            for i, leg in enumerate(legs)
            if (pct[i] if i < len(pct) else leg["max_pct_total"]) > 0
        ]
        if account_value is None:
            p = services.pit(ctx)
            account_value = services.portfolio_state(ctx, j, p, now().date())[0].total_value
        try:
            gate = GateSettings(
                ctx.config.app.approval.expiry_days, ctx.config.app.approval.min_reason_chars
            )
            _, a = approve(
                j,
                briefing_hash,
                approved,
                reason,
                account_value,
                now(),
                justification=justification,
                settings=gate,
            )
        except ApprovalError as err:
            raise fail(f"not approved: {err}") from None
    typer.echo(
        f"approved; approval hash {a.hash}\nplace orders with: committee orders place {a.hash}"
    )


def reject_cmd(
    briefing_hash: str, reason: str = typer.Option(..., "--reason"), root: Path | None = ROOT
) -> None:
    """Reject a briefing (it is still scored, in the PASS or OVERRIDE cohort)."""
    ctx = ctx_of(root)
    with ctx.journal() as j:
        try:
            reject(j, briefing_hash, reason, now())
        except ApprovalError as err:
            raise fail(str(err)) from None
    typer.echo("rejected")


# ------------------------------------------------------------------- orders
def gateway(ctx: AppContext, j: Journal) -> OrderGateway:
    from committee import services

    b = ctx.config.app.broker
    p = services.pit(ctx)
    price = services.price_lookup(p, now().date())
    return OrderGateway(
        j,
        make_broker(ctx, j),
        ctx.flags(),
        Caps(
            b.per_order_notional_cap_pct,
            b.daily_notional_cap_pct,
            b.limit_band_pct,
            b.time_in_force,
        ),
        last_close=price,
        live=is_live(ctx, j),
    )


@orders_app.command("place")
def orders_place(
    approval_hash: str,
    account_value: float | None = typer.Option(None, "--account-value"),
    root: Path | None = ROOT,
) -> None:
    """Place limit orders for an approval (tranches under the per-order and daily caps)."""
    from committee import services

    ctx = ctx_of(root)
    with ctx.journal() as j:
        if account_value is None:
            p = services.pit(ctx)
            account_value = services.portfolio_state(ctx, j, p, now().date())[0].total_value
        try:
            placed = gateway(ctx, j).place(approval_hash, account_value)
        except OrderBlocked as e:
            raise fail(f"blocked: {e}") from None
    for o in placed:
        typer.echo(
            f"{o.venue:<6} {o.side} {o.qty} {o.symbol} @ {o.limit_price} (${o.notional_usd:,.0f}) {o.client_order_id}"
        )
    typer.echo(
        f"{len(placed)} orders"
        + ("" if placed else " (nothing left today under the caps, or fully executed)")
    )


@orders_app.command("reconcile")
def orders_reconcile(root: Path | None = ROOT) -> None:
    """Journal new fills and record them as tax lots."""
    from committee import services

    ctx = ctx_of(root)
    with ctx.journal() as j:
        gw = gateway(ctx, j)
        fills = gw.reconcile(on_fill=services.fill_to_ledger(ctx))
        missing = gw.unreconciled_orders()
    typer.echo(f"{len(fills)} new fills reconciled")
    if missing:
        raise fail(f"unreconciled filled orders: {missing}")


def kill_switch_cmd(
    release: bool = typer.Option(False, "--release"),
    reason: str = typer.Option(..., "--reason"),
    root: Path | None = ROOT,
) -> None:
    """Engage (or, after a write-up, release) the kill switch."""
    ctx = ctx_of(root)
    with ctx.journal() as j:
        if release:
            release_kill_switch(j, ctx.flags(), reason)
            typer.echo("kill switch released")
        else:
            n = kill_switch(j, make_broker(ctx, j), ctx.flags(), reason)
            typer.secho(
                f"KILL SWITCH ENGAGED: {n} open orders cancelled; submission blocked",
                fg=typer.colors.RED,
            )


# --------------------------------------------------------------------- core
@core_app.command("import")
def core_import(csv_path: Path, root: Path | None = ROOT) -> None:
    """Import IRA / 401(k) (or any account) positions from CSV."""
    ctx = ctx_of(root)
    store = HoldingsStore(ctx.state_db)
    try:
        counts = store.import_csv(csv_path, now())
    except ImportError_ as e:
        raise fail(str(e)) from None
    finally:
        store.close()
    with ctx.journal() as j:
        j.append(
            "ingest_summary",
            {"source": "holdings_csv", "file": csv_path.name, "counts": counts, "status": "ok"},
        )
    typer.echo(f"imported {counts}")


def _drift(ctx: AppContext, j: Journal):  # type: ignore[no-untyped-def]
    from committee import services

    p = services.pit(ctx)
    pos = services.positions(ctx)
    price = services.price_lookup(p, now().date())
    missing = sorted({x.symbol for x in pos if x.symbol != "CASH" and not price(x.symbol)})
    if missing:
        raise fail(f"no price for {missing}; ingest prices first")
    pol = ctx.active_config(j).policy_portfolio
    return (
        pol,
        pos,
        price,
        compute_drift(pol, pos, price, satellite_symbols=set(services.satellite_buckets(j))),
    )


@core_app.command("drift")
def core_drift(root: Path | None = ROOT) -> None:
    ctx = ctx_of(root)
    with ctx.journal() as j:
        _, _, _, d = _drift(ctx, j)
    typer.echo(f"total ${d.total_value_usd:,.0f}")
    for s in d.sleeves:
        typer.echo(
            f"{s.sleeve_id:<10} target {s.target_pct:5.1f}%  actual {s.actual_pct:5.1f}%  band {s.band_pct}  {'BREACH' if s.breached else ''}"
        )


@core_app.command("propose")
def core_propose(root: Path | None = ROOT) -> None:
    """Journal a rebalance proposal when a sleeve breaches its band (approve it like a briefing)."""
    from committee import services

    ctx = ctx_of(root)
    rates = TaxRates.from_config(ctx.config.tax)
    with ctx.journal() as j:
        pol, pos, price, d = _drift(ctx, j)
        with services.tax_store(ctx) as ts:
            ledger = ts.ledger()

        def tax_cost(symbol: str, account: str, dollars: float) -> float:
            if account != "taxable" or not price(symbol):
                return 0.0
            lots = list(ledger.open_lots(account="taxable", symbol=symbol))
            if not lots:
                return 0.0
            return float(
                select_lots(
                    lots, dollars / price(symbol), price(symbol), now().date(), rates
                ).estimated_tax
            )

        prop = propose_rebalance(pol, d, pos, price, tax_cost=tax_cost)
        if not prop.legs:
            typer.echo("; ".join(prop.notes))
            return
        e = j.append(
            "core_proposal",
            prop.payload(cooling_off_hours=ctx.config.risk_limits.cooling_off_hours.default),
        )
    for leg in prop.legs:
        typer.echo(
            f"{leg.side:<4} {leg.symbol:<5} {leg.account:<8} ${leg.notional_usd:>12,.0f}  ({leg.why})"
        )
    typer.echo(
        f"estimated tax ${prop.est_tax_usd:,.0f}. Not tax advice. Confirm with a CPA.\nproposal hash {e.hash}"
    )


# --------------------------------------------------------------------- eval
def _path_fn(ctx: AppContext):  # type: ignore[no-untyped-def]
    from committee import services
    from committee.data.security_master import resolve
    from committee.orchestration.pit_source import PITSource

    p = services.pit(ctx)

    def path(symbol: str, start: dt.date, end: dt.date) -> list[float]:
        sid = resolve(p, symbol, end)
        if not sid:
            return []
        rows = PITSource(p).fetch("prices", sid, end)
        return [
            float(r["adj_close"])
            for r in rows
            if start.isoformat() <= str(r["date"]) <= end.isoformat()
        ]

    return path


def _forecasts(j: Journal) -> list[Forecast]:
    out = []
    for e in j.entries("forecast_resolution"):
        p = e.payload
        out.append(
            Forecast(
                str(p["forecast_seq"]),
                str(p["thesis_id"]),
                str(p["agent"]),
                str(p["event"]),
                int(p["horizon_months"]),
                float(p["probability"]),
                e.created_at,
                str(p["cohort"]),
                int(p["outcome"]),
            )
        )
    return out


@eval_app.command("monthly")
def eval_monthly(root: Path | None = ROOT) -> None:
    """Resolve due forecasts; report scores, cohorts, overrides and costs."""
    from committee.broker.approval import decided
    from committee.ui.views import costs

    ctx = ctx_of(root)
    with ctx.journal() as j:
        n = resolve_due(j, now().date(), _path_fn(ctx))
        scores = score_forecasts(_forecasts(j))
        res = {
            (e.payload["thesis_id"], int(e.payload["horizon_months"])): e.payload
            for e in j.entries("forecast_resolution")
            if e.payload["agent"] == "chair" and e.payload["event"] == "beats_benchmark"
        }
        reviews = []
        for b in j.entries("briefing"):
            rid = b.payload["review_id"]
            ex = {
                h: (float(r["return"]) - float(r["benchmark_return"]))
                if (r := res.get((rid, h)))
                else None
                for h in (3, 6, 12)
            }
            reviews.append(
                ReviewOutcome(
                    rid,
                    b.payload["recommendation"],
                    b.payload.get("risk_verdict", "PASS"),
                    decided(j, b.hash),
                    b.payload.get("flags") == ["human_request"],
                    b.payload.get("model_cohort", ""),
                    ex,
                )
            )
        stats = cohort_stats(reviews)
        report = {
            "resolved_now": n,
            "scores": [s.__dict__ for s in scores],
            "cohorts": [s.__dict__ for s in stats if s.n],
            "overrides_underperform": overrides_underperform(stats),
            "costs": costs(j, now()),
        }
        j.append("note", {"kind": "monthly_report", **json.loads(json.dumps(report, default=str))})
    typer.echo(json.dumps(report, indent=2, default=str))


@eval_app.command("quarterly")
def eval_quarterly(root: Path | None = ROOT) -> None:
    """Agent reweighting (shrunk, <=2x) and the capital allocator recommendation."""
    ctx = ctx_of(root)
    with ctx.journal() as j:
        scores = [
            s
            for s in score_forecasts(_forecasts(j))
            if s.event == "beats_benchmark" and s.brier_skill_vs_base_rate is not None
        ]
        skills: dict[str, list[float]] = {}
        for s in scores:
            skills.setdefault(s.agent, []).append(float(s.brier_skill_vs_base_rate or 0.0))
        weights = agent_weights({a: sum(v) / len(v) for a, v in skills.items()})
        if weights:
            j.append(
                "agent_weights",
                {
                    "weights": weights,
                    "basis": "Brier skill vs base rate, shrunk 50% toward equal, max 2x",
                },
            )
        live = [e for e in j.entries("order") if e.payload.get("mode") == "live"]
        months = 0 if not live else (now() - live[0].created_at).days // 30
        lim = ctx.active_config(j).risk_limits.account
        inp = AllocatorInputs(
            live_months=months,
            current_pct=lim.satellite_target_pct,
            min_pct=lim.satellite_min_pct,
            max_pct=lim.satellite_max_pct,
            deflated_sharpe_vs_fmb=None,
            cum_excess_after_tax_vs_spy=None,
            cum_excess_after_tax_vs_fmb=None,
            satellite_brier=None,
            base_rate_brier=None,
            satellite_max_dd=None,
            benchmark_max_dd=None,
            excess_24m_after_tax_vs_fmb=None,
            quarters_brier_worse_than_base=0,
            guardrail_breaches_this_quarter=sum(
                1
                for e in j.entries("incident")
                if (now() - e.created_at).days <= 92 and e.payload.get("level") == "P1"
            ),
        )
        d = decide(inp)
        journal_decision(j, inp, d)
    typer.echo(f"agent weights: {weights or 'not enough resolved forecasts'}")
    typer.echo(
        f"allocator: {d.action} {d.current_pct}% -> {d.recommended_pct}%: " + "; ".join(d.reasons)
    )


# --------------------------------------------------------------------- digest
@digest_app.command("send")
def digest_send(
    print_only: bool = typer.Option(False, "--print"), root: Path | None = ROOT
) -> None:
    from committee.ui.digest import build_digest

    ctx = ctx_of(root)
    day = now().date()
    with ctx.journal() as j:
        from committee import services

        with services.tax_store(ctx) as ts:
            guard = WashSaleGuard(ts.ledger())
            blocks = {
                s: (guard.check_purchase(s, "taxable", day).clear_on or day)
                for s in guard.block_list(day)
            }
        d = build_digest(
            j, now(), wash_sale_blocks=blocks, orders_blocked=ctx.flags().orders_blocked()
        )
    typer.echo(d.text())
    if not print_only:
        a = alerter(ctx)
        if not a.senders:
            raise fail("SMTP_URL / DIGEST_TO not configured; printed only")
        for s in a.senders:
            s(d.subject, d.text())


# ------------------------------------------------------------------------ ops
@ops_app.command("backup")
def ops_backup(keep: int = 30, root: Path | None = ROOT) -> None:
    from committee.ops.backup import create_backup, prune

    ctx = ctx_of(root)
    out = ctx.path(ctx.config.app.paths.backup_dir)
    path = create_backup(ctx.var_dir, out, ctx.secrets.require("BACKUP_PASSPHRASE"))
    prune(out, keep)
    with ctx.journal() as j:
        j.append("backup", {"file": path.name, "bytes": path.stat().st_size})
    typer.echo(f"backup written: {path}")


@ops_app.command("restore-test")
def ops_restore_test(
    file: Path | None = typer.Option(None, "--file"), root: Path | None = ROOT
) -> None:
    from committee.ops.backup import restore_test

    ctx = ctx_of(root)
    out = ctx.path(ctx.config.app.paths.backup_dir)
    target = file or max(out.glob("committee-*.bak"), default=None)
    if target is None:
        raise fail("no backups found")
    rt = restore_test(target, ctx.secrets.require("BACKUP_PASSPHRASE"))
    with ctx.journal() as j:
        j.append("note", {"kind": "restore_test", **rt.__dict__})
    typer.echo(
        f"restore test {'OK' if rt.ok else 'FAILED'}: {rt.files} files, journal {rt.journal_entries} entries ({rt.detail})"
    )
    if not rt.ok:
        raise typer.Exit(code=1)


@ops_app.command("health")
def ops_health(root: Path | None = ROOT) -> None:
    from committee.ops.health import check_health

    ctx = ctx_of(root)
    with ctx.journal() as j:
        checks = check_health(j, ctx.flags(), ctx.var_dir, now())
    for c in checks:
        typer.echo(f"{'OK ' if c.ok else 'BAD'} {c.name:<16} {c.detail}")
    if not all(c.ok for c in checks):
        raise typer.Exit(code=1)


@ops_app.command("scheduler")
def ops_scheduler(root: Path | None = ROOT) -> None:
    """Run the operating schedule in the foreground (Ctrl-C to stop)."""
    from committee.ops.scheduler import build_scheduler

    ctx = ctx_of(root)
    a = alerter(ctx)
    sched = build_scheduler(ctx.journal, a)
    typer.echo("scheduler running (America/New_York)")
    sched.start()  # type: ignore[attr-defined]


@ops_app.command("jobs")
def ops_jobs(root: Path | None = ROOT) -> None:
    """List scheduled jobs."""
    from committee.ops.scheduler import JOBS

    for job in JOBS:
        typer.echo(f"{job.id:<24} {job.cron:<20} committee {job.command}")


# ---------------------------------------------------------------------- gates
@gate_app.command("check")
def gate_check(root: Path | None = ROOT) -> None:
    from committee.ops.gates import run_gate_check

    ctx = ctx_of(root)
    with ctx.journal() as j:
        gates, report = run_gate_check(
            j, ctx.root, ctx.path(ctx.config.app.broker.live_gate_file), now()
        )
    for g in gates:
        typer.echo(f"{g.id} {g.name}: {'PASS' if g.ok else 'FAIL'}")
    typer.echo(f"report: {report}")


@gate_app.command("sign")
def gate_sign(
    signer: str = typer.Option(..., "--signer"),
    statement: str = typer.Option(..., "--statement"),
    root: Path | None = ROOT,
) -> None:
    """Sign the live-gate file. You still set LIVE_TRADING_ENABLED=true yourself."""
    from committee.ops.gates import sign

    ctx = ctx_of(root)
    typer.confirm(
        "Signing allows live orders once you also set LIVE_TRADING_ENABLED=true. Continue?",
        abort=True,
    )
    with ctx.journal() as j:
        try:
            h = sign(j, ctx.path(ctx.config.app.broker.live_gate_file), signer, statement)
        except (FileNotFoundError, ValueError) as e:
            raise fail(str(e)) from None
    typer.echo(f"signed gate file {h[:16]}")


# ------------------------------------------------------------- ingest nightly
def ingest_nightly(root: Path | None = ROOT) -> None:
    """Nightly ingest: securities, EDGAR, prices, macro, news for holdings + latest shortlist + benchmark."""
    from committee import services
    from committee.data import ingest

    ctx = ctx_of(root)
    with ctx.journal() as j:
        scr = j.latest("screen")
        tickers = {r["ticker"] for r in (scr.payload["shortlist"] if scr else [])}
    tickers |= {p.symbol for p in services.positions(ctx) if p.symbol != "CASH"} | {
        services.BENCHMARK
    }
    tickers |= set(ctx.config.universe.test_universe)
    today = now().date()
    results = []
    steps: tuple[tuple[str, Callable[[], dict[str, Any]]], ...] = (
        ("securities", lambda: ingest.run_securities(ctx)),
        ("edgar", lambda: ingest.run_edgar(ctx, sorted(tickers), today - dt.timedelta(days=30))),
        ("prices", lambda: ingest.run_prices(ctx, sorted(tickers), today - dt.timedelta(days=10))),
        ("macro", lambda: ingest.run_macro(ctx, today - dt.timedelta(days=120))),
        ("news", lambda: ingest.run_news(ctx, sorted(tickers))),
    )
    for name, fn in steps:
        try:
            r: dict[str, Any] = fn()
            results.append((name, str(r.get("status", "?"))))
        except Exception as e:  # each source fails independently
            results.append((name, f"error: {e}"[:200]))
    for name, status in results:
        typer.echo(f"{name:<12} {status}")
    if all(s != "ok" for _, s in results):
        raise typer.Exit(code=1)


def register(app: typer.Typer) -> None:
    app.command("screen")(screen)
    app.add_typer(review_app, name="review")
    app.add_typer(briefings_app, name="briefings")
    app.command("approve")(approve_cmd)
    app.command("reject")(reject_cmd)
    app.add_typer(orders_app, name="orders")
    app.command("kill-switch")(kill_switch_cmd)
    app.add_typer(core_app, name="core")
    app.add_typer(eval_app, name="eval")
    app.add_typer(digest_app, name="digest")
    app.add_typer(ops_app, name="ops")
    app.add_typer(gate_app, name="gate")


def command_exists(app: typer.Typer, command: str) -> bool:
    """True if ``committee <command>`` resolves to a real command (used by tests)."""
    cur: Any = typer.main.get_command(app)
    for a in shlex.split(command):
        if a.startswith("-") or not hasattr(cur, "get_command"):
            break
        nxt = cur.get_command(None, a)
        if nxt is None:
            return False
        cur = nxt
    return not hasattr(cur, "get_command")
