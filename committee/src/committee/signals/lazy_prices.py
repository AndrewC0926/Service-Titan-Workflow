"""Lazy Prices (Cohen, Malloy and Nguyen 2020): year-over-year filing text similarity.

For a security's most recent 10-K or 10-Q known as of ``asof``, find the same form a
year earlier (10-K vs prior 10-K; 10-Q vs the 10-Q whose period ended about 365 days
earlier, i.e. the same fiscal quarter). For each section present in both (Item 1A risk
factors and MD&A: Item 7 in a 10-K; Item 2 or 7 in a 10-Q as the ingester labels it),
similarity = cosine of TF-IDF vectors fitted on the two texts. The filing score is the
mean over compared sections (0 = disjoint, 1 = identical).

The screen uses it negative-only: low similarity (big changes) is bear evidence, high
similarity contributes 0. A structured paragraph diff is kept for the Filings agent.
Filing text is untrusted data; it is only tokenized here, never interpreted.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import pandas as pd
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity

from committee.signals.common import date_col

FORMS = ("10-K", "10-Q")
ITEMS_BY_FORM: dict[str, tuple[str, ...]] = {"10-K": ("1A", "7"), "10-Q": ("1A", "2", "7")}
PRIOR_YEAR_DAYS = 365
PRIOR_TOLERANCE_DAYS = 45
DIFF_TOP_N = 5
_WS = re.compile(r"\s+")
_PARA = re.compile(r"\n\s*\n|\r\n\s*\r\n")


@dataclass(frozen=True)
class ParagraphDiff:
    """Paragraphs added or removed vs the prior-year filing (top N by length)."""

    added: tuple[str, ...]
    removed: tuple[str, ...]
    n_added: int
    n_removed: int


@dataclass(frozen=True)
class LazyPricesResult:
    security_id: str
    form: str
    current_accession: str
    prior_accession: str
    current_period: str | None
    prior_period: str | None
    similarity: float | None
    item_similarity: dict[str, float] = field(default_factory=dict)
    diffs: dict[str, ParagraphDiff] = field(default_factory=dict)


def tfidf_cosine(a: str, b: str) -> float | None:
    """Cosine similarity of TF-IDF vectors fitted on the pair. None if either is empty."""
    if not a.strip() or not b.strip():
        return None
    try:
        m = TfidfVectorizer(lowercase=True).fit_transform([a, b])
    except ValueError:  # empty vocabulary (no word tokens)
        return None
    return float(min(1.0, max(0.0, cosine_similarity(m[0], m[1])[0, 0])))


def split_paragraphs(text: str) -> list[str]:
    """Blank-line separated paragraphs with whitespace collapsed; empties dropped.
    Text without blank lines falls back to one paragraph per line."""
    parts = _PARA.split(text) if _PARA.search(text) else text.splitlines()
    return [p for p in (_WS.sub(" ", x).strip() for x in parts) if p]


def paragraph_diff(current: str, prior: str, top_n: int = DIFF_TOP_N) -> ParagraphDiff:
    cur, old = split_paragraphs(current), split_paragraphs(prior)
    cur_set, old_set = set(cur), set(old)
    added = [p for p in dict.fromkeys(cur) if p not in old_set]
    removed = [p for p in dict.fromkeys(old) if p not in cur_set]

    def top(ps: list[str]) -> tuple[str, ...]:
        return tuple(sorted(ps, key=lambda p: (-len(p), p))[:top_n])

    return ParagraphDiff(top(added), top(removed), len(added), len(removed))


def _filings(sections: pd.DataFrame) -> pd.DataFrame:
    df = sections[sections["form"].isin(FORMS)].copy()
    if df.empty:
        return df
    df["period_d"] = date_col(df, "period")
    df["known_time"] = pd.to_datetime(df["known_time"], utc=True)
    return (
        df.groupby("accession", as_index=False)
        .agg(form=("form", "first"), period_d=("period_d", "first"), known=("known_time", "max"))
        .sort_values(["known", "accession"])
        .reset_index(drop=True)
    )


def find_prior(filings: pd.DataFrame, current: pd.Series) -> pd.Series | None:
    """The same-form filing whose period ended closest to one year before ``current``'s
    (within the tolerance) and that was known before it."""
    same = filings[
        (filings["form"] == current["form"])
        & (filings["accession"] != current["accession"])
        & (filings["known"] < current["known"])
    ]
    if same.empty or pd.isna(current["period_d"]):
        return None
    target = current["period_d"] - pd.Timedelta(days=PRIOR_YEAR_DAYS)
    gap = (same["period_d"] - target).abs()
    ok = gap[gap <= pd.Timedelta(days=PRIOR_TOLERANCE_DAYS)]
    if ok.empty:
        return None
    row: pd.Series = same.loc[ok.sort_values(kind="stable").index[0]]
    return row


def _period(v: object) -> str | None:
    if v is None or v is pd.NaT or (isinstance(v, float) and v != v):
        return None
    return str(pd.Timestamp(str(v)).date())


def lazy_prices(
    sections: pd.DataFrame, security_id: str, top_n: int = DIFF_TOP_N
) -> LazyPricesResult | None:
    """Score the latest 10-K/10-Q of one security; None if no comparable prior filing."""
    own = sections[sections["security_id"] == security_id]
    filings = _filings(own)
    if filings.empty:
        return None
    current = filings.iloc[-1]
    prior = find_prior(filings, current)
    if prior is None:
        return None
    text = own.set_index(["accession", "item"])["text"].astype(str)
    sims: dict[str, float] = {}
    diffs: dict[str, ParagraphDiff] = {}
    for item in ITEMS_BY_FORM[str(current["form"])]:
        a, b = (current["accession"], item), (prior["accession"], item)
        if a not in text.index or b not in text.index:
            continue
        sim = tfidf_cosine(str(text.loc[a]), str(text.loc[b]))
        if sim is None:
            continue
        sims[item] = sim
        diffs[item] = paragraph_diff(str(text.loc[a]), str(text.loc[b]), top_n)
    return LazyPricesResult(
        security_id=security_id,
        form=str(current["form"]),
        current_accession=str(current["accession"]),
        prior_accession=str(prior["accession"]),
        current_period=_period(current["period_d"]),
        prior_period=_period(prior["period_d"]),
        similarity=sum(sims.values()) / len(sims) if sims else None,
        item_similarity=sims,
        diffs=diffs,
    )
