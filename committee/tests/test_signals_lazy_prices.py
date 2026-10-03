"""Lazy Prices: TF-IDF cosine vs the prior-year same filing, and the paragraph diff."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from committee.signals.inputs import load_inputs
from committee.signals.lazy_prices import (
    lazy_prices,
    paragraph_diff,
    split_paragraphs,
    tfidf_cosine,
)
from fixtures.signals.builder import ASOF, LakeBuilder

SID = "CIK0000000001"
RISK_A = "Supply chain risk from overseas vendors.\n\nCompetition may reduce margins."
RISK_B = "Cyber attacks may disrupt operations.\n\nLitigation could be costly and lengthy."


def test_tfidf_cosine_bounds() -> None:
    assert tfidf_cosine(RISK_A, RISK_A) == pytest.approx(1.0)
    assert tfidf_cosine("alpha beta gamma", "delta epsilon zeta") == pytest.approx(0.0)
    mid = tfidf_cosine(RISK_A, RISK_A + "\n\nCyber attacks may disrupt operations.")
    assert mid is not None and 0.3 < mid < 1.0
    assert tfidf_cosine("", RISK_A) is None
    assert tfidf_cosine("!!! ???", "... ,,,") is None


def test_paragraph_diff_added_removed_top_n_by_length() -> None:
    prior = "Keep me.\n\nOld short.\n\nOld paragraph that is quite a bit longer."
    cur = "Keep me.\n\nNew A.\n\nNew paragraph that is the longest one of all here.\n\nNew BB."
    d = paragraph_diff(cur, prior, top_n=2)
    assert d.n_added == 3 and d.n_removed == 2
    assert d.added == ("New paragraph that is the longest one of all here.", "New BB.")
    assert d.removed == ("Old paragraph that is quite a bit longer.", "Old short.")


def test_split_paragraphs_collapses_whitespace_and_falls_back_to_lines() -> None:
    assert split_paragraphs("a  b\n\n\n c ") == ["a b", "c"]
    assert split_paragraphs("line one\nline two") == ["line one", "line two"]


def _tenk(
    b: LakeBuilder, acc: str, year: int, risk: str, mdna: str, known: dt.date | None = None
) -> None:
    period = dt.date(year, 12, 31)
    k = known or dt.date(year + 1, 2, 20)
    b.section(SID, acc, "10-K", period, "1A", risk, k)
    b.section(SID, acc, "10-K", period, "7", mdna, k)


def _tenq(b: LakeBuilder, acc: str, period: dt.date, risk: str, known: dt.date) -> None:
    b.section(SID, acc, "10-Q", period, "1A", risk, known)


@pytest.fixture
def b(tmp_path: Path) -> LakeBuilder:
    return LakeBuilder(tmp_path / "lake")


def _run(b: LakeBuilder, asof: dt.date = ASOF):
    return lazy_prices(load_inputs(b.pit(), asof).sections, SID)


def test_10k_vs_prior_10k_identical_is_one(b: LakeBuilder) -> None:
    _tenk(b, "k24", 2024, RISK_A, "Revenue grew.")
    _tenk(b, "k25", 2025, RISK_A, "Revenue grew.")
    r = _run(b, dt.date(2026, 3, 1))
    assert r is not None
    assert (r.form, r.current_accession, r.prior_accession) == ("10-K", "k25", "k24")
    assert r.similarity == pytest.approx(1.0)
    assert set(r.item_similarity) == {"1A", "7"}
    assert r.diffs["1A"].n_added == 0


def test_changed_risk_factors_lower_similarity_and_diff(b: LakeBuilder) -> None:
    _tenk(b, "k24", 2024, RISK_A, "Revenue grew.")
    _tenk(b, "k25", 2025, RISK_B, "Revenue grew.")
    r = _run(b, dt.date(2026, 3, 1))
    assert r is not None and r.similarity is not None
    assert r.item_similarity["1A"] < 0.2
    assert r.similarity == pytest.approx((r.item_similarity["1A"] + 1.0) / 2)
    assert "Cyber attacks may disrupt operations." in r.diffs["1A"].added
    assert "Supply chain risk from overseas vendors." in r.diffs["1A"].removed


def test_10q_compared_with_same_quarter_a_year_earlier(b: LakeBuilder) -> None:
    _tenq(b, "q2_25", dt.date(2025, 6, 30), RISK_A, dt.date(2025, 8, 5))
    _tenq(b, "q1_26", dt.date(2026, 3, 31), RISK_B, dt.date(2026, 5, 5))  # previous 10-Q
    _tenq(b, "q2_26", dt.date(2026, 6, 30), RISK_A, dt.date(2026, 8, 5))
    r = _run(b)
    assert r is not None
    assert (r.form, r.current_accession, r.prior_accession) == ("10-Q", "q2_26", "q2_25")
    assert r.similarity == pytest.approx(1.0)


def test_no_prior_year_filing_gives_none(b: LakeBuilder) -> None:
    _tenk(b, "k25", 2025, RISK_A, "x")
    assert _run(b, dt.date(2026, 3, 1)) is None


def test_filing_known_after_asof_is_ignored(b: LakeBuilder) -> None:
    _tenk(b, "k24", 2024, RISK_A, "Revenue grew.")
    _tenk(b, "k25", 2025, RISK_A, "Revenue grew.")
    _tenk(b, "k26", 2026, RISK_B, "Totally new.", known=dt.date(2027, 2, 20))
    r = _run(b)
    assert r is not None and r.current_accession == "k25"
