"""Chair decision gates as deterministic code (DESIGN 7 Chair task 6, DESIGN 8).

The Chair's prompt states the rules, but code enforces them: if the Chair's
recommendation violates a gate it is downgraded here, and the reason recorded.

- CORE_PICK BUY/ADD: P(beat, 12m) >= 0.58 and a passing risk verdict.
- ASYMMETRIC_BET BUY/ADD: EV of scenario payoffs > 0 after the round-trip cost,
  P(doubles, 36m) >= 1.5 x P(loses 50%, 36m), lottery filter pass, risk pass.
- Size <= risk engine max x scenario risk-budget modifier, never larger.

"Passing" risk verdict means PASS or RESIZE (RESIZE allows the trade at the
engine's reduced maximum); VETO fails. Downgrades: a risk VETO or a failed
lottery filter make the idea ineligible (PASS); a probability or EV gate
failure keeps it on the watch list (WATCH). Non-buy recommendations carry no
new size.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from committee.agents.engine_inputs import EngineInputs
from committee.agents.schemas import ChairOutput, expected_payoff
from committee.domain import Recommendation

CORE_MIN_P_BEAT_12M = 0.58
ASYM_SKEW_RATIO = 1.5
BUY_ACTIONS: frozenset[str] = frozenset({"BUY", "ADD"})


class GateResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    chair_recommendation: Recommendation
    chair_size_pct_total: float
    recommendation: Recommendation
    size_pct_total: float
    size_cap_pct_total: float
    expected_value_after_cost: float
    violations: list[str]
    downgraded: bool
    size_clamped: bool


def apply_gates(chair: ChairOutput, engines: EngineInputs) -> GateResult:
    f = chair.forecasts
    risk_pass = engines.risk.verdict in ("PASS", "RESIZE")
    cap = round(engines.risk.max_size_pct_total * engines.risk_budget_modifier, 6)
    ev = expected_payoff(chair.scenario_payoffs) - engines.round_trip_cost
    rec: Recommendation = chair.recommendation
    violations: list[str] = []
    ineligible = False

    if chair.bucket_tag != engines.bucket_tag:  # also enforced by schema validation
        violations.append(f"bucket tag {chair.bucket_tag} != deterministic {engines.bucket_tag}")
        ineligible = True

    if rec in BUY_ACTIONS:
        if not risk_pass:
            violations.append(
                f"risk verdict {engines.risk.verdict}"
                + (f" ({engines.risk.veto_rule})" if engines.risk.veto_rule else "")
            )
            ineligible = True
        if engines.bucket_tag == "CORE_PICK":
            if f.p_beat_12m < CORE_MIN_P_BEAT_12M:
                violations.append(
                    f"CORE_PICK requires P(beat,12m) >= {CORE_MIN_P_BEAT_12M}; got {f.p_beat_12m:.2f}"
                )
        else:
            if ev <= 0:
                violations.append(
                    f"ASYMMETRIC_BET requires EV > 0 after {engines.round_trip_cost:.1%} "
                    f"round-trip cost; got {ev:+.3f}"
                )
            if f.p_doubles_36m < ASYM_SKEW_RATIO * f.p_loses_50pct_36m:
                violations.append(
                    f"ASYMMETRIC_BET requires P(doubles,36m) >= {ASYM_SKEW_RATIO} x "
                    f"P(loses 50%,36m); got {f.p_doubles_36m:.2f} vs {f.p_loses_50pct_36m:.2f}"
                )
            if engines.lottery_filter_pass is not True:
                violations.append("ASYMMETRIC_BET requires a passing lottery filter")
                ineligible = True

    final: Recommendation = rec
    if violations and rec in BUY_ACTIONS:
        final = "PASS" if ineligible else "WATCH"

    size = chair.size_pct_total
    clamped = False
    if final in BUY_ACTIONS:
        if size > cap:
            violations.append(f"size {size:g}% exceeds risk max x modifier = {cap:g}%")
            size = cap
            clamped = True
    elif final in ("PASS", "WATCH", "HOLD"):
        size = 0.0

    return GateResult(
        chair_recommendation=rec,
        chair_size_pct_total=chair.size_pct_total,
        recommendation=final,
        size_pct_total=size,
        size_cap_pct_total=cap,
        expected_value_after_cost=round(ev, 6),
        violations=violations,
        downgraded=final != rec,
        size_clamped=clamped,
    )
