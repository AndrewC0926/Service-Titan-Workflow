"""The one-screen briefing (DESIGN 7 Chair, 8): the journaled artifact a human
approves or rejects. Its payload also carries the approval-gate fields
(recommendation, legs, cooling_off_hours, behavioral_severity)."""

from __future__ import annotations

import datetime as dt
import html

from pydantic import Field

from committee.broker.models import OrderLeg
from committee.domain import Model

FOOTER = "Research, not advice. The human decides."


class ForecastLine(Model):
    event: str
    horizon_months: int
    probability: float = Field(ge=0, le=1)


class ScenarioPayoff(Model):
    scenario: str
    probability: float = Field(ge=0, le=1)
    return_36m: float


class Falsifier(Model):
    observable: str
    threshold: str
    check_by: dt.date


class BriefingPayload(Model):
    review_id: str
    symbol: str
    security_id: str
    anon_id: str
    asof: dt.date
    bucket: str
    # approval-gate fields
    recommendation: str
    legs: list[OrderLeg]
    cooling_off_hours: int
    behavioral_severity: str
    # content
    thesis: str
    forecasts: list[ForecastLine]
    scenario_payoffs: list[ScenarioPayoff]
    falsifiers: list[Falsifier]
    kill_criteria: list[str]
    base_rate_p_beat_12m: float | None
    bear_strongest_point: str
    bear_premortem: str
    risk_verdict: str
    risk_max_size_pct: float
    risk_binding: list[str]
    risk_text: str
    tax_text: str
    tax_account: str | None
    behavioral_flags: list[str]
    behavioral_text: str
    gate_notes: list[str] = Field(default_factory=list)
    size_pct_total: float
    agent_output_hashes: dict[str, str]
    model_cohort: str
    cost_usd: float = 0.0
    flags: list[str] = Field(default_factory=list)


def _pct(x: float) -> str:
    return f"{100 * x:.0f}%"


def render_markdown(b: BriefingPayload) -> str:
    f = {(x.event, x.horizon_months): x.probability for x in b.forecasts}
    lines = [
        f"# {b.symbol} — {b.recommendation} ({b.bucket.replace('_', ' ')})",
        f"As of {b.asof} · size {b.size_pct_total:.2f}% of account (risk max {b.risk_max_size_pct:.2f}%) · "
        f"risk {b.risk_verdict} · cooling-off {b.cooling_off_hours}h",
        "",
        f"**Thesis.** {b.thesis}",
        "",
        "| Forecast | 3m | 6m | 12m | 36m |",
        "|---|---|---|---|---|",
        "| Beats benchmark | "
        + " | ".join(
            _pct(f[("beats_benchmark", h)]) if ("beats_benchmark", h) in f else "–"
            for h in (3, 6, 12)
        )
        + " | – |",
        "| 30% drawdown | – | – | "
        + (_pct(f[("drawdown_exceeds_30pct", 12)]) if ("drawdown_exceeds_30pct", 12) in f else "–")
        + " | – |",
        "| Doubles / loses half | – | – | – | "
        + (
            f"{_pct(f[('doubles', 36)])} / {_pct(f[('loses_50pct', 36)])}"
            if ("doubles", 36) in f and ("loses_50pct", 36) in f
            else "–"
        )
        + " |",
        "",
        f"Base rate P(beat, 12m): {_pct(b.base_rate_p_beat_12m) if b.base_rate_p_beat_12m is not None else 'n/a'} · "
        "Scenarios: "
        + ", ".join(
            f"{s.scenario} {_pct(s.probability)} → {s.return_36m:+.0%}" for s in b.scenario_payoffs
        ),
        "",
        f"**Bear's strongest point.** {b.bear_strongest_point}",
        f"**Pre-mortem.** {b.bear_premortem}",
        "",
        f"**Risk.** {b.risk_text}",
        f"**Tax.** {b.tax_text}",
        f"**Behavior ({b.behavioral_severity}).** {b.behavioral_text}",
        "",
        "**Falsifiers:** "
        + "; ".join(f"{x.observable} {x.threshold} by {x.check_by}" for x in b.falsifiers),
        "**Kill criteria:** " + "; ".join(b.kill_criteria),
    ]
    if b.gate_notes:
        lines += ["", "**Code gate notes:** " + "; ".join(b.gate_notes)]
    if b.flags:
        lines += ["**Flags:** " + ", ".join(b.flags)]
    lines += ["", f"_{FOOTER}_"]
    return "\n".join(lines)


def render_html(b: BriefingPayload) -> str:
    """Minimal, phone-readable HTML (used by the digest)."""
    body = html.escape(render_markdown(b)).replace("\n", "<br>")
    return f"<div style='font-family:system-ui;max-width:720px'>{body}</div>"
