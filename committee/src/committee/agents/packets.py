"""Evidence packet builder (DESIGN 7, 10, 11).

Code, not the model, decides what each agent sees. The builder:

1. pulls as-of data for the review through a ``PacketSource`` (PIT lake or fixtures);
2. assigns evidence ids E1..En once per review, so every agent cites the same ids;
3. replaces the ticker and company names with an anonymous id and scrubs them
   from all text;
4. wraps every news and filing text field in <untrusted_content> delimiters,
   escaping any delimiter inside the text (prompt-injection defense);
5. shifts dates to relative terms ("T-34d") relative to the as-of date;
6. drops account numbers, balances and dollar holdings (position weights only).

Each agent then gets a filtered *view* (``ReviewPacket.for_agent``); the view's
hash is SHA-256 of its canonical JSON and is journaled with every call.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
from collections.abc import Callable, Iterable, Mapping
from typing import Any, Protocol

import pandas as pd
from pydantic import BaseModel, ConfigDict, Field

from committee.agents.errors import PacketLeak
from committee.journal.canonical import canonical_json

# ------------------------------------------------------------------ vocabulary
SOURCE_KINDS: tuple[str, ...] = (
    "signals",
    "base_rate_table",
    "fundamentals",
    "call_tone",
    "valuation",
    "insider_txns",
    "filing_diffs",
    "filings_8k",
    "news",
    "macro",
    "risk_indexes",
    "scenarios",
    "exposures",
    "prices",
    "proposal",
    "decision_history",
    "risk_engine",
    "tax_engine",
)

UNTRUSTED_KINDS = frozenset({"news", "filing_diffs", "filings_8k"})
UNTRUSTED_FIELDS = frozenset({"headline", "summary", "text", "added", "removed", "excerpt", "body"})

_ANALYST_KINDS = (
    "signals",
    "base_rate_table",
    "fundamentals",
    "call_tone",
    "valuation",
    "insider_txns",
    "filing_diffs",
    "filings_8k",
    "news",
    "macro",
    "scenarios",
    "exposures",
)

AGENT_KINDS: dict[str, tuple[str, ...]] = {
    "base_rate": ("signals", "base_rate_table"),
    "fundamentals": ("signals", "fundamentals", "call_tone"),
    "valuation": ("signals", "valuation", "fundamentals", "macro"),
    "filings_insiders": ("insider_txns", "filing_diffs", "filings_8k"),
    "news_narrative": ("news",),
    "macro_scenario": ("macro", "risk_indexes", "scenarios", "exposures"),
    "bear": _ANALYST_KINDS,
    "risk_explainer": ("risk_engine",),
    "tax_explainer": ("tax_engine",),
    "behavioral_auditor": ("prices", "proposal", "decision_history"),
    "chair": SOURCE_KINDS,
}

# Keys that must never reach a model (DESIGN 11, least data).
SENSITIVE_KEYS = frozenset(
    {
        "account",
        "account_id",
        "account_number",
        "account_no",
        "balance",
        "balances",
        "cash_balance",
        "market_value",
        "holding_value",
        "position_value",
        "dollar_amount",
        "notional",
        "notional_usd",
        "cost_basis",
        "basis_usd",
        "qty",
        "quantity",
        "shares_held",
        "ssn",
        "tax_id",
    }
)

_DATE_KEYS = frozenset(
    {"date", "period_end", "published_at", "txn_date", "filed_at", "accepted_at", "obs_date"}
)
_ISO_DATE = re.compile(r"\b(\d{4}-\d{2}-\d{2})(?:[T ][0-9:.+\-Z]+)?\b")
_OPEN = re.compile(r"<\s*untrusted_content", re.IGNORECASE)
_CLOSE = re.compile(r"<\s*/\s*untrusted_content", re.IGNORECASE)
_LEGAL_SUFFIX = re.compile(
    r"[,\s]+(inc\.?|incorporated|corp\.?|corporation|co\.?|company|ltd\.?|limited|plc|"
    r"holdings?|n\.v\.|s\.a\.|ag|llc|l\.p\.)$",
    re.IGNORECASE,
)


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SecurityInfo(Strict):
    security_id: str
    ticker: str
    name: str
    sector: str | None = None
    size_bucket: str | None = None
    aliases: list[str] = Field(default_factory=list)


class EvidenceItem(Strict):
    id: str
    kind: str
    label: str
    data: dict[str, Any]


class EvidencePacket(Strict):
    """What one agent sees. Its hash is journaled with the call."""

    review_id: str
    agent: str
    anon_id: str
    asof: str
    bucket_tag: str | None
    context: dict[str, Any]
    items: list[EvidenceItem]

    @property
    def evidence_ids(self) -> frozenset[str]:
        return frozenset(i.id for i in self.items)

    @property
    def hash(self) -> str:
        return packet_hash(self.model_dump(mode="json"))

    def render(self) -> str:
        """Deterministic text sent to the model (stable bytes keep the prompt cache warm)."""
        body = json.dumps(
            self.model_dump(mode="json"), indent=1, sort_keys=True, ensure_ascii=False
        )
        return "EVIDENCE PACKET (JSON). Cite items by their id, e.g. [E1].\n" + body


class ReviewPacket(Strict):
    """All evidence for one review, ids assigned once."""

    review_id: str
    anon_id: str
    asof: str
    bucket_tag: str | None
    context: dict[str, Any]
    items: list[EvidenceItem]

    def for_agent(
        self, agent: str, extra_context: Mapping[str, Any] | None = None
    ) -> EvidencePacket:
        kinds = AGENT_KINDS[agent]
        ctx = dict(self.context)
        if extra_context:
            ctx.update(extra_context)
        pkt = EvidencePacket(
            review_id=self.review_id,
            agent=agent,
            anon_id=self.anon_id,
            asof=self.asof,
            bucket_tag=self.bucket_tag,
            context=ctx,
            items=[i for i in self.items if i.kind in kinds],
        )
        assert_safe(pkt)
        return pkt

    @property
    def evidence_ids(self) -> frozenset[str]:
        return frozenset(i.id for i in self.items)


def packet_hash(obj: Any) -> str:
    return hashlib.sha256(canonical_json(obj).encode("utf-8")).hexdigest()


# --------------------------------------------------------------------- source
class PacketSource(Protocol):
    """Where as-of data comes from. Implementations must return only rows knowable at asof."""

    def security(self, security_id: str, asof: dt.date) -> SecurityInfo: ...

    def fetch(self, kind: str, security_id: str, asof: dt.date) -> list[dict[str, Any]]: ...


class FixtureSource:
    """Packet source backed by a fixture dict: {"security": {...}, "asof": ..., kinds...}."""

    def __init__(self, data: Mapping[str, Any]) -> None:
        self.data = data

    def security(self, security_id: str, asof: dt.date) -> SecurityInfo:
        return SecurityInfo.model_validate(self.data["security"])

    def fetch(self, kind: str, security_id: str, asof: dt.date) -> list[dict[str, Any]]:
        rows = self.data.get("evidence", {}).get(kind, [])
        return [dict(r) for r in rows]


# -------------------------------------------------------------- anonymization
def anon_id_for(security_id: str, salt: str) -> str:
    """Deterministic per (security, review salt); different reviews get different ids."""
    digest = hashlib.sha256(f"{security_id}:{salt}".encode()).hexdigest()
    return f"SEC-{digest[:4].upper()}"


class Scrubber:
    """Replaces ticker and company-name mentions with the anon id."""

    def __init__(self, info: SecurityInfo, anon_id: str) -> None:
        self.anon_id = anon_id
        names: set[str] = set()
        for n in [info.name, *info.aliases]:
            n = n.strip()
            if not n:
                continue
            names.add(n)
            core = _LEGAL_SUFFIX.sub("", n).strip(" ,")
            if len(core) >= 4:
                names.add(core)
        ordered = sorted(names, key=len, reverse=True)
        self._name_re = (
            re.compile(
                r"(?<![A-Za-z0-9])("
                + "|".join(re.escape(n) for n in ordered)
                + r")(?![A-Za-z0-9])",
                re.IGNORECASE,
            )
            if ordered
            else None
        )
        t = re.escape(info.ticker.strip())
        if len(info.ticker.strip()) >= 3:
            self._ticker_re = re.compile(rf"(?<![A-Za-z0-9])\$?{t}(?![A-Za-z0-9])")
        else:  # short tickers ("ON", "A") only in unambiguous forms: $X, (X), :X)
            self._ticker_re = re.compile(
                rf"\${t}(?![A-Za-z0-9])|(?<=\(){t}(?=\))|(?<=: ){t}(?=\))|(?<=:){t}(?=\))"
            )

    def __call__(self, text: str) -> str:
        if self._name_re is not None:
            text = self._name_re.sub(self.anon_id, text)
        return self._ticker_re.sub(self.anon_id, text)


# ------------------------------------------------------------------- helpers
def _as_date(v: Any) -> dt.date | None:
    if isinstance(v, dt.datetime):
        return v.astimezone(dt.UTC).date() if v.tzinfo else v.date()
    if isinstance(v, dt.date):
        return v
    if isinstance(v, pd.Timestamp):
        return v.date()
    if isinstance(v, str):
        m = _ISO_DATE.fullmatch(v.strip())
        if m:
            try:
                return dt.date.fromisoformat(m.group(1))
            except ValueError:
                return None
    return None


def relative_date(d: dt.date, asof: dt.date) -> str:
    n = (d - asof).days
    return "T" if n == 0 else (f"T{n:+d}d")


def _shift_text_dates(text: str, asof: dt.date) -> str:
    def rep(m: re.Match[str]) -> str:
        try:
            return relative_date(dt.date.fromisoformat(m.group(1)), asof)
        except ValueError:
            return m.group(0)

    return _ISO_DATE.sub(rep, text)


def escape_untrusted(text: str) -> str:
    text = _CLOSE.sub("&lt;/untrusted_content", text)
    return _OPEN.sub("&lt;untrusted_content", text)


def wrap_untrusted(text: str) -> str:
    return f"<untrusted_content>{escape_untrusted(text)}</untrusted_content>"


def _is_date_key(key: str) -> bool:
    return key in _DATE_KEYS or key.endswith("_date") or key.endswith("_at")


def _clean(value: Any, *, key: str, asof: dt.date, scrub: Scrubber, untrusted: bool) -> Any:
    if isinstance(value, Mapping):
        return {
            str(k): _clean(v, key=str(k), asof=asof, scrub=scrub, untrusted=untrusted)
            for k, v in value.items()
            if str(k).lower() not in SENSITIVE_KEYS
        }
    if isinstance(value, list | tuple):
        return [_clean(v, key=key, asof=asof, scrub=scrub, untrusted=untrusted) for v in value]
    if _is_date_key(key):
        d = _as_date(value)
        if d is not None:
            return relative_date(d, asof)
    if isinstance(value, dt.date | pd.Timestamp):
        return value.isoformat()
    if isinstance(value, float) and value != value:  # NaN -> null
        return None
    if isinstance(value, str):
        text = _shift_text_dates(scrub(value), asof)
        if untrusted and key in UNTRUSTED_FIELDS:
            return wrap_untrusted(text)
        return text
    return value


def assert_safe(obj: Any, path: str = "packet") -> None:
    """Raise PacketLeak if any sensitive key survived (defense in depth)."""
    if isinstance(obj, BaseModel):
        obj = obj.model_dump(mode="json")
    if isinstance(obj, Mapping):
        for k, v in obj.items():
            if str(k).lower() in SENSITIVE_KEYS:
                raise PacketLeak(f"sensitive field {path}.{k} in agent packet")
            assert_safe(v, f"{path}.{k}")
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            assert_safe(v, f"{path}[{i}]")


# ------------------------------------------------------------- item grouping
def _group(
    kind: str, rows: list[dict[str, Any]], asof: dt.date
) -> list[tuple[str, dict[str, Any]]]:
    """Turn source rows into (label, data) evidence items for one kind."""
    if not rows:
        return []
    if kind == "fundamentals":
        by_metric: dict[str, list[dict[str, Any]]] = {}
        for r in rows:
            by_metric.setdefault(str(r.get("metric")), []).append(r)
        out = []
        for metric in sorted(by_metric):
            series = sorted(by_metric[metric], key=lambda r: str(r.get("period_end", "")))
            out.append(
                (
                    f"fundamentals: {metric}",
                    {
                        "metric": metric,
                        "series": [
                            {
                                "fiscal_period": r.get("fiscal_period"),
                                "period_end": r.get("period_end"),
                                "value": r.get("value"),
                            }
                            for r in series
                        ],
                    },
                )
            )
        return out
    if kind == "macro":
        by_series: dict[str, list[dict[str, Any]]] = {}
        for r in rows:
            by_series.setdefault(str(r.get("series_id")), []).append(r)
        return [
            (
                f"macro: {sid}",
                {
                    "series_id": sid,
                    "observations": [
                        {"obs_date": r.get("obs_date"), "value": r.get("value")}
                        for r in sorted(by_series[sid], key=lambda r: str(r.get("obs_date", "")))
                    ],
                },
            )
            for sid in sorted(by_series)
        ]
    if kind == "prices":
        return [("price path (indexed to 100 at start)", price_path_summary(rows, asof))]
    labels: dict[str, Callable[[dict[str, Any]], str]] = {
        "signals": lambda r: f"signal: {r.get('signal_name', '')}",
        "news": lambda r: "news article",
        "insider_txns": lambda r: f"insider transaction ({r.get('txn_code', '?')})",
        "filing_diffs": lambda r: f"filing change: {r.get('form', '')} item {r.get('item', '')}",
        "filings_8k": lambda r: f"8-K items {r.get('items', '')}",
        "scenarios": lambda r: f"scenario: {r.get('id', r.get('scenario_id', ''))}",
        "exposures": lambda r: f"exposure: {r.get('factor', r.get('theme', ''))}",
        "risk_indexes": lambda r: f"risk index: {r.get('index_id', '')}",
    }
    default_label = kind.replace("_", " ")
    fn = labels.get(kind)
    return [(fn(r) if fn else default_label, r) for r in rows]


def price_path_summary(rows: list[dict[str, Any]], asof: dt.date) -> dict[str, Any]:
    """Relative price path: no absolute prices (they can identify the company)."""
    pts = sorted(
        ((_as_date(r.get("date")), float(r["adj_close"])) for r in rows if r.get("adj_close")),
        key=lambda x: x[0] or dt.date.min,
    )
    pts = [(d, p) for d, p in pts if d is not None and d <= asof and p > 0]
    if not pts:
        return {"observations": 0}
    base = pts[0][1]
    last = pts[-1][1]

    def ret_over(days: int) -> float | None:
        cutoff = asof - dt.timedelta(days=days)
        prior = [p for d, p in pts if d is not None and d <= cutoff]
        return round(last / prior[-1] - 1, 4) if prior else None

    daily = [pts[i][1] / pts[i - 1][1] - 1 for i in range(1, len(pts))]
    month = [
        r
        for (d, _), r in zip(pts[1:], daily, strict=True)
        if d and d >= asof - dt.timedelta(days=31)
    ]
    step = max(1, len(pts) // 24)
    return {
        "observations": len(pts),
        "return_1m": ret_over(30),
        "return_60d": ret_over(60),
        "return_3m": ret_over(91),
        "return_12m": ret_over(365),
        "max_daily_return_1m": round(max(month), 4) if month else None,
        "path": [
            {"date": d.isoformat() if d else None, "index": round(100 * p / base, 2)}
            for d, p in pts[::step]
        ],
    }


# -------------------------------------------------------------------- builder
def build_review_packet(
    source: PacketSource,
    security_id: str,
    asof: dt.date,
    review_id: str,
    *,
    bucket_tag: str | None,
    context: Mapping[str, Any] | None = None,
    extras: Mapping[str, Iterable[Mapping[str, Any]]] | None = None,
    kinds: Iterable[str] = SOURCE_KINDS,
) -> ReviewPacket:
    """Build the review's packet. ``extras`` supply code-computed kinds (engine outputs,
    base-rate table) that override the source."""
    info = source.security(security_id, asof)
    anon = anon_id_for(security_id, review_id)
    scrub = Scrubber(info, anon)
    items: list[EvidenceItem] = []
    n = 0
    extras = extras or {}
    for kind in kinds:
        rows = (
            [dict(r) for r in extras[kind]]
            if kind in extras
            else source.fetch(kind, security_id, asof)
        )
        for label, data in _group(kind, rows, asof):
            n += 1
            clean = _clean(data, key="", asof=asof, scrub=scrub, untrusted=kind in UNTRUSTED_KINDS)
            items.append(EvidenceItem(id=f"E{n}", kind=kind, label=scrub(label), data=clean))
    ctx: dict[str, Any] = {"sector": info.sector, "size_bucket": info.size_bucket}
    if context:
        ctx.update(_clean(dict(context), key="", asof=asof, scrub=scrub, untrusted=False))
    pkt = ReviewPacket(
        review_id=review_id,
        anon_id=anon,
        asof=asof.isoformat(),
        bucket_tag=bucket_tag,
        context=ctx,
        items=items,
    )
    assert_safe(pkt)
    return pkt
