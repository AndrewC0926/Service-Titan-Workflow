"""Live extraction regression against the hand-verified golden set.

Skipped without ANTHROPIC_API_KEY or a verified golden file — but once those
exist, this is the gate that stops a prompt change from silently degrading
extraction: zero fabrications allowed, and per-field recall must not drop more
than 5 points below the recorded baseline (evals/baseline.json, written the
first time this suite passes).
"""
import json
import os

import pytest

from app.golden import DOCS_DIR, EVALS_DIR, load_golden, score

BASELINE_PATH = EVALS_DIR / "baseline.json"
KEY_FIELDS = ["mw_it", "generator_count", "generator_hp_each", "building_sqft",
              "stage", "county", "named_firms", "named_people"]

golden = [e for e in load_golden() if e.get("verified")]

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        not os.environ.get("ANTHROPIC_API_KEY") or not golden,
        reason="needs ANTHROPIC_API_KEY and a verified evals/golden.jsonl",
    ),
]


@pytest.fixture(scope="module")
def rerun_entries():
    """Re-extract every golden document with the current prompt/model."""
    from app.llm import extract
    out = []
    for e in golden:
        text = (DOCS_DIR / e["text_file"]).read_text()
        fresh = extract(text, title=e["title"], source=e["source"], url=e["url"])
        fresh["stage"] = fresh.get("stage") or "unknown"
        out.append({**e, "rerun": fresh})
    return out


def test_zero_fabrications(rerun_entries):
    result = score(rerun_entries, model_key="rerun")
    assert not result["fabrications"], (
        "FABRICATED VALUES — fix the extraction prompt before shipping:\n"
        + "\n".join(f"{f}: {s.examples}" for f, s in result["fabrications"].items())
    )


def test_recall_does_not_regress(rerun_entries):
    result = score(rerun_entries, model_key="rerun")
    current = {
        f: result["fields"][f].recall
        for f in KEY_FIELDS if result["fields"][f].recall is not None
    }
    if not BASELINE_PATH.exists():
        BASELINE_PATH.write_text(json.dumps(current, indent=2))
        pytest.skip("baseline recorded for the first time; future runs enforce it")
    baseline = json.loads(BASELINE_PATH.read_text())
    regressions = {
        f: (baseline[f], current.get(f, 0.0))
        for f in baseline if current.get(f, 0.0) < baseline[f] - 0.05
    }
    assert not regressions, f"recall regressed vs baseline: {regressions}"
