"""Offline end-to-end demo on synthetic data with a simulated committee.

Creates a fresh project root, then: synthetic lake -> holdings import -> weekly
screen -> committee reviews -> approval (on a SIMULATED clock that skips the
cooling-off wait) -> paper orders on the offline simulator -> fill reconciliation
into tax lots -> core drift -> digest -> journal verification -> gate check.
"""

from __future__ import annotations

import datetime as dt
import shutil
from collections.abc import Callable
from pathlib import Path

from committee import services
from committee.agents.prompts import PromptRegistry
from committee.agents.runtime import AgentRuntime
from committee.broker.approval import approve
from committee.broker.factory import make_broker
from committee.broker.gateway import Caps, OrderGateway
from committee.broker.models import ApprovedLeg
from committee.context import AppContext
from committee.core.drift import compute_drift
from committee.core.holdings import HoldingsStore, Position
from committee.demo.sim_llm import SimulatedCommitteeClient
from committee.demo.synthetic import build_lake
from committee.ops.gates import run_gate_check
from committee.orchestration.review import Orchestrator
from committee.signals.persist import journal_screen, persist_signals
from committee.signals.screen import run_screen
from committee.ui.digest import build_digest

PROJECT = Path(__file__).resolve().parents[3]


def init_root(root: Path) -> None:
    if (root / "var").exists():
        raise FileExistsError(f"{root} already has var/; use an empty directory")
    root.mkdir(parents=True, exist_ok=True)
    for d in ("config", "prompts"):
        if not (root / d).exists():
            shutil.copytree(PROJECT / d, root / d)
    if not (root / "pyproject.toml").exists():
        shutil.copy(PROJECT / "pyproject.toml", root / "pyproject.toml")
    (root / ".env").write_text("BACKUP_PASSPHRASE=demo-only-passphrase\n")


def seed_holdings(
    ctx: AppContext, total: float, now: dt.datetime, price: Callable[[str], float]
) -> None:
    pos: list[Position] = []
    for s in ctx.config.policy_portfolio.sleeves:
        if s.holding:
            pos.append(
                Position(
                    s.location[0],
                    s.holding,
                    round(total * s.target_pct / 100 / price(s.holding), 4),
                )
            )
    pos += [Position("ira", "CASH", total * 0.15), Position("taxable", "CASH", total * 0.05)]
    store = HoldingsStore(ctx.state_db)
    for acct in ("taxable", "ira"):
        store.snapshot(acct, pos, "demo", now)
    store.close()


def run_demo(
    root: Path,
    asof: dt.date,
    reviews: int = 3,
    say: Callable[[str], None] = print,
    optimism: float = 0.15,
) -> dict[str, object]:
    init_root(root)
    ctx = AppContext.load(root)
    now = dt.datetime.now(dt.UTC)
    say(f"1. synthetic lake as of {asof}")
    built = build_lake(services.lake(ctx), asof)
    say(f"   {built['counts']}")
    p = services.pit(ctx)
    seed_holdings(ctx, 1_000_000.0, now, services.price_lookup(p, asof))
    summary: dict[str, object] = {}
    with ctx.journal() as j:
        say("2. weekly screen")
        result = run_screen(p, asof, ctx.config.signals, ctx.config.universe)
        persist_signals(services.lake(ctx), result)
        p.refresh()
        journal_screen(j, result, dict(ctx.config.signals.weights))
        say(
            f"   universe {result.universe_size}, shortlist {[r.ticker for r in result.shortlist][:10]}"
        )
        say(f"3. committee reviews (SIMULATED agents) on the top {reviews}")
        rt = AgentRuntime(
            SimulatedCommitteeClient(optimism=optimism),
            ctx.config.models,
            PromptRegistry.load(root / "prompts"),
            run_id="demo",
            journal=j,
        )
        briefings = []
        for row in result.shortlist[:reviews]:
            bucket = row.bucket or "core_pick"
            inp, rep = services.build_review_inputs(
                ctx,
                j,
                p,
                row.ticker,
                asof,
                bucket,
                lottery_pass=row.lottery.passed if row.lottery else None,
            )
            out = Orchestrator(rt, j, ctx.config, scenario_report=rep).review(
                inp, f"{row.security_id}-{asof:%Y%m%d}"
            )
            say(f"   {row.ticker}: {out.state} {out.reason}")
            if out.briefing is not None:
                briefings.append(out.briefing)
                say(
                    f"     -> {out.briefing.payload['recommendation']} size {out.briefing.payload['size_pct_total']}%"
                )
        summary["reviews"] = len(briefings)
        actionable = [b for b in briefings if b.payload["legs"]]
        if actionable:
            b = actionable[0]
            leg = b.payload["legs"][0]
            simulated_now = b.created_at + dt.timedelta(
                hours=b.payload["cooling_off_hours"], minutes=1
            )
            say(
                f"4. approve {b.payload['symbol']} at {leg['max_pct_total']}% on a SIMULATED clock ({simulated_now:%Y-%m-%d %H:%M} UTC)"
            )
            state, _ = services.portfolio_state(ctx, j, p, asof)
            _, a = approve(
                j,
                b.hash,
                [
                    ApprovedLeg(
                        symbol=leg["symbol"],
                        side=leg["side"],
                        account=leg["account"],
                        pct_total=leg["max_pct_total"],
                    )
                ],
                "Demo approval: engine-sized position, thesis is simulated.",
                state.total_value,
                simulated_now,
            )
            say("5. paper orders on the offline simulator, then reconcile fills into tax lots")
            gw = OrderGateway(
                j,
                make_broker(ctx, j),
                ctx.flags(),
                Caps(),
                services.price_lookup(p, asof),
            )
            placed = gw.place(a.hash, state.total_value)
            fills = gw.reconcile(on_fill=services.fill_to_ledger(ctx))
            say(f"   {len(placed)} orders, {len(fills)} fills reconciled")
            summary["orders"] = len(placed)
        else:
            say("4. no BUY recommendations survived the gates this week (that is a normal outcome)")
        say("6. core drift")
        pos = services.positions(ctx)
        d = compute_drift(
            ctx.config.policy_portfolio,
            pos,
            services.price_lookup(p, asof),
            satellite_symbols=set(services.satellite_buckets(j)),
        )
        for s in d.sleeves:
            say(
                f"   {s.sleeve_id:<10} target {s.target_pct:5.1f}% actual {s.actual_pct:5.1f}%{'  BREACH' if s.breached else ''}"
            )
        say("7. digest")
        say("   " + build_digest(j, dt.datetime.now(dt.UTC)).text().replace("\n", "\n   "))
        rep_ = j.verify()
        say(f"8. journal verify: {'OK' if rep_.ok else rep_.first_break} ({rep_.entries} entries)")
        gates, _ = run_gate_check(
            j, root, ctx.path(ctx.config.app.broker.live_gate_file), dt.datetime.now(dt.UTC)
        )
        say(
            "9. gate check: "
            + ", ".join(f"{g.id} {'PASS' if g.ok else 'FAIL'}" for g in gates)
            + " (expected: a new system fails every gate)"
        )
        summary.update(journal_ok=rep_.ok, entries=rep_.entries)
    p.close()
    return summary
