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
        grounded = any(v.lower() in lower for v in variants)
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
