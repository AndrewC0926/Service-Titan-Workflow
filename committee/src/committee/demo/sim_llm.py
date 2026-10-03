"""A deterministic stand-in for the LLM, for offline demos and integration tests.

It is NOT a model: every output is computed by simple rules from the evidence
packet (base rate, signal z-scores) so the whole pipeline — validation, gates,
engines, journal, approval — can run without network access. Outputs are
labelled with the SIMULATED flag so they can never be mistaken for real
committee research.
"""

from __future__ import annotations

import datetime as dt
import json
from typing import Any

from committee.agents.llm import LLMRequest, LLMResponse, Usage

FLAG = "SIMULATED"


def _packet(req: LLMRequest) -> dict[str, Any]:
    return dict(json.loads(req.packet.split("\n", 1)[1]))


def _first(items: list[dict[str, Any]], kind: str | None = None) -> str | None:
    for i in items:
        if kind is None or i["kind"] == kind:
            return str(i["id"])
    return None


def _base_p(items: list[dict[str, Any]]) -> float:
    for i in items:
        if i["kind"] == "base_rate_table":
            for o in i["data"].get("outcomes", []):
                if (
                    o.get("event") == "beats_benchmark"
                    and o.get("horizon_months") == 12
                    and o.get("frequency") is not None
                ):
                    return float(o["frequency"])
    return 0.45


def _tilt(items: list[dict[str, Any]], optimism: float) -> float:
    z = sum(float(i["data"].get("zscore") or 0) for i in items if i["kind"] == "signals")
    cap = 0.08 + optimism
    return max(-cap, min(cap, (0.02 + optimism / 4) * z + optimism))


class SimulatedCommitteeClient:
    def __init__(self, optimism: float = 0.0) -> None:
        """``optimism`` shifts analyst probabilities up (demo only, to exercise the BUY path)."""
        self.calls = 0
        self.optimism = optimism

    def complete(self, request: LLMRequest) -> LLMResponse:
        self.calls += 1
        pkt = _packet(request)
        body = getattr(self, f"_{request.agent}", self._analyst)(request.agent, pkt)
        return LLMResponse(
            text=json.dumps(body), model_id=request.model, usage=Usage(), stop_reason="end_turn"
        )

    # ---------------------------------------------------------------- pieces
    @staticmethod
    def _asof(pkt: dict[str, Any]) -> dt.date:
        return dt.date.fromisoformat(str(pkt["asof"])[:10])

    def _common(self, agent: str, pkt: dict[str, Any]) -> dict[str, Any]:
        return {"agent": agent, "anon_id": pkt["anon_id"], "flags": [FLAG]}

    def _payoffs(self, p: float) -> list[dict[str, Any]]:
        bull = round(min(0.45, max(0.15, p - 0.2)), 2)
        bear = round(min(0.45, max(0.15, 0.55 - p)), 2)
        return [
            {"scenario": "bear", "probability": bear, "return_36m": -0.35},
            {"scenario": "base", "probability": round(1 - bull - bear, 2), "return_36m": 0.15},
            {"scenario": "bull", "probability": bull, "return_36m": 0.80},
        ]

    def _analyst(self, agent: str, pkt: dict[str, Any]) -> dict[str, Any]:
        items = pkt["items"]
        p = round(
            min(
                0.75,
                max(
                    0.25,
                    _base_p(items) + (0 if agent == "base_rate" else _tilt(items, self.optimism)),
                ),
            ),
            3,
        )
        eid = _first(items)
        bucket = "core_pick" if pkt.get("bucket_tag") != "ASYMMETRIC_BET" else "asymmetric_bet"
        out: dict[str, Any] = {
            **self._common(agent, pkt),
            "bucket": bucket,
            "thesis_points": [
                {
                    "claim": f"Rule-based summary of {items[0]['label']}",
                    "evidence_ids": [eid],
                    "direction": "neutral",
                    "strength": 2,
                }
            ]
            if eid
            else [],
            "forecasts": [
                {"event": "beats_benchmark", "horizon_months": 12, "probability": p},
                {"event": "drawdown_exceeds_30pct", "horizon_months": 12, "probability": 0.15},
                {
                    "event": "doubles",
                    "horizon_months": 36,
                    "probability": round(max(0.05, p - 0.35), 3),
                },
                {"event": "loses_50pct", "horizon_months": 36, "probability": 0.08},
            ],
            "scenario_payoffs": self._payoffs(p),
            "key_uncertainties": ["simulated output"],
            "falsifiers": [
                {
                    "observable": "revenue growth",
                    "threshold": "< 0% year over year",
                    "check_by": (self._asof(pkt) + dt.timedelta(days=180)).isoformat(),
                }
            ],
            "insufficient_evidence": [] if eid else ["no evidence in packet"],
        }
        if agent == "base_rate":
            out["forecasts"] += [
                {
                    "event": "beats_benchmark",
                    "horizon_months": h,
                    "probability": round(0.5 + (p - 0.5) * h / 12, 3),
                }
                for h in (3, 6)
            ]
            out.update(reference_class="sector and size peers (simulated)", adjustments=[])
        if agent == "news_narrative":
            out["rereview_triggers"] = ["guidance cut", "management change"]
        if agent == "bear":
            out.update(
                premortem="It is 12 months later and the position lost 30%: the base rate won.",
                kill_criteria=[
                    {"observable": "gross margin", "threshold": "falls 3 points year over year"}
                ],
                shared_evidence=[],
            )
        return out

    def _macro_scenario(self, agent: str, pkt: dict[str, Any]) -> dict[str, Any]:
        eid = _first(pkt["items"], "scenarios") or _first(pkt["items"])
        exp = (
            [
                {
                    "scenario_id": "rate_shock",
                    "direction": "negative",
                    "magnitude": "medium",
                    "evidence_ids": [eid],
                }
            ]
            if eid
            else []
        )
        return {
            **self._common(agent, pkt),
            "exposures": exp,
            "insufficient_evidence": [] if eid else ["no macro evidence"],
        }

    def _risk_explainer(self, agent: str, pkt: dict[str, Any]) -> dict[str, Any]:
        item = pkt["items"][0]
        d = item["data"]
        return {
            **self._common(agent, pkt),
            "explanation": f"The risk engine verdict is {d['verdict']} with a maximum of {d['max_size_pct_total']}% [{item['id']}].",
            "verdict": d["verdict"],
            "max_size_pct_total": d["max_size_pct_total"],
            "veto_rule": d.get("veto_rule"),
        }

    def _tax_explainer(self, agent: str, pkt: dict[str, Any]) -> dict[str, Any]:
        item = pkt["items"][0]
        d = item["data"]
        return {
            **self._common(agent, pkt),
            "explanation": f"Buy in the {d['recommended_account']} account [{item['id']}]. Not tax advice; confirm with a CPA.",
            "recommended_account": d["recommended_account"],
            "wash_sale_blocked": d["wash_sale_blocked"],
            "after_tax_hurdle_pct": d["after_tax_hurdle_pct"],
            "cpa_flags": d.get("cpa_flags", []),
        }

    def _behavioral_auditor(self, agent: str, pkt: dict[str, Any]) -> dict[str, Any]:
        return {
            "agent": agent,
            "anon_id": pkt["anon_id"],
            "severity": "none",
            "cooling_off_hours": 0,
            "requires_written_justification": False,
            "flags": [],
            "insufficient_evidence": [],
        }

    def _chair(self, agent: str, pkt: dict[str, Any]) -> dict[str, Any]:
        ctx = pkt["context"]
        outs = ctx.get("committee_outputs", {})
        ps = [
            f["probability"]
            for o in outs.values()
            for f in o.get("forecasts", [])
            if isinstance(f, dict)
            and f.get("event") == "beats_benchmark"
            and f.get("horizon_months") == 12
        ]
        p = round(sum(ps) / len(ps), 3) if ps else 0.45
        det = ctx.get("deterministic", {})
        tag = pkt.get("bucket_tag") or det.get("bucket_tag") or "CORE_PICK"
        rec = "BUY" if p >= 0.58 else "WATCH"
        return {
            **self._common(agent, pkt),
            "bucket_tag": tag,
            "adjustments": [],
            "bear_response": "The base rate is the strongest argument against; the signals move it only modestly.",
            "forecasts": {
                "p_beat_3m": round(0.5 + (p - 0.5) / 3, 3),
                "p_beat_6m": round(0.5 + (p - 0.5) / 2, 3),
                "p_beat_12m": p,
                "p_drawdown_30pct_12m": 0.15,
                "p_doubles_36m": round(max(0.05, p - 0.35), 3),
                "p_loses_50pct_36m": 0.08,
            },
            "scenario_payoffs": self._payoffs(p),
            "recommendation": rec,
            "size_pct_total": float(det.get("size_cap_pct_total") or 0.0) if rec == "BUY" else 0.0,
            "thesis": "Simulated committee output for an offline demo. It is not research.",
            "evidence_ids": [],
            "falsifiers": [
                {
                    "observable": "revenue growth",
                    "threshold": "< 0%",
                    "check_by": (self._asof(pkt) + dt.timedelta(days=180)).isoformat(),
                }
            ],
            "kill_criteria": ["8-K item 4.02 non-reliance"],
            "closing_line": "Research, not advice. The human decides.",
        }
