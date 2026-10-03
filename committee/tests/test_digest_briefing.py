from __future__ import annotations

import datetime as dt
from pathlib import Path

from committee.journal.store import Journal
from committee.orchestration.briefing import FOOTER, BriefingPayload, render_html, render_markdown
from committee.ui.digest import build_digest


def sample_briefing(**kw: object) -> BriefingPayload:
    base: dict[str, object] = dict(
        review_id="r1",
        symbol="ABC",
        security_id="CIK0000000001",
        anon_id="SEC-1A2B",
        asof=dt.date(2026, 10, 4),
        bucket="core_pick",
        recommendation="BUY",
        legs=[{"symbol": "ABC", "side": "buy", "account": "ira", "max_pct_total": 2.4}],
        cooling_off_hours=24,
        behavioral_severity="none",
        thesis="Cheap, improving margins. Insiders buying.",
        forecasts=[
            {"event": "beats_benchmark", "horizon_months": h, "probability": p}
            for h, p in ((3, 0.52), (6, 0.55), (12, 0.6))
        ]
        + [
            {"event": "drawdown_exceeds_30pct", "horizon_months": 12, "probability": 0.12},
            {"event": "doubles", "horizon_months": 36, "probability": 0.1},
            {"event": "loses_50pct", "horizon_months": 36, "probability": 0.05},
        ],
        scenario_payoffs=[
            {"scenario": "bear", "probability": 0.25, "return_36m": -0.3},
            {"scenario": "base", "probability": 0.5, "return_36m": 0.2},
            {"scenario": "bull", "probability": 0.25, "return_36m": 0.7},
        ],
        falsifiers=[{"observable": "gross margin", "threshold": "< 40%", "check_by": "2027-03-31"}],
        kill_criteria=["8-K 4.02 non-reliance"],
        base_rate_p_beat_12m=0.47,
        bear_strongest_point="Margins are cyclical.",
        bear_premortem="Demand rolled over.",
        risk_verdict="RESIZE",
        risk_max_size_pct=2.4,
        risk_binding=["sector_cap"],
        risk_text="Sector cap binds.",
        tax_text="Buy in the IRA.",
        tax_account="ira",
        behavioral_flags=[],
        behavioral_text="No flags.",
        size_pct_total=2.4,
        agent_output_hashes={"chair": "abc"},
        model_cohort="m1",
    )
    base.update(kw)
    return BriefingPayload.model_validate(base)


def test_render() -> None:
    md = render_markdown(sample_briefing(gate_notes=["downgraded"], flags=["CONTAMINATION_RISK"]))
    assert (
        "ABC — BUY" in md
        and "60%" in md
        and "10% / 5%" in md
        and md.rstrip().endswith(f"_{FOOTER}_")
    )
    assert "downgraded" in md and "CONTAMINATION_RISK" in md
    assert "<br>" in render_html(sample_briefing())


def test_digest(tmp_path: Path) -> None:
    t = dt.datetime(2026, 10, 5, 12, tzinfo=dt.UTC)
    clock = [t]
    j = Journal(tmp_path / "j.sqlite", clock=lambda: clock[0])
    j.append("briefing", sample_briefing().model_dump(mode="json"))
    j.append(
        "briefing", sample_briefing(symbol="XYZ", recommendation="PASS").model_dump(mode="json")
    )
    j.append(
        "state_transition", {"review_id": "r9", "symbol": "DEF", "reason": "trigger:8-K item 4.02"}
    )
    j.append("incident", {"level": "P2", "kind": "job_failed", "job": "dq_check"})
    j.append("dq_report", {"failures": ["prices_daily stale 3 days"]})
    clock[0] = t + dt.timedelta(hours=2)
    d = build_digest(
        j,
        clock[0],
        wash_sale_blocks={"VTI": dt.date(2026, 10, 30)},
        orders_blocked="kill_switch: drill",
    )
    assert len(d.awaiting) == 1 and "ABC" in d.awaiting[0] and "cooling-off 22h" in d.awaiting[0]
    assert d.triggers == ["DEF: 8-K item 4.02"]
    assert (
        d.dq == ["prices_daily stale 3 days"]
        and d.incidents
        and d.wash_sale == ["VTI until 2026-10-30"]
    )
    txt = d.text()
    assert "ORDERS BLOCKED" in txt and "No trading from the digest" in txt
    assert "1 awaiting" in d.subject
