"""Golden-set extraction evaluation.

Workflow:
  1. `scout golden collect --limit 30` — sample extracted documents (weighted
     toward CEQAnet NOPs and GOED packets), snapshot text + model output into
     evals/golden.jsonl and evals/docs/.
  2. `scout golden review` — hand-verify one document at a time: the model's
     value for each field next to the source text; Enter accepts, a typed value
     corrects, 'null' marks not-stated.
  3. `scout golden report` — per-field precision/recall and the fabrication
     list. Exit code 1 on any fabrication: an invented number is a system
     failure, a missed field is not.

The verified set doubles as a pytest regression suite (tests/test_golden_regression.py)
so a prompt change can never silently degrade extraction.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path

from sqlmodel import Session, select

from app.models import RawDocument, Signal
from app.normalize import normalize_name

EVALS_DIR = Path(__file__).resolve().parent.parent / "evals"
GOLDEN_PATH = EVALS_DIR / "golden.jsonl"
DOCS_DIR = EVALS_DIR / "docs"

# Fields scored in the report. Scalars compare directly (numbers with 1%
# tolerance); list fields match items by normalized name.
SCALAR_FIELDS = [
    "project_name", "developer_or_owner", "county", "state", "mw_it", "mw_total",
    "generator_count", "generator_hp_each", "generator_kw_each", "building_sqft",
    "acres", "stage", "apn_parcel", "cooling_type",
]
NUMERIC = {"mw_it", "mw_total", "generator_count", "generator_hp_each",
           "generator_kw_each", "building_sqft", "acres"}
LIST_FIELDS = ["named_people", "named_firms"]
# Fabrication is only meaningful for facts, not judgment calls like stage.
FABRICATION_FIELDS = NUMERIC | {"apn_parcel", "project_name", "developer_or_owner"}


def load_golden() -> list[dict]:
    if not GOLDEN_PATH.exists():
        return []
    return [json.loads(line) for line in GOLDEN_PATH.read_text().splitlines() if line.strip()]


def save_golden(entries: list[dict]) -> None:
    EVALS_DIR.mkdir(exist_ok=True)
    GOLDEN_PATH.write_text("\n".join(json.dumps(e, default=str) for e in entries) + "\n")


def collect(session: Session, limit: int = 30) -> int:
    """Sample extracted docs into the golden file (skipping ones already there)."""
    existing = {e["doc_key"] for e in load_golden()}
    signals = session.exec(select(Signal).where(Signal.raw_document_id.is_not(None))).all()
    by_doc = {s.raw_document_id: s for s in signals}
    docs = [session.get(RawDocument, did) for did in by_doc]
    docs = [d for d in docs if d is not None]

    def weight(d: RawDocument) -> int:
        if d.source == "ceqanet" and (d.meta or {}).get("document_type") == "NOP":
            return 0
        if d.source == "goed":
            return 1
        if d.source == "ceqanet":
            return 2
        return 3

    docs.sort(key=weight)
    entries = load_golden()
    added = 0
    DOCS_DIR.mkdir(parents=True, exist_ok=True)
    for doc in docs:
        key = f"{doc.source}:{doc.source_uid}"
        if key in existing or added >= limit:
            continue
        text_sha = hashlib.sha256(doc.raw_text.encode()).hexdigest()
        text_path = DOCS_DIR / f"{text_sha[:16]}.txt"
        text_path.write_text(doc.raw_text)
        signal = by_doc[doc.id]
        model = {f: getattr(signal, f) for f in SCALAR_FIELDS if f != "stage"}
        model["stage"] = signal.stage.value
        model["named_people"] = signal.named_people
        model["named_firms"] = signal.named_firms
        entries.append({
            "doc_key": key, "source": doc.source, "url": doc.url, "title": doc.title,
            "text_sha256": text_sha, "text_file": text_path.name,
            "model": model, "truth": None, "verified": False,
        })
        added += 1
    save_golden(entries)
    return added


def review_entry(entry: dict, input_fn=input, print_fn=print) -> dict:
    """Interactive hand-verification of one entry. Returns the updated entry."""
    text = (DOCS_DIR / entry["text_file"]).read_text()
    print_fn("=" * 78)
    print_fn(f"[{entry['source']}] {entry['title']}")
    print_fn(entry["url"])
    print_fn("-" * 78)
    print_fn(text[:3500])
    if len(text) > 3500:
        print_fn(f"... [{len(text) - 3500} more chars — full text in evals/docs/{entry['text_file']}]")
    print_fn("-" * 78)
    print_fn("Enter = model is right | typed value = correction | 'null' = not stated in doc")
    truth: dict = {}
    for f in SCALAR_FIELDS:
        model_val = entry["model"].get(f)
        raw = input_fn(f"  {f} [{model_val!r}]: ").strip()
        if raw == "":
            truth[f] = model_val
        elif raw.lower() == "null":
            truth[f] = None
        elif f in NUMERIC:
            truth[f] = float(raw)
        else:
            truth[f] = raw
    for f in LIST_FIELDS:
        model_val = entry["model"].get(f) or []
        names = "; ".join(x.get("name", "?") for x in model_val) or "(none)"
        raw = input_fn(f"  {f} [{names}] (Enter=right, or semicolon-separated correct names): ").strip()
        if raw == "":
            truth[f] = model_val
        elif raw.lower() == "null":
            truth[f] = []
        else:
            truth[f] = [{"name": n.strip()} for n in raw.split(";") if n.strip()]
    entry["truth"] = truth
    entry["verified"] = True
    return entry


def review(input_fn=input, print_fn=print) -> int:
    entries = load_golden()
    done = 0
    for entry in entries:
        if entry.get("verified"):
            continue
        review_entry(entry, input_fn, print_fn)
        save_golden(entries)  # save after every doc — never lose hand work
        done += 1
    return done


@dataclass
class FieldScore:
    tp: int = 0          # model non-null and correct
    fabricated: int = 0  # model non-null, truth null — the cardinal sin
    wrong: int = 0       # both non-null, values differ
    missed: int = 0      # model null, truth non-null
    examples: list = field(default_factory=list)

    @property
    def precision(self) -> float | None:
        denom = self.tp + self.fabricated + self.wrong
        return self.tp / denom if denom else None

    @property
    def recall(self) -> float | None:
        denom = self.tp + self.wrong + self.missed
        return self.tp / denom if denom else None


def _scalar_match(field_name: str, a, b) -> bool:
    if a is None or b is None:
        return a is None and b is None
    if field_name in NUMERIC:
        try:
            fa, fb = float(a), float(b)
        except (TypeError, ValueError):
            return False
        return abs(fa - fb) <= 0.01 * max(abs(fa), abs(fb), 1e-9)
    return str(a).strip().lower() == str(b).strip().lower()


def score(entries: list[dict] | None = None, model_key: str = "model") -> dict:
    """Per-field scores over verified entries. model_key allows re-scoring a
    fresh extraction pass ('rerun') against the same hand-verified truth."""
    entries = [e for e in (entries if entries is not None else load_golden()) if e.get("verified")]
    fields: dict[str, FieldScore] = {f: FieldScore() for f in SCALAR_FIELDS + LIST_FIELDS}
    for e in entries:
        model, truth = e.get(model_key) or {}, e["truth"]
        for f in SCALAR_FIELDS:
            m, t = model.get(f), truth.get(f)
            if m is None and t is None:
                continue
            if m is not None and t is None:
                fields[f].fabricated += 1
                fields[f].examples.append(f"{e['doc_key']}: model={m!r} but doc does not state it")
            elif m is None:
                fields[f].missed += 1
            elif _scalar_match(f, m, t):
                fields[f].tp += 1
            else:
                fields[f].wrong += 1
                fields[f].examples.append(f"{e['doc_key']}: model={m!r} truth={t!r}")
        for f in LIST_FIELDS:
            m_names = {normalize_name(x.get("name", "")) for x in (model.get(f) or [])}
            t_names = {normalize_name(x.get("name", "")) for x in (truth.get(f) or [])}
            m_names.discard("")
            t_names.discard("")
            fields[f].tp += len(m_names & t_names)
            fields[f].fabricated += len(m_names - t_names)
            fields[f].missed += len(t_names - m_names)
            for extra in m_names - t_names:
                fields[f].examples.append(f"{e['doc_key']}: model named {extra!r}, not in doc")
    fabrications = {
        f: s for f, s in fields.items()
        if s.fabricated and (f in FABRICATION_FIELDS or f in LIST_FIELDS)
    }
    return {"n_verified": len(entries), "fields": fields, "fabrications": fabrications}


def report_text(result: dict) -> str:
    lines = [f"Golden set: {result['n_verified']} hand-verified documents", "",
             f"{'field':22s} {'precision':>9s} {'recall':>7s} {'fab':>4s} {'wrong':>5s} {'miss':>5s}"]
    for f, s in result["fields"].items():
        if s.tp + s.fabricated + s.wrong + s.missed == 0:
            continue
        p = f"{s.precision:.2f}" if s.precision is not None else "  — "
        r = f"{s.recall:.2f}" if s.recall is not None else "  — "
        lines.append(f"{f:22s} {p:>9s} {r:>7s} {s.fabricated:>4d} {s.wrong:>5d} {s.missed:>5d}")
    if result["fabrications"]:
        lines += ["", "FABRICATIONS (hard failure — fix the prompt and re-run):"]
        for f, s in result["fabrications"].items():
            for ex in s.examples[:10]:
                lines.append(f"  {f}: {ex}")
    else:
        lines += ["", "Zero fabricated values."]
    return "\n".join(lines)
