"""Digest narration: a cheap-model prose pass over the same grounded data
build_digest already assembles. Two hard constraints, both enforced in code
(not just prompted for), and both tested here:
  1. Any failure -- the call itself, an unparseable response, or a number
     that can't be traced back to the input -- falls back to the existing
     plain-text digest, never to sending nothing.
  2. Every number in the narrated prose must already appear in the
     structured payload it was given -- app.pipeline.notify._numbers_grounded
     is the actual check, not the system prompt alone."""
import pytest

from app.config import Config
from app.pipeline.notify import (
    _numbers_grounded,
    _numbers_in,
    _structured_payload,
    narrate_or_fallback,
)


def _cfg(narrate=True, budget=0.05):
    return Config({"digest": {"narrate": narrate}, "llm": {"digest_narration_budget_usd": budget}})


PLAIN_BODY = "DMG Scout — Wed Aug 19\n\nTHREE TO CALL\n  Nobody contactable.\n"


# ---- _numbers_in / _numbers_grounded ---------------------------------------

def test_numbers_in_extracts_plain_integers():
    assert _numbers_in("call in 45 days") == {"45"}


def test_numbers_in_strips_thousands_commas():
    assert _numbers_in("1,234 sqft") == {"1234"}


def test_numbers_in_keeps_decimals_exact():
    assert _numbers_in("score 0.42") == {"0.42"}


def test_numbers_in_extracts_every_number_in_a_range():
    assert _numbers_in("500-800 tons") == {"500", "800"}


def test_numbers_grounded_true_when_every_output_number_is_in_the_payload():
    payload = {"calls_to_make_today": [{"tons_estimate": "500-800 tons"}]}
    assert _numbers_grounded("Call about the 500-800 ton project.", payload) is True


def test_numbers_grounded_false_on_a_fabricated_number():
    payload = {"calls_to_make_today": [{"tons_estimate": "500-800 tons"}]}
    assert _numbers_grounded("Call about the roughly 650 ton project.", payload) is False


def test_numbers_grounded_false_when_a_range_is_collapsed_to_one_figure():
    """The exact failure mode the hard constraint calls out: a band
    smoothed into a single number is not grounded, even though both
    endpoints individually are."""
    payload = {"calls_to_make_today": [{"tons_estimate": "500-800 tons"}]}
    assert _numbers_grounded("A 650 ton project worth a call.", payload) is False


def test_numbers_grounded_true_with_no_numbers_at_all():
    assert _numbers_grounded("Nothing changed today.", {"one_thing_worth_knowing": None}) is True


# ---- _structured_payload ----------------------------------------------------

def test_structured_payload_reuses_render_calls_tons_formatting():
    from types import SimpleNamespace
    project = SimpleNamespace(name="Meridian DC", county="San Bernardino",
                              tons_estimate_low=500, tons_estimate_high=800)
    calls = [{"project": project, "contact": {"name": "Jane Doe", "phone": "555-1000"},
             "reason": "pre-BOD, ~10d to est. bid"}]
    payload = _structured_payload(calls, ["NEW: Meridian DC"], [], "3 item(s) changed today")
    assert payload["calls_to_make_today"][0]["tons_estimate"] == "500-800 tons"
    assert payload["calls_to_make_today"][0]["project_name"] == "Meridian DC"
    assert payload["changed_since_yesterday"] == ["NEW: Meridian DC"]


def test_structured_payload_says_so_plainly_when_empty():
    payload = _structured_payload([], [], [], None)
    assert payload["changed_since_yesterday"] == "nothing changed since yesterday"
    assert payload["overdue_and_due_next_actions"] == "nothing due or overdue"


# ---- narrate_or_fallback: the orchestration + fallback contract -----------

def test_narrate_or_fallback_disabled_returns_plain_body_unchanged():
    body, stats = narrate_or_fallback(_cfg(narrate=False), [], [], [], None, PLAIN_BODY)
    assert body == PLAIN_BODY
    assert stats == {"narrated": False, "reason": "disabled", "cost_usd": 0.0}


def test_narrate_or_fallback_llm_unavailable_falls_back(monkeypatch):
    monkeypatch.setenv("SCOUT_LLM_DISABLED", "")  # ensure it's not that path
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    body, stats = narrate_or_fallback(_cfg(), [], [], [], None, PLAIN_BODY)
    assert body == PLAIN_BODY
    assert stats["narrated"] is False
    assert stats["cost_usd"] == 0.0


def test_narrate_or_fallback_call_raises_falls_back(monkeypatch):
    def _boom(payload):
        raise RuntimeError("model returned no narrated_digest tool call")
    monkeypatch.setattr("app.llm.narrate_digest", _boom)
    body, stats = narrate_or_fallback(_cfg(), [], [], [], None, PLAIN_BODY)
    assert body == PLAIN_BODY
    assert stats["narrated"] is False
    assert "model returned no" in stats["reason"]


def test_narrate_or_fallback_empty_body_falls_back(monkeypatch):
    monkeypatch.setattr("app.llm.narrate_digest", lambda payload: {"body": ""})
    monkeypatch.setattr("app.llm.last_call_cost_usd", lambda: 0.002)
    body, stats = narrate_or_fallback(_cfg(), [], [], [], None, PLAIN_BODY)
    assert body == PLAIN_BODY
    assert stats["narrated"] is False
    assert stats["reason"] == "empty body"
    assert stats["cost_usd"] == 0.002  # the call happened and cost money even though it failed


def test_narrate_or_fallback_ungrounded_number_falls_back(monkeypatch):
    monkeypatch.setattr("app.llm.narrate_digest",
                        lambda payload: {"body": "Nothing changed, but watch that 650 ton deal."})
    monkeypatch.setattr("app.llm.last_call_cost_usd", lambda: 0.002)
    body, stats = narrate_or_fallback(_cfg(), [], [], [], None, PLAIN_BODY)
    assert body == PLAIN_BODY
    assert stats["narrated"] is False
    assert stats["reason"] == "ungrounded number"


def test_narrate_or_fallback_success_returns_narrated_prose(monkeypatch):
    from types import SimpleNamespace
    project = SimpleNamespace(name="Meridian DC", county="San Bernardino",
                              tons_estimate_low=500, tons_estimate_high=800)
    calls = [{"project": project, "contact": {"name": "Jane Doe", "phone": "555-1000"},
             "reason": "pre-BOD"}]
    prose = "Call Jane Doe at 555-1000 about Meridian DC, a 500-800 ton project in San Bernardino."
    monkeypatch.setattr("app.llm.narrate_digest", lambda payload: {"body": prose})
    monkeypatch.setattr("app.llm.last_call_cost_usd", lambda: 0.0031)

    body, stats = narrate_or_fallback(_cfg(), calls, [], [], None, PLAIN_BODY)
    assert body == prose + "\n"
    assert stats == {"narrated": True, "cost_usd": 0.0031}


def test_narrate_or_fallback_never_raises_even_on_an_unexpected_exception(monkeypatch):
    """The hard requirement: narration failing must never become the digest
    failing to send. A KeyError deep inside a malformed response must be
    caught the same as any other failure."""
    def _boom(payload):
        raise KeyError("usage")
    monkeypatch.setattr("app.llm.narrate_digest", _boom)
    body, stats = narrate_or_fallback(_cfg(), [], [], [], None, PLAIN_BODY)
    assert body == PLAIN_BODY
    assert stats["narrated"] is False
