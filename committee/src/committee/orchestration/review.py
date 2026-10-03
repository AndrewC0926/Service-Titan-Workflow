"""Committee review orchestrator: an explicit state machine, not a chat (DESIGN 8).

Each state's inputs and outputs are journaled. Agents only read, argue and
forecast; code computes eligibility, size, tax and gates. A Risk VETO (or a
wash-sale block) ends the review early, but its forecasts are still
pre-registered so the filters can be scored. Any failure parks the review in
NEEDS_ATTENTION with the reason.

Sizing happens twice in code: before the Chair (Kelly from the Brier-weighted
analyst consensus) to give the Chair a ceiling, and after the Chair (Kelly from
the Chair's own probabilities). The briefing size is the smallest of the gate
result and both engine maxima.
"""

from __future__ import annotations

import datetime as dt
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel

from committee.agents.engine_inputs import EngineInputs, RiskEngineView, TaxEngineView
from committee.agents.packets import PacketSource, ReviewPacket, build_review_packet
from committee.agents.runtime import AgentResult, AgentRuntime
from committee.agents.schemas import AnalystOutput, ChairOutput, NewsOutput
from committee.agents.steps import (
    review_seed,
    run_analysts,
    run_base_rate,
    run_bear,
    run_behavioral,
    run_chair,
    run_macro_name,
    run_risk_explainer,
    run_tax_explainer,
)
from committee.config.schema import AppConfig
from committee.domain import AccountKind, Bucket
from committee.engines.risk import Outcome, PortfolioState, Proposal, RiskResult, evaluate
from committee.engines.scenario.models import ScenarioReport
from committee.engines.tax.ledger import LotLedger
from committee.engines.tax.location import recommend_location
from committee.engines.tax.wash_sale import WashSaleGuard
from committee.journal.store import Journal, JournalEntry
from committee.orchestration.briefing import BriefingPayload, render_markdown
from committee.orchestration.states import current_state, transition

ANALYSTS = ("fundamentals", "valuation", "filings_insiders", "news_narrative")
TAG = {"core_pick": "CORE_PICK", "asymmetric_bet": "ASYMMETRIC_BET"}


@dataclass
class ReviewInputs:
    """Everything code knows about the idea before any agent runs."""

    security_id: str
    symbol: str
    asof: dt.date
    bucket: Bucket
    sector: str
    market_cap_usd: float
    adv_usd: float
    annual_vol: float
    price: float
    source: PacketSource
    portfolio: PortfolioState
    cash_by_account: Mapping[AccountKind, float]
    tax_ledger: LotLedger
    lottery_pass: bool | None = None
    themes: Mapping[str, float] = field(default_factory=dict)
    extras: Mapping[str, Sequence[Mapping[str, Any]]] = field(default_factory=dict)
    decision_history: Sequence[Mapping[str, Any]] = ()
    origin: str = "weekly screen"


@dataclass
class ReviewOutcome:
    review_id: str
    state: str
    briefing: JournalEntry | None = None
    reason: str = ""
    cost_usd: float = 0.0
    results: dict[str, AgentResult[Any]] = field(default_factory=dict)


def _annualize(r36: float) -> float:
    return float((1 + max(r36, -0.99)) ** (1 / 3) - 1)


def consensus(
    outputs: Mapping[str, AnalystOutput], weights: Mapping[str, float]
) -> tuple[float, list[Outcome]]:
    """Brier-weighted P(beat, 12m) and averaged scenario payoffs (annualized)."""
    num = den = 0.0
    scen: dict[str, list[tuple[float, float, float]]] = {}
    for agent, out in outputs.items():
        w = float(weights.get(agent, 1.0))
        for f in out.forecasts:
            if f.event == "beats_benchmark" and f.horizon_months == 12:
                num += w * f.probability
                den += w
        for s in out.scenario_payoffs:
            scen.setdefault(s.scenario, []).append((w, s.probability, s.return_36m))
    p = num / den if den else 0.5
    outcomes = []
    for name, rows in sorted(scen.items()):
        tw = sum(w for w, _, _ in rows)
        outcomes.append(
            Outcome(
                label=name,
                probability=sum(w * pr for w, pr, _ in rows) / tw,
                ret=_annualize(sum(w * r for w, _, r in rows) / tw),
            )
        )
    tot = sum(o.probability for o in outcomes)
    if tot > 0:
        outcomes = [o.model_copy(update={"probability": o.probability / tot}) for o in outcomes]
    return p, outcomes


class Orchestrator:
    def __init__(
        self,
        rt: AgentRuntime,
        journal: Journal,
        config: AppConfig,
        *,
        scenario_report: ScenarioReport | None = None,
        brier_weights: Mapping[str, float] | None = None,
    ) -> None:
        self.rt = rt
        self.journal = journal
        self.cfg = config
        self.scenario_report = scenario_report
        self.weights = dict(brier_weights or {})

    # ------------------------------------------------------------------ helpers
    def _t(self, rid: str, to: str, reason: str = "", **extra: Any) -> None:
        transition(self.journal, rid, to, reason, **extra)

    @property
    def modifier(self) -> float:
        return self.scenario_report.modifier if self.scenario_report else 1.0

    def _risk(
        self, inp: ReviewInputs, p: float, outcomes: list[Outcome], size: float
    ) -> RiskResult:
        prop = Proposal(
            symbol=inp.symbol,
            side="buy",
            bucket=inp.bucket,
            sector=inp.sector,
            themes=dict(inp.themes),
            market_cap_usd=inp.market_cap_usd,
            adv_usd=inp.adv_usd,
            annual_vol=inp.annual_vol,
            price=inp.price,
            p_beat_12m=p,
            outcomes=outcomes,
            size_pct_total=size,
        )
        return evaluate(
            prop,
            inp.portfolio,
            self.cfg.risk_limits,
            lookthrough=self.cfg.policy_portfolio.lookthrough,
            scenario_report=self.scenario_report,
            modifier=self.modifier,
        )

    def _tax(self, inp: ReviewInputs, size_pct: float) -> tuple[TaxEngineView, str | None]:
        amount = max(1.0, inp.portfolio.total_value * size_pct / 100)
        loc = recommend_location(
            "satellite", amount, dict(inp.cash_by_account), self.cfg.tax.location_preference
        )
        account: AccountKind = loc.account or (loc.preference[0] if loc.preference else "ira")
        verdict = WashSaleGuard(inp.tax_ledger).check_purchase(inp.symbol, account, inp.asof)
        flags = [] if loc.account else [f"purchase does not fit one account: {loc.reason}"]
        view = TaxEngineView(
            recommended_account=account,
            wash_sale_blocked=not verdict.allowed,
            wash_sale_window_ends=verdict.clear_on,
            after_tax_hurdle_pct=0.0,
            holding_period_warnings=[],
            cpa_flags=flags,
        )
        return view, (verdict.reason if not verdict.allowed else None)

    def _risk_view(self, r: RiskResult) -> RiskEngineView:
        return RiskEngineView(
            verdict=r.verdict,
            max_size_pct_total=round(r.max_order_pct_total, 6),
            binding_constraints=[c.rule_id for c in r.binding],
            scenario_losses_pct={
                s.scenario_id: round(100 * s.satellite_return, 4) for s in r.scenario_losses
            },
            veto_rule=r.veto.text if r.veto else None,
        )

    def _preregister(
        self, rid: str, results: Mapping[str, AgentResult[Any]], chair: ChairOutput | None
    ) -> None:
        """Write every forecast to the journal before its outcome window opens."""
        for agent, res in results.items():
            out = res.output
            cohort = f"{res.model_id}:{res.prompt_hash[:12]}"
            for f in (
                getattr(out, "forecasts", [])
                if isinstance(getattr(out, "forecasts", None), list)
                else []
            ):
                self.journal.append(
                    "forecast",
                    {
                        "thesis_id": rid,
                        "agent": agent,
                        "event": f.event,
                        "horizon_months": f.horizon_months,
                        "probability": f.probability,
                        "cohort": cohort,
                    },
                )
        if chair is not None:
            cf = chair.forecasts
            res = results["chair"]
            cohort = f"{res.model_id}:{res.prompt_hash[:12]}"
            for event, h, p in (
                ("beats_benchmark", 3, cf.p_beat_3m),
                ("beats_benchmark", 6, cf.p_beat_6m),
                ("beats_benchmark", 12, cf.p_beat_12m),
                ("drawdown_exceeds_30pct", 12, cf.p_drawdown_30pct_12m),
                ("doubles", 36, cf.p_doubles_36m),
                ("loses_50pct", 36, cf.p_loses_50pct_36m),
            ):
                self.journal.append(
                    "forecast",
                    {
                        "thesis_id": rid,
                        "agent": "chair",
                        "event": event,
                        "horizon_months": h,
                        "probability": p,
                        "cohort": cohort,
                    },
                )

    def existing(self, rid: str) -> ReviewOutcome | None:
        state = current_state(self.journal, rid)
        if state in ("AWAITING_APPROVAL", "VETOED") or state in (
            "APPROVED",
            "REJECTED",
            "EXPIRED",
            "ORDERED",
            "FILLED",
            "MONITORING",
        ):
            b = next(
                (e for e in self.journal.entries("briefing") if e.payload.get("review_id") == rid),
                None,
            )
            return ReviewOutcome(rid, state, b, "already reviewed")
        return None

    # -------------------------------------------------------------------- run
    def review(self, inp: ReviewInputs, review_id: str) -> ReviewOutcome:
        done = self.existing(review_id)
        if done is not None:
            return done
        out = ReviewOutcome(review_id, "")
        try:
            return self._review(inp, review_id, out)
        except Exception as e:  # park, never guess
            reason = f"{type(e).__name__}: {e}"[:500]
            if current_state(self.journal, review_id) not in ("", "NEEDS_ATTENTION"):
                self._t(review_id, "NEEDS_ATTENTION", reason, symbol=inp.symbol)
            out.state, out.reason = "NEEDS_ATTENTION", reason
            out.cost_usd = sum(r.cost_usd for r in out.results.values())
            return out

    def _review(self, inp: ReviewInputs, rid: str, out: ReviewOutcome) -> ReviewOutcome:
        sym = {"symbol": inp.symbol}
        if current_state(self.journal, rid) in ("NEEDS_ATTENTION", "EXPIRED"):
            pass
        self._t(rid, "SCREENED", inp.origin, security_id=inp.security_id, **sym)
        tag = TAG[inp.bucket]
        default_size = (
            self.cfg.risk_limits.buckets.core_pick.default_initial_pct_total
            if inp.bucket == "core_pick"
            else self.cfg.risk_limits.buckets.asymmetric_bet.default_initial_pct_total
        )
        extras: dict[str, list[dict[str, Any]]] = {
            k: [dict(r) for r in v] for k, v in inp.extras.items()
        }
        extras.setdefault(
            "proposal",
            [
                {
                    "action": "BUY",
                    "bucket": inp.bucket,
                    "origin": inp.origin,
                    "position_weight_pct": 0.0,
                }
            ],
        )
        extras.setdefault("decision_history", [dict(d) for d in inp.decision_history])
        packet: ReviewPacket = build_review_packet(
            inp.source,
            inp.security_id,
            inp.asof,
            rid,
            bucket_tag=tag,
            context={"benchmark": self.rt.benchmark, "position_weight_pct": 0.0},
            extras=extras,
        )
        R = out.results
        # Base rate first, then the analysts and Macro, then the Bear.
        self._t(rid, "BASE_RATE", packet_items=len(packet.items), **sym)
        R["base_rate"] = run_base_rate(self.rt, packet)
        self._t(rid, "ANALYSTS", **sym)
        R.update(run_analysts(self.rt, packet, R["base_rate"].output))
        R["macro_scenario"] = run_macro_name(self.rt, packet)
        self._t(rid, "BEAR", **sym)
        analysts: dict[str, BaseModel] = {k: R[k].output for k in ("base_rate", *ANALYSTS)}
        analysts["macro_scenario"] = R["macro_scenario"].output
        R["bear"] = run_bear(self.rt, packet, analysts, seed=review_seed(self.rt.run_id, rid))
        # Deterministic engines.
        self._t(rid, "RISK", **sym)
        p, outcomes = consensus(
            {k: R[k].output for k in ("base_rate", *ANALYSTS, "bear")}, self.weights
        )
        pre = self._risk(inp, p, outcomes, default_size)
        risk_view = self._risk_view(pre)
        self.journal.append(
            "note",
            {
                "kind": "risk_result",
                "review_id": rid,
                "stage": "pre_chair",
                **pre.model_dump(mode="json", exclude={"caps"}),
            },
        )
        if pre.verdict == "VETO":
            self._preregister(rid, R, None)
            self._t(rid, "VETOED", f"risk veto: {risk_view.veto_rule}", **sym)
            out.state, out.reason = "VETOED", f"risk veto: {risk_view.veto_rule}"
            out.cost_usd = sum(r.cost_usd for r in R.values())
            return out
        self._t(rid, "TAX", **sym)
        tax_view, wash = self._tax(inp, pre.max_order_pct_total)
        if wash:
            self._preregister(rid, R, None)
            self._t(rid, "VETOED", f"wash-sale block: {wash}", **sym)
            out.state, out.reason = "VETOED", f"wash-sale block: {wash}"
            out.cost_usd = sum(r.cost_usd for r in R.values())
            return out
        engines = EngineInputs(
            bucket_tag=tag,
            lottery_filter_pass=inp.lottery_pass if inp.bucket == "asymmetric_bet" else None,
            risk=risk_view,
            tax=tax_view,
            risk_budget_modifier=max(0.5, min(1.0, self.modifier)),
            brier_weights=self.weights,
        )
        # Engine outputs are the last evidence kinds, so rebuilding keeps every earlier id.
        full = build_review_packet(
            inp.source,
            inp.security_id,
            inp.asof,
            rid,
            bucket_tag=tag,
            context={"benchmark": self.rt.benchmark, "position_weight_pct": 0.0},
            extras={**extras, **engines.as_evidence()},
        )
        if full.items[: len(packet.items)] != packet.items:
            raise RuntimeError("evidence ids shifted when engine outputs were added")
        packet = full
        R["risk_explainer"] = run_risk_explainer(self.rt, packet, engines)
        R["tax_explainer"] = run_tax_explainer(self.rt, packet, engines)
        self._t(rid, "BEHAVIORAL", **sym)
        R["behavioral_auditor"] = run_behavioral(self.rt, packet)
        self._t(rid, "CHAIR", **sym)
        outputs = {k: v.output for k, v in R.items()}
        decision = run_chair(self.rt, packet, engines, outputs)
        R["chair"] = decision.result
        chair, gates = decision.result.output, decision.gates
        # Post-Chair sizing from the Chair's own probabilities (code, not prompt).
        chair_outcomes = [
            Outcome(label=s.scenario, probability=s.probability, ret=_annualize(s.return_36m))
            for s in chair.scenario_payoffs
        ]
        post = self._risk(
            inp,
            chair.forecasts.p_beat_12m,
            chair_outcomes,
            max(gates.size_pct_total, 0.0) or default_size,
        )
        size = 0.0
        notes = list(gates.violations)
        rec = gates.recommendation
        if rec in ("BUY", "ADD"):
            if post.verdict == "VETO":
                rec, notes = (
                    "PASS",
                    [*notes, f"post-Chair risk veto: {post.veto.text if post.veto else ''}"],
                )
            else:
                size = max(
                    0.0,
                    min(gates.size_pct_total, post.max_order_pct_total, pre.max_order_pct_total),
                )
                size = math.floor(size * 100) / 100
                if size <= 0:
                    rec, notes = "WATCH", [*notes, "no size left after engine caps"]
        beh = R["behavioral_auditor"].output
        cooling = max(self.cfg.risk_limits.cooling_off_hours.default, int(beh.cooling_off_hours))
        if beh.severity == "stop":
            cooling = max(cooling, self.cfg.risk_limits.cooling_off_hours.behavioral_stop)
        legs = (
            [
                {
                    "symbol": inp.symbol,
                    "side": "buy",
                    "account": tax_view.recommended_account,
                    "max_pct_total": size,
                }
            ]
            if size > 0
            else []
        )
        bear = R["bear"].output
        news = R["news_narrative"].output
        flags = sorted({f for r in R.values() for f in (getattr(r.output, "flags", None) or [])})
        payload = BriefingPayload(
            review_id=rid,
            symbol=inp.symbol,
            security_id=inp.security_id,
            anon_id=packet.anon_id,
            asof=inp.asof,
            bucket=inp.bucket,
            recommendation=rec,
            legs=legs,
            cooling_off_hours=cooling,
            behavioral_severity=beh.severity,
            thesis=chair.thesis,
            forecasts=[
                {"event": e, "horizon_months": h, "probability": pr}
                for e, h, pr in (
                    ("beats_benchmark", 3, chair.forecasts.p_beat_3m),
                    ("beats_benchmark", 6, chair.forecasts.p_beat_6m),
                    ("beats_benchmark", 12, chair.forecasts.p_beat_12m),
                    ("drawdown_exceeds_30pct", 12, chair.forecasts.p_drawdown_30pct_12m),
                    ("doubles", 36, chair.forecasts.p_doubles_36m),
                    ("loses_50pct", 36, chair.forecasts.p_loses_50pct_36m),
                )
            ],
            scenario_payoffs=[s.model_dump() for s in chair.scenario_payoffs],
            falsifiers=[f.model_dump() for f in chair.falsifiers],
            kill_criteria=[
                *chair.kill_criteria,
                *(f"{k.observable} {k.threshold}" for k in bear.kill_criteria),
            ],
            base_rate_p_beat_12m=next(
                (
                    f.probability
                    for f in R["base_rate"].output.forecasts
                    if f.event == "beats_benchmark" and f.horizon_months == 12
                ),
                None,
            ),
            bear_strongest_point=chair.bear_response,
            bear_premortem=bear.premortem,
            risk_verdict=post.verdict if rec in ("BUY", "ADD") else pre.verdict,
            risk_max_size_pct=round(min(pre.max_order_pct_total, post.max_order_pct_total), 4),
            risk_binding=sorted({c.rule_id for c in (*pre.binding, *post.binding)}),
            risk_text=R["risk_explainer"].output.explanation,
            tax_text=R["tax_explainer"].output.explanation,
            tax_account=tax_view.recommended_account,
            behavioral_flags=[f"{f.kind}: {f.detail}" for f in beh.flags],
            behavioral_text="; ".join(f.detail for f in beh.flags) or "No behavioral flags.",
            gate_notes=notes,
            size_pct_total=size,
            agent_output_hashes={},
            model_cohort=f"{R['chair'].model_id}:{R['chair'].prompt_hash[:12]}",
            cost_usd=round(sum(r.cost_usd for r in R.values()), 6),
            flags=flags,
        )
        self._preregister(rid, R, chair)
        entry = self.journal.append(
            "briefing",
            {
                **payload.model_dump(mode="json"),
                "markdown": render_markdown(payload),
                "rereview_triggers": list(news.rereview_triggers)
                if isinstance(news, NewsOutput)
                else [],
                "entry_price": inp.price,
            },
        )
        self._t(rid, "BRIEFED", briefing_hash=entry.hash, **sym)
        self._t(rid, "AWAITING_APPROVAL", **sym)
        out.state, out.briefing = "AWAITING_APPROVAL", entry
        out.cost_usd = payload.cost_usd
        return out
