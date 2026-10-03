"""Recall probe (DESIGN 10, LLM leakage defenses).

Ask the model, with no context, whether a sample of securities went up or down
over past periods inside the evaluation window. Compare with realized returns
(passed in, computed by code from PIT prices). If the correct-direction rate is
significantly above chance (one-sided binomial test), the model likely
memorized outcomes in that window: flag contamination so the cohort is
quarantined. Every probe is journaled as a ``recall_probe`` entry.

Chance is the majority-class rate of the sample (a model that always answers
"up" scores the share of winners), never less than 0.5. "unknown" answers count
as incorrect.
"""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import Sequence

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from scipy.stats import binomtest

from committee.agents.budget import BudgetGuard
from committee.agents.llm import LLMClient, LLMRequest, cost_usd
from committee.agents.runtime import extract_json
from committee.config.schema import ModelsConfig
from committee.journal.store import Journal

PROBE_SYSTEM = (
    "You are being tested for memorized knowledge of past market outcomes. There is no "
    "evidence packet. For each item, say whether the security's total return over the "
    "stated period was positive (up) or negative (down), and your best point estimate of "
    'the return as a decimal. If you do not know, answer "unknown". Return ONLY JSON: '
    '{"answers": [{"id": "P1", "direction": "up|down|unknown", "return_estimate": 0.0}]}'
)


class ProbeItem(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    security_id: str
    ticker: str
    company: str
    start: dt.date
    end: dt.date
    realized_return: float


class ProbeAnswer(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    direction: str = Field(pattern="^(up|down|unknown)$")
    return_estimate: float | None = None


class ProbeAnswers(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    answers: list[ProbeAnswer]


class RecallProbeResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: str
    model_id: str
    n: int
    answered: int
    correct: int
    hit_rate: float
    chance: float
    p_value: float
    alpha: float
    contaminated: bool
    insufficient_sample: bool
    window_start: dt.date | None
    window_end: dt.date | None
    cost_usd: float
    parse_error: str | None = None


def score_answers(
    items: Sequence[ProbeItem], answers: Sequence[ProbeAnswer], *, alpha: float, min_n: int
) -> tuple[int, int, float, float, bool, bool]:
    """(answered, correct, chance, p_value, contaminated, insufficient_sample)."""
    by_id = {a.id: a for a in answers}
    n = len(items)
    correct = answered = 0
    for i, item in enumerate(items, start=1):
        a = by_id.get(f"P{i}")
        if a is None or a.direction == "unknown":
            continue
        answered += 1
        actual = "up" if item.realized_return > 0 else "down"
        correct += a.direction == actual
    ups = sum(1 for it in items if it.realized_return > 0)
    chance = max(0.5, ups / n, (n - ups) / n) if n else 0.5
    chance = min(chance, 0.999)
    p_value = float(binomtest(correct, n, chance, alternative="greater").pvalue) if n else 1.0
    insufficient = n < min_n
    return answered, correct, chance, p_value, (not insufficient and p_value < alpha), insufficient


def run_recall_probe(
    client: LLMClient,
    models: ModelsConfig,
    items: Sequence[ProbeItem],
    *,
    run_id: str,
    journal: Journal | None = None,
    agent_tier_of: str = "chair",
    alpha: float = 0.05,
    min_n: int = 10,
    budget: BudgetGuard | None = None,
) -> RecallProbeResult:
    """Probe the model used by ``agent_tier_of`` (default: the Chair's pinned model)."""
    if budget is not None:
        budget.check()
    tier = models.tier_for(agent_tier_of)
    listing = [
        {
            "id": f"P{i}",
            "ticker": it.ticker,
            "company": it.company,
            "start": it.start.isoformat(),
            "end": it.end.isoformat(),
        }
        for i, it in enumerate(items, start=1)
    ]
    request = LLMRequest(
        agent="recall_probe",
        model=tier.model,
        max_tokens=tier.max_tokens,
        temperature=tier.temperature,
        system_blocks=(PROBE_SYSTEM,),
        packet=json.dumps({"items": listing}, sort_keys=True),
        instruction="Answer every item.",
    )
    resp = client.complete(request)
    answers: list[ProbeAnswer] = []
    parse_error: str | None = None
    try:
        answers = ProbeAnswers.model_validate(extract_json(resp.text)).answers
    except (ValidationError, ValueError) as e:
        parse_error = str(e)[:500]
    answered, correct, chance, p_value, contaminated, insufficient = score_answers(
        items, answers, alpha=alpha, min_n=min_n
    )
    price = models.pricing.get(resp.model_id) or models.pricing[tier.model]
    result = RecallProbeResult(
        run_id=run_id,
        model_id=resp.model_id,
        n=len(items),
        answered=answered,
        correct=correct,
        hit_rate=round(correct / len(items), 4) if items else 0.0,
        chance=round(chance, 4),
        p_value=round(p_value, 6),
        alpha=alpha,
        contaminated=contaminated,
        insufficient_sample=insufficient,
        window_start=min((i.start for i in items), default=None),
        window_end=max((i.end for i in items), default=None),
        cost_usd=round(cost_usd(resp.usage, price), 8),
        parse_error=parse_error,
    )
    if journal is not None:
        journal.append(
            "recall_probe",
            {**result.model_dump(mode="json"), "tokens": resp.usage.as_dict()},
        )
    return result
