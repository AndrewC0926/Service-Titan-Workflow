"""Numeric grounding audit: does the source document actually contain the number?

The dangerous extraction failure is not a null, it is a confident wrong value. A
missed MW costs a sizing estimate; an invented one puts a number in front of a
customer that the filing does not support. Amperesand's 500 MW became 357 MW IT and
136,964 tons, and nothing in the pipeline objected, because "stated" outranks every
low-confidence flag.

This checks the one thing that needs no human: if the model asserts 99 MW, does the
string 99 appear in the document at all? A number absent from the source cannot have
been read from it.

Deliberately NOT a correctness check. A grounded number can still be the wrong number
— read off the wrong row, or the product rating rather than the building load — and
only hand verification catches that. This finds fabrication, which is the subset that
can be found mechanically.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from sqlmodel import Session, select

from app.models import RawDocument, Signal

# Numeric fields worth auditing. Coordinates are excluded: they are transcribed from
# DMS to decimal, so the digits legitimately differ from the source text.
NUMERIC_FIELDS = [
    "mw_it", "mw_total", "generator_count", "generator_hp_each", "generator_kw_each",
    "building_sqft", "acres", "water_acre_feet_per_year", "building_count",
]
# Small integers appear everywhere by chance (page numbers, dates, item numbers), so
# a bare match proves nothing. Report them, but do not call them grounded evidence.
LOW_SIGNAL_BELOW = 10

# Fields where the number is meaningless without its unit, and where a wrong value
# is expensive. A dollar amount and a megawatt figure are both just digits; only the
# neighbouring token tells them apart, which is how Blue Owl's $163,355,xxx became
# 163.355 MW and 44,748 tons.
#
# Guarded by (?<![a-z]) rather than a leading \b. Bare "sf" and "mw" are real
# abbreviations but also live inside ordinary words ("tran-sf-er"), so the unit must
# not follow a LETTER. It may follow a digit, because filings glue the two together
# constantly -- "16MW", "8-12MW", "6MW" -- and a leading \b fails on every one of
# those, since there is no boundary between "6" and "M". That bug rejected Colovore's
# stated 16 MW, the second row on the data center board.
UNIT_TOKENS: dict[str, str] = {
    "mw_total": r"(?<![a-z])(?:mw|megawatts?)\b",
    "mw_it": r"(?<![a-z])(?:mw|megawatts?)\b",
    "generator_kw_each": r"(?<![a-z])(?:kw|kilowatts?)\b",
    "building_sqft": r"(?<![a-z])(?:sq\.?\s?ft\.?|sqft|sf|square[\s-]f(?:ee|oo)t)\b",
    "acres": r"(?<![a-z])acres?\b",
}
# How far from the matched digits the unit may sit. Wide enough for a flattened
# table row, where the column header carries the unit and the value sits several
# cells later: GOED board packets render as
#   "Year Land Cost Building SqFt | Cost Purchase Amount | Year-1 | ... 91,000"
# and a 40-char window rejected three real square-footage values on that layout
# alone. Widening cannot resurrect the cases this guard exists for — Blue Owl's
# filing contains no MW token at any distance, and FAAC's 84,800 appears nowhere at
# all — so the window trades no precision for real recall.
UNIT_PROXIMITY_CHARS = 120

_UNIT_RE: dict[str, re.Pattern] = {
    f: re.compile(p, re.I) for f, p in UNIT_TOKENS.items()
}


def unit_grounded(field: str, value: float, text: str) -> bool:
    """True if the value appears in the text with its unit close by.

    Grounding alone is not enough for these fields: a filing full of dollar figures
    in the 160-million range makes "163.355" findable in spirit and meaningless in
    fact. Requiring the unit token nearby is what separates 99 in "99 MW" from 99 in
    "$99,000,000".
    """
    pattern = _UNIT_RE.get(field)
    if pattern is None:
        return True                      # not a unit-bearing field; nothing to check
    lower = text.lower()
    for variant in _variants(value):
        for m in _value_pattern(variant.lower()).finditer(lower):
            lo = max(0, m.start() - UNIT_PROXIMITY_CHARS)
            hi = m.end() + UNIT_PROXIMITY_CHARS
            if pattern.search(lower[lo:hi]):
                return True
    return False


def reject_ungrounded_numbers(data: dict, text: str) -> tuple[dict, list[dict]]:
    """Null out unit-bearing numbers the document does not support, and say why.

    Rejects rather than downgrades. The whole pattern behind Amperesand and Blue Owl
    is that a bad number outranks a null everywhere downstream: it is "stated", so it
    beats the low-confidence flag, drives sizing, and lands on the board looking like
    the best-evidenced row there. A null costs an estimate; a confident wrong megawatt
    figure costs the credibility of every number beside it.
    """
    rejections: list[dict] = []
    for fname in UNIT_TOKENS:
        value = data.get(fname)
        if value is None:
            continue
        try:
            fval = float(value)
        except (TypeError, ValueError):
            continue
        if unit_grounded(fname, fval, text):
            continue
        data[fname] = None
        rejections.append({
            "field": fname, "value": fval,
            "reason": f"{fval:g} does not appear in the source document with a "
                      f"{fname.split('_')[-1]} unit within "
                      f"{UNIT_PROXIMITY_CHARS} characters",
            "nearest": _numbers_near(text, len(_variants(fval)[0])),
        })
    return data, rejections


@dataclass
class Finding:
    doc_id: int
    signal_id: int
    source: str
    title: str
    field: str
    value: float
    grounded: bool
    weak: bool                      # matched, but the value is too small to be evidence
    nearest: list = field(default_factory=list)   # nearby numbers actually in the text


def _variants(value: float) -> list[str]:
    """How a number might legitimately be written in a filing."""
    out: set[str] = set()
    as_int = int(value) if float(value).is_integer() else None
    if as_int is not None:
        out.add(str(as_int))
        out.add(f"{as_int:,}")
        # 1,100,000 sqft is often written "1.1 million"
        for unit, div in (("million", 1_000_000), ("thousand", 1_000)):
            if as_int >= div:
                q = as_int / div
                out.add(f"{q:g} {unit}")
                out.add(f"{q:g}-{unit}")
    else:
        out.add(f"{value:g}")
        out.add(f"{value:,.1f}")
        out.add(f"{value:,.2f}")
    return [v for v in out if v]


def _value_pattern(variant: str) -> re.Pattern:
    """Match a number as a number, not as a run of digits inside a bigger one.

    Plain substring search grounds "3" against the 3 in "$30,183,000" and "16"
    against "163" — so every small value grounds trivially and the guard silently
    stops guarding. Digit boundaries fix that while still allowing a unit to be glued
    on the right ("16MW"), since a letter is not a digit.
    """
    return re.compile(rf"(?<![\d.]){re.escape(variant)}(?![\d])")


def _numbers_near(text: str, needle_len: int) -> list[str]:
    """A sample of numbers that ARE in the document, for the report."""
    found = re.findall(r"\b\d[\d,]*(?:\.\d+)?\b", text)
    return sorted({f for f in found if len(f) >= max(2, needle_len - 1)},
                  key=lambda s: -len(s))[:12]


def audit_signal(signal: Signal, doc: RawDocument) -> list[Finding]:
    text = doc.raw_text or ""
    lower = text.lower()
    out: list[Finding] = []
    for fname in NUMERIC_FIELDS:
        value = getattr(signal, fname, None)
        if value is None:
            continue
        variants = _variants(float(value))
        grounded = any(_value_pattern(v.lower()).search(lower) for v in variants)
        weak = grounded and abs(float(value)) < LOW_SIGNAL_BELOW
        out.append(Finding(
            doc_id=doc.id, signal_id=signal.id, source=doc.source,
            title=doc.title or "", field=fname, value=float(value),
            grounded=grounded, weak=weak,
            nearest=[] if grounded else _numbers_near(text, len(variants[0])),
        ))
    return out


def audit_corpus(session: Session) -> dict:
    signals = session.exec(select(Signal).where(Signal.raw_document_id.is_not(None))).all()
    findings: list[Finding] = []
    for s in signals:
        doc = session.get(RawDocument, s.raw_document_id)
        if doc is not None:
            findings.extend(audit_signal(s, doc))
    ungrounded = [f for f in findings if not f.grounded]
    by_field: dict[str, dict] = {}
    for f in findings:
        b = by_field.setdefault(f.field, {"asserted": 0, "grounded": 0, "ungrounded": 0,
                                          "weak": 0})
        b["asserted"] += 1
        b["grounded" if f.grounded else "ungrounded"] += 1
        if f.weak:
            b["weak"] += 1
    return {"findings": findings, "ungrounded": ungrounded, "by_field": by_field,
            "n_signals": len(signals), "n_asserted": len(findings)}


def audit_text(result: dict) -> str:
    lines = [f"Numeric grounding audit — {result['n_asserted']} asserted values "
             f"across {result['n_signals']} signals", "",
             f"{'field':24s}{'asserted':>9}{'grounded':>10}{'UNGROUNDED':>12}{'weak':>7}"]
    for fname, b in sorted(result["by_field"].items(),
                           key=lambda kv: -kv[1]["ungrounded"]):
        lines.append(f"{fname:24s}{b['asserted']:>9}{b['grounded']:>10}"
                     f"{b['ungrounded']:>12}{b['weak']:>7}")
    ung = result["ungrounded"]
    if not ung:
        lines += ["", "Every asserted number appears in its source document.",
                  "That rules out invention, NOT misreading — a number can be present "
                  "and still be the wrong one. Hand verification is what catches that."]
        return "\n".join(lines)
    lines += ["", f"UNGROUNDED ASSERTIONS ({len(ung)}) — the model stated a number that "
                  f"does not appear in the document:"]
    for f in sorted(ung, key=lambda f: -abs(f.value)):
        lines.append(f"  doc {f.doc_id} [{f.source}] {f.title[:52]}")
        lines.append(f"    {f.field} = {f.value:g}   not found in source text")
        if f.nearest:
            lines.append(f"    numbers actually present: {', '.join(f.nearest[:8])}")
    return "\n".join(lines)
