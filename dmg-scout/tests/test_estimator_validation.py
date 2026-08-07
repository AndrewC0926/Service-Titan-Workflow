"""Invariant 8: no estimate is trusted that has not been validated against actuals.

The estimator had never been checked against money actually spent. When it finally
was, on 2026-08-06 against 673 CEQAnet documents, it came in 10.9% under on triage
input and 9.2% under on extract input — both in the direction that gets a budget
approved and then overrun.

Two defects behind it:
  - the triage pass rate was a config constant (0.39) where the corpus said 0.512
  - chars_per_token was 3.7 where both stages independently imply ~3.1
"""
from datetime import datetime, timedelta

import pytest
from sqlmodel import select

from app.models import RawDocument, TokenSpend, TriageResult
from app.pipeline.backfill import (
    ESTIMATE_TOLERANCE, MIN_PASS_RATE_SAMPLE, estimate_cost, measured_pass_rate,
    validate_estimate,
)


def _doc(i: int, result: TriageResult, chars: int = 6000) -> RawDocument:
    return RawDocument(source="ceqanet", source_uid=f"u{i}", url=f"https://x/{i}",
                       title="NOP", raw_text="word " * (chars // 5),
                       content_hash=f"h{i}", triage_result=result)


def _corpus(session, n_relevant: int, n_irrelevant: int) -> None:
    for i in range(n_relevant):
        session.add(_doc(i, TriageResult.relevant))
    for i in range(n_irrelevant):
        session.add(_doc(1000 + i, TriageResult.irrelevant))
    session.commit()


# --- the pass rate is measured, not assumed ---------------------------------

def test_pass_rate_is_measured_from_the_corpus(db_session, cfg):
    _corpus(db_session, n_relevant=60, n_irrelevant=40)
    rate, basis = measured_pass_rate(db_session, cfg)
    assert rate == pytest.approx(0.6)
    assert "measured over 100 triaged documents" in basis
    assert rate != cfg.get("llm.assumed_triage_pass_rate")


def test_pass_rate_falls_back_below_the_sample_floor(db_session, cfg):
    """A rate off five documents is noise, and must not be dressed up as measured."""
    _corpus(db_session, n_relevant=3, n_irrelevant=2)
    rate, basis = measured_pass_rate(db_session, cfg)
    assert rate == cfg.get("llm.assumed_triage_pass_rate")
    assert "config assumption" in basis
    assert str(MIN_PASS_RATE_SAMPLE) in basis


def test_pending_documents_do_not_count_toward_the_pass_rate(db_session, cfg):
    _corpus(db_session, n_relevant=60, n_irrelevant=40)
    for i in range(500):
        db_session.add(_doc(5000 + i, TriageResult.pending))
    db_session.commit()
    rate, basis = measured_pass_rate(db_session, cfg)
    assert rate == pytest.approx(0.6)          # not 60/600
    assert "over 100 triaged documents" in basis


def test_estimate_states_the_basis_of_its_pass_rate(db_session, cfg):
    """"measured on 861 real documents" and "a number in a YAML file" must not
    look alike on the same report."""
    _corpus(db_session, n_relevant=60, n_irrelevant=40)
    db_session.add(_doc(9999, TriageResult.pending))
    db_session.commit()
    est = estimate_cost(db_session, cfg)
    assert est["triage_pass_rate"] == pytest.approx(0.6)
    assert "measured over" in est["triage_pass_rate_basis"]
    assert "assumed" not in est


# --- validation against actuals ---------------------------------------------

WINDOW_START = datetime(2026, 8, 6, 0, 0)
WINDOW_END = datetime(2026, 8, 6, 3, 0)


def _spend(session, stage: str, calls: int, tokens_in: int, tokens_out: int,
           when: datetime = WINDOW_START + timedelta(minutes=30)) -> None:
    for _ in range(calls):
        session.add(TokenSpend(stage=stage, model="m", ts=when,
                               day=f"{when:%Y-%m-%d}",
                               input_tokens=tokens_in // calls,
                               output_tokens=tokens_out // calls, cost_usd=0.01))
    session.commit()


def test_validation_flags_an_estimate_outside_the_gate(db_session, cfg):
    _corpus(db_session, n_relevant=0, n_irrelevant=100)
    # actual input tokens per call double what the constants predict
    est = estimate_cost(db_session, cfg)  # no pending docs -> paper estimate
    del est
    _spend(db_session, "triage", calls=100, tokens_in=100 * 20_000, tokens_out=100 * 95)
    report = validate_estimate(db_session, cfg, WINDOW_START, WINDOW_END,
                               sources=["ceqanet"])
    tri = report["stages"]["triage"]
    assert tri["tokens_in_per_call"]["within_tolerance"] is False
    assert tri["tokens_in_per_call"]["error_pct"] < -50
    assert report["gate_passed"] is False


def test_validation_passes_when_the_constants_match_reality(db_session, cfg):
    _corpus(db_session, n_relevant=0, n_irrelevant=100)
    cpt = cfg.get("llm.chars_per_token")
    ovh = cfg.get("llm.triage_prompt_overhead_tokens")
    out = cfg.get("llm.triage_output_tokens")
    from app.sections import select_triage_text
    docs = db_session.exec(select(RawDocument)).all()
    per_call = int(sum(select_triage_text(d.raw_text, cfg).body_chars / cpt + ovh
                       for d in docs) / len(docs))
    _spend(db_session, "triage", calls=100, tokens_in=100 * per_call,
           tokens_out=100 * out)
    report = validate_estimate(db_session, cfg, WINDOW_START, WINDOW_END,
                               sources=["ceqanet"])
    tri = report["stages"]["triage"]
    assert tri["tokens_in_per_call"]["within_tolerance"] is True
    assert abs(tri["tokens_in_per_call"]["error_pct"]) <= ESTIMATE_TOLERANCE * 100
    assert report["gate_passed"] is True


def test_validation_surfaces_a_corpus_that_is_not_the_window(db_session, cfg):
    """A purge and re-backfill leaves spend behind for documents that are gone.

    Averaging across that gap would charge the difference to the estimator's
    constants instead of to the mismatch, so the mismatch has to be visible.
    """
    _corpus(db_session, n_relevant=0, n_irrelevant=100)
    _spend(db_session, "triage", calls=50, tokens_in=50 * 3000, tokens_out=50 * 95)
    report = validate_estimate(db_session, cfg, WINDOW_START, WINDOW_END,
                               sources=["ceqanet"])
    tri = report["stages"]["triage"]
    assert tri["predicted_docs"] == 100 and tri["actual_calls"] == 50
    assert tri["corpus_matches_calls"] is False


def test_validation_reports_no_actual_rather_than_inventing_one(db_session, cfg):
    _corpus(db_session, n_relevant=10, n_irrelevant=10)
    report = validate_estimate(db_session, cfg, WINDOW_START, WINDOW_END)
    assert report["stages"]["triage"] == {"status": "no actual spend in window"}
    assert report["gate_passed"] is False   # nothing checked is not a pass
