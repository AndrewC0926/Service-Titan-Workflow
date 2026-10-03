from __future__ import annotations

import datetime as dt
import json
import shutil
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from committee.agents.cli import app
from committee.agents.errors import AgentOutputInvalid
from committee.agents.llm import RecordedClient
from committee.agents.packets import FixtureSource, build_review_packet
from committee.agents.prompts import PromptRegistry
from committee.agents.recall import ProbeAnswer, ProbeItem, run_recall_probe, score_answers
from committee.agents.runtime import AgentRuntime
from committee.agents.steps import run_macro_portfolio
from committee.config.loader import load_config
from committee.journal.store import Journal

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "agents"
MODELS = load_config(ROOT / "config").models


def items(returns: list[float]) -> list[ProbeItem]:
    return [
        ProbeItem(
            security_id=f"S{i}",
            ticker=f"T{i}",
            company=f"Co {i}",
            start=dt.date(2026, 1, 2),
            end=dt.date(2026, 6, 30),
            realized_return=r,
        )
        for i, r in enumerate(returns)
    ]


def answers(dirs: list[str]) -> list[ProbeAnswer]:
    return [ProbeAnswer(id=f"P{i + 1}", direction=d) for i, d in enumerate(dirs)]


def test_score_perfect_recall_is_contamination() -> None:
    its = items([0.1, -0.1] * 6)
    answered, correct, chance, p, flagged, insufficient = score_answers(
        its, answers(["up", "down"] * 6), alpha=0.05, min_n=10
    )
    assert (answered, correct, chance) == (12, 12, 0.5)
    assert p == pytest.approx(0.5**12) and flagged and not insufficient


def test_score_chance_level_not_flagged_and_majority_baseline() -> None:
    its = items([0.1] * 10 + [-0.1] * 2)
    # always "up" scores 10/12 but chance is the majority rate 10/12: not contamination
    *_, flagged, _ = score_answers(its, answers(["up"] * 12), alpha=0.05, min_n=10)
    assert not flagged
    _, correct, *_ = score_answers(its, answers(["unknown"] * 12), alpha=0.05, min_n=10)
    assert correct == 0


def test_small_sample_never_flags() -> None:
    its = items([0.1, -0.1, 0.2])
    *_, flagged, insufficient = score_answers(
        its, answers(["up", "down", "up"]), alpha=0.05, min_n=10
    )
    assert insufficient and not flagged


def test_run_recall_probe_journals() -> None:
    data = json.loads((FIXTURES / "recall_samples.json").read_text())
    its = [ProbeItem.model_validate(i) for i in data["items"]]
    j = Journal(":memory:")
    client = RecordedClient({"recall_probe": [data["recorded_response"]]})
    res = run_recall_probe(client, MODELS, its, run_id="p1", journal=j)
    assert res.correct == 10 and res.n == 12 and res.contaminated
    assert res.model_id == MODELS.tier_for("chair").model
    entry = j.latest("recall_probe")
    assert (
        entry is not None
        and entry.payload["contaminated"] is True
        and entry.payload["cost_usd"] > 0
    )
    req = client.requests[0]
    assert "TST0" in req.packet and req.system_blocks[0].startswith("You are being tested")


def test_recall_probe_parse_error_is_reported() -> None:
    res = run_recall_probe(
        RecordedClient({"recall_probe": ["garbage"]}), MODELS, items([0.1] * 12), run_id="p"
    )
    assert res.parse_error and res.correct == 0 and not res.contaminated


# ------------------------------------------------------------- macro portfolio
def _portfolio_packet() -> Any:
    src = FixtureSource(
        {
            "security": {
                "security_id": "PORTFOLIO",
                "ticker": "PORTFOLIO",
                "name": "Satellite portfolio",
            },
            "evidence": {
                "macro": [{"series_id": "DGS10", "obs_date": "2026-09-24", "value": 4.6}],
                "scenarios": [{"id": "rate_shock", "probability": 0.1}],
            },
        }
    )
    review = build_review_packet(src, "PORTFOLIO", dt.date(2026, 9, 27), "wk-39", bucket_tag=None)
    return review.for_agent("macro_scenario")


def test_macro_portfolio_step() -> None:
    pkt = _portfolio_packet()
    out = {
        "agent": "macro_scenario",
        "anon_id": pkt.anon_id,
        "regime": {
            "growth": "down",
            "inflation": "up",
            "real_rate_level": "high",
            "evidence_ids": ["E1"],
        },
        "scenario_losses": [
            {
                "scenario_id": "rate_shock",
                "portfolio_return_pct": -14.0,
                "exceeds_budget": True,
                "evidence_ids": ["E2"],
            }
        ],
        "risk_budget_modifier": 0.8,
        "drivers": [
            {"driver": "real rates", "explanation": "High real yields.", "evidence_ids": ["E1"]}
        ],
    }
    reg = PromptRegistry.load(ROOT / "prompts")
    rt = AgentRuntime(RecordedClient({"macro_scenario": [out]}), MODELS, reg, run_id="w")
    assert run_macro_portfolio(rt, pkt).output.risk_budget_modifier == 0.8
    bad = {**out, "risk_budget_modifier": 1.2}
    rt = AgentRuntime(RecordedClient({"macro_scenario": [bad, bad]}), MODELS, reg, run_id="w")
    with pytest.raises(AgentOutputInvalid):
        run_macro_portfolio(rt, pkt)


# ------------------------------------------------------------------------ CLI
@pytest.fixture
def root(project: Path) -> Path:
    if not (project / "prompts").exists():
        shutil.copytree(ROOT / "prompts", project / "prompts")
    return project


def test_cli_dry_run(root: Path) -> None:
    r = CliRunner().invoke(
        app,
        ["dry-run", "--fixture", "fx_001", "--fixtures-dir", str(FIXTURES), "--root", str(root)],
    )
    assert r.exit_code == 0, r.output
    for agent in ("base_rate", "fundamentals", "bear", "chair", "chair_gates"):
        assert f"## {agent}" in r.output
    assert "Research, not advice. The human decides." in r.output
    assert "Northwind" not in r.output
    assert not (root / "var").exists() or not any((root / "var").rglob("*.db"))  # in-memory journal


def test_cli_dry_run_missing_fixture(root: Path) -> None:
    r = CliRunner().invoke(
        app,
        ["dry-run", "--fixture", "fx_999", "--fixtures-dir", str(FIXTURES), "--root", str(root)],
    )
    assert r.exit_code != 0


def test_cli_dry_run_reports_needs_attention(root: Path, tmp_path: Path) -> None:
    data = json.loads((FIXTURES / "fx_002.json").read_text())
    data["responses"]["chair"] = ["{}", "{}"]
    (tmp_path / "fx_002.json").write_text(json.dumps(data))
    r = CliRunner().invoke(
        app,
        ["dry-run", "--fixture", "fx_002", "--fixtures-dir", str(tmp_path), "--root", str(root)],
    )
    assert r.exit_code == 1 and "## bear" in r.output


def test_cli_eval_harness(root: Path) -> None:
    r = CliRunner().invoke(
        app, ["eval-harness", "--fixtures-dir", str(FIXTURES), "--root", str(root)]
    )
    assert r.exit_code == 0, r.output
    assert "schema validity     100.0% (1 repaired)" in r.output
    assert "citation coverage   100.0%" in r.output
    assert "contamination flags fx_002/news_narrative" in r.output
    r = CliRunner().invoke(
        app, ["eval-harness", "--json", "--fixtures-dir", str(FIXTURES), "--root", str(root)]
    )
    assert json.loads(r.output)["fixtures"] == 5
    r = CliRunner().invoke(app, ["eval-harness", "--fixtures-dir", str(root), "--root", str(root)])
    assert r.exit_code == 2


def test_cli_recall_probe(root: Path) -> None:
    r = CliRunner().invoke(
        app,
        ["recall-probe", "--samples", str(FIXTURES / "recall_samples.json"), "--root", str(root)],
    )
    assert r.exit_code == 0, r.output
    assert "CONTAMINATION" in r.output and '"correct": 10' in r.output


def test_cli_live_requires_key(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("committee.config.secrets.Secrets.get", lambda self, name: None)
    r = CliRunner().invoke(
        app,
        [
            "dry-run",
            "--live",
            "--fixture",
            "fx_001",
            "--fixtures-dir",
            str(FIXTURES),
            "--root",
            str(root),
        ],
    )
    assert r.exit_code == 2 and "ANTHROPIC_API_KEY is not configured" in r.output
