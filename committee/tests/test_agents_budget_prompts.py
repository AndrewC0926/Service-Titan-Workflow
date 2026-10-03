from __future__ import annotations

import datetime as dt
import re
from pathlib import Path

import pytest

from committee.agents.budget import BudgetGuard
from committee.agents.errors import BudgetExceeded
from committee.agents.prompts import PromptRegistry
from committee.agents.schemas import AGENT_SCHEMAS, output_spec
from committee.config.schema import AGENT_NAMES, Budget
from committee.journal.store import Journal

ROOT = Path(__file__).resolve().parents[1]
BUDGET = Budget(
    monthly_usd=100.0,
    downgrade_at_fraction=0.8,
    reviews_per_week_normal=6,
    reviews_per_week_downgraded=3,
)


class Clock:
    def __init__(self, t: dt.datetime) -> None:
        self.t = t

    def __call__(self) -> dt.datetime:
        return self.t


def test_budget_guard_counts_this_month_only() -> None:
    clock = Clock(dt.datetime(2026, 8, 30, tzinfo=dt.UTC))
    j = Journal(":memory:", clock=clock)
    j.append("agent_output", {"cost_usd": 500.0})  # last month
    clock.t = dt.datetime(2026, 9, 2, tzinfo=dt.UTC)
    g = BudgetGuard(j, BUDGET, clock=clock)
    assert g.spent_this_month() == 0.0
    assert g.reviews_per_week_allowed() == 6
    j.append("agent_output", {"cost_usd": 50.0})
    j.append("recall_probe", {"cost_usd": 29.0})
    j.append("note", {"cost_usd": 1000.0})  # not a cost entry
    assert g.spent_this_month() == pytest.approx(79.0)
    assert not g.downgraded() and g.check() == pytest.approx(0.79)
    j.append("agent_output", {"cost_usd": 1.0})
    assert g.downgraded() and g.reviews_per_week_allowed() == 3
    j.append("agent_output", {"cost_usd": 20.0})
    with pytest.raises(BudgetExceeded):
        g.check()
    assert g.reviews_per_week_allowed() == 0


# ------------------------------------------------------------------ prompts
def _design_section7() -> str:
    text = (ROOT / "docs" / "DESIGN.md").read_text(encoding="utf-8")
    return text[text.index("## 7. Agent roster") : text.index("## 8. Orchestration")]


def test_prompt_files_are_verbatim_from_design() -> None:
    section = _design_section7()
    blocks = re.findall(r"```(?:json)?\n(.*?)```", section, flags=re.DOTALL)
    reg = PromptRegistry.load(ROOT / "prompts")
    assert reg.preamble in blocks
    assert reg.schema in blocks
    for agent in AGENT_NAMES:
        assert reg.agents[agent].text in blocks, agent
    assert len(blocks) == 13  # preamble + schema + 11 agents


def test_preamble_composition_and_hash() -> None:
    reg = PromptRegistry.load(ROOT / "prompts")
    spec = output_spec("chair", AGENT_SCHEMAS["chair"], reg.schema)
    c = reg.compose(
        "chair", spec, asof="2026-09-27", anon_id="SEC-AB12", benchmark="SPY", cost_bps=100
    )
    pre = c.system_blocks[0]
    assert (
        "As-of date: 2026-09-27." in pre
        and "SEC-AB12" in pre
        and "after an\nassumed 100 bps" in pre
    )
    for placeholder in ("{asof}", "{anon_id}", "{benchmark}", "{cost_bps}"):
        assert placeholder not in pre
    assert reg.agents["chair"].text in c.system_blocks[1] and "OUTPUT FORMAT" in c.system_blocks[1]
    # the hash does not depend on per-review values, but does on content
    c2 = reg.compose(
        "chair", spec, asof="2026-10-04", anon_id="SEC-0000", benchmark="SPY", cost_bps=100
    )
    assert c.prompt_hash == c2.prompt_hash
    assert reg.prompt_hash("chair", spec + "x") != c.prompt_hash
    assert reg.prompt_hash("bear", spec) != c.prompt_hash


def test_analyst_spec_contains_shared_schema() -> None:
    reg = PromptRegistry.load(ROOT / "prompts")
    spec = output_spec("bear", AGENT_SCHEMAS["bear"], reg.schema)
    assert reg.schema.strip() in spec and "premortem" in spec and "shared_evidence" in spec


def test_registry_rejects_missing(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        PromptRegistry.load(tmp_path / "nope")
    reg = PromptRegistry.load(ROOT / "prompts")
    with pytest.raises(ValueError, match="placeholder"):
        PromptRegistry("no placeholders", reg.schema, reg.agents)
    with pytest.raises(ValueError, match="missing"):
        PromptRegistry(reg.preamble, reg.schema, {})


def test_register_trials_is_idempotent() -> None:
    reg = PromptRegistry.load(ROOT / "prompts")
    specs = {a: output_spec(a, s, reg.schema) for a, s in AGENT_SCHEMAS.items()}
    j = Journal(":memory:")
    assert len(reg.register_trials(j, specs)) == 11
    assert reg.register_trials(j, specs) == []
    specs["chair"] += "\nchanged"
    assert reg.register_trials(j, specs) == ["chair"]
    assert sum(1 for _ in j.entries("trial")) == 12
