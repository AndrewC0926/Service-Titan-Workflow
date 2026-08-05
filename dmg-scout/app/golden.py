"""Golden-set extraction evaluation.

Workflow:
  1. `scout golden collect --limit 30` — sample extracted documents, weighted
     toward head+tail fallbacks first, then CEQAnet environmental filings and
     CivicPlus packets (the sources that carry MW and generator figures), and
     snapshot text + model output into evals/golden.jsonl and evals/docs/.
     `--include-doc <id>` forces in a document triage dropped; `scout doc-stats
     --fallbacks` lists the ones worth forcing.
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


TAIL_FALLBACK = "tail_fallback"  # app.sections marks the head+tail path with this


def _no_sections_matched(signal: Signal) -> bool:
    """True if section-aware chunking found nothing and fell back to head+tail.

    These are the documents most likely to have silently dropped a megawatt figure:
    the MW number lives in a utilities or air-quality section, and when no section
    header matched, the middle of the document — where that section sits — was never
    sent to the model at all. Sampling them is the only way to tell a document that
    states no MW from one whose MW we threw away.

    Detected by the `tail_fallback` marker, NOT by an empty `sections_found`: the
    fallback still reports `["document_head", "tail_fallback"]`, so testing for
    emptiness silently matched nothing at all.
    """
    sections = (signal.extraction_json or {}).get("sections") or {}
    return TAIL_FALLBACK in (sections.get("sections_found") or [])


def collect_forced(session: Session, doc_ids: list[int]) -> int:
    """Add specific documents to the golden set even if triage dropped them.

    Exists for the head+tail fallbacks. Those documents are 60k-122k chars, triage
    judged them on the first 6,000, and section chunking would show extraction only
    the head and tail — two compounding blind spots over the same middle of the
    document. The only way to know whether a real project is hiding in there is to
    extract one and read it by hand.

    Extraction runs here but NO Signal row is written: these documents are
    triage-negative, and materialising signals for them would invent projects on the
    board. The model output lives in the golden entry only.
    """
    from app.llm import extract as llm_extract

    entries = load_golden()
    existing = {e["doc_key"] for e in entries}
    DOCS_DIR.mkdir(parents=True, exist_ok=True)
    added = 0
    for doc_id in doc_ids:
        doc = session.get(RawDocument, doc_id)
        if doc is None:
            continue
        key = f"{doc.source}:{doc.source_uid}"
        if key in existing:
            continue
        data = llm_extract(doc.raw_text, title=doc.title, source=doc.source, url=doc.url)
        sections = data.get("_sections") or {}
        model = {f: data.get(f) for f in SCALAR_FIELDS if f != "stage"}
        model["stage"] = data.get("stage") or "unknown"
        model["named_people"] = data.get("named_people") or []
        model["named_firms"] = data.get("named_firms") or []
        text_sha = hashlib.sha256(doc.raw_text.encode()).hexdigest()
        text_path = DOCS_DIR / f"{text_sha[:16]}.txt"
        text_path.write_text(doc.raw_text)
        entries.append({
            "doc_key": key, "source": doc.source, "url": doc.url, "title": doc.title,
            "text_sha256": text_sha, "text_file": text_path.name,
            "chunked": bool(sections.get("chunked")),
            "sections_found": sections.get("sections_found") or [],
            "head_tail_fallback": TAIL_FALLBACK in (sections.get("sections_found") or []),
            "original_chars": sections.get("original_chars"),
            "selected_chars": sections.get("selected_chars"),
            # Recorded so the reviewer knows triage dropped this and why: if the doc
            # DOES contain a project, that is a triage miss, not an extraction miss.
            "forced": True,
            "triage_result": doc.triage_result.value,
            "triage_reason": doc.triage_reason,
            "model": model, "truth": None, "verified": False,
        })
        existing.add(key)
        added += 1
    save_golden(entries)
    return added


def collect(session: Session, limit: int = 30) -> int:
    """Sample extracted docs into the golden file (skipping ones already there).

    Stratified by source in proportion to how many extracted documents each one
    contributes, because the golden set has to look like the corpus the board
    actually runs on. Strict source-priority ordering does not: it produced a set
    that was 19 of 30 GOED, and after CEQAnet was re-backfilled the same rule would
    have filled all 30 slots with CEQAnet — the opposite skew, equally unusable as
    a measure of extraction quality.

    Two things still jump the queue, deliberately:
      - head+tail fallbacks, where chunking showed the model only the ends of the
        document and a missed MW may be a chunking bug rather than an absent value;
      - within each source, CEQA environmental documents (NOP/DEIR/EIR) and then
        the largest documents, since those are where MW and generator specs live.
    """
    existing = {e["doc_key"] for e in load_golden()}
    signals = session.exec(select(Signal).where(Signal.raw_document_id.is_not(None))).all()
    by_doc = {s.raw_document_id: s for s in signals}
    docs = [session.get(RawDocument, did) for did in by_doc]
    docs = [d for d in docs if d is not None and f"{d.source}:{d.source_uid}" not in existing]

    def within_source(d: RawDocument) -> tuple:
        # Lower sorts first, inside one source's pool.
        env_doc = (d.source == "ceqanet"
                   and (d.meta or {}).get("document_type") in ("NOP", "DEIR", "EIR"))
        return (0 if _no_sections_matched(by_doc[d.id]) else 1,
                0 if env_doc else 1,
                -len(d.raw_text))

    pools: dict[str, list[RawDocument]] = {}
    for d in docs:
        pools.setdefault(d.source, []).append(d)
    for pool in pools.values():
        pool.sort(key=within_source)

    # One slot per source first, then largest-remainder on what is left. The floor
    # matters: proportional allocation alone gives a source holding 1 of 42
    # documents 0.24 slots and therefore none, and an adapter with no documents in
    # the set is an adapter whose extraction quality is simply unmeasured. EDGAR
    # filings look nothing like GOED packets, so "small" is not "skippable".
    total = sum(len(p) for p in pools.values()) or 1
    floor = 1 if len(pools) <= limit else 0
    alloc = {src: min(len(p), floor) for src, p in pools.items()}
    remaining = limit - sum(alloc.values())
    quotas = {src: remaining * len(p) / total for src, p in pools.items()}
    for src, q in quotas.items():
        take = min(len(pools[src]) - alloc[src], int(q))
        alloc[src] += take
    for src in sorted(pools, key=lambda s: quotas[s] - int(quotas[s]), reverse=True):
        if sum(alloc.values()) >= limit:
            break
        if alloc[src] < len(pools[src]):
            alloc[src] += 1

    docs = []
    for src in sorted(pools):
        docs.extend(pools[src][:alloc[src]])
    # Any shortfall (a source ran dry) is backfilled from whatever is left, so the
    # set still reaches `limit` rather than quietly coming up short.
    if len(docs) < limit:
        chosen = {id(d) for d in docs}
        leftovers = [d for src in sorted(pools) for d in pools[src] if id(d) not in chosen]
        leftovers.sort(key=within_source)
        docs.extend(leftovers[:limit - len(docs)])
    docs.sort(key=lambda d: (d.source, within_source(d)))
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
        sections = (signal.extraction_json or {}).get("sections") or {}
        entries.append({
            "doc_key": key, "source": doc.source, "url": doc.url, "title": doc.title,
            "text_sha256": text_sha, "text_file": text_path.name,
            # Chunking record: a field missed on a head+tail fallback may be a
            # chunking failure rather than a value the document never stated, and
            # the reviewer has to be able to tell those apart.
            "chunked": bool(sections.get("chunked")),
            "sections_found": sections.get("sections_found") or [],
            "head_tail_fallback": _no_sections_matched(signal),
            "original_chars": sections.get("original_chars"),
            "selected_chars": sections.get("selected_chars"),
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
    if entry.get("forced"):
        print_fn(f"!! TRIAGE DROPPED THIS ({entry.get('triage_result')}): "
                 f"{entry.get('triage_reason') or '(no reason)'}")
        print_fn("   Triage read only the first 6,000 chars. If this document DOES "
                 "contain a real building project, that is a triage miss — say so.")
    if entry.get("head_tail_fallback"):
        print_fn(f"!! HEAD+TAIL FALLBACK — no section headers matched. The model saw "
                 f"{entry.get('selected_chars') or '?'} of {entry.get('original_chars') or '?'} "
                 f"chars, and NOT the middle of the document. If a megawatt or generator "
                 f"figure is in this text but the model missed it, that is a chunking bug, "
                 f"not a model miss — record the true value anyway.")
    elif entry.get("chunked"):
        print_fn(f"   section-chunked: {', '.join(entry.get('sections_found') or [])} "
                 f"({entry.get('selected_chars')} of {entry.get('original_chars')} chars sent)")
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


# Fields whose recall decides whether a tonnage estimate can be defended at all.
# Called out by name because burying mw_it in an alphabetical table is how a 0% recall
# goes unnoticed while 38 of 43 estimates quietly fall back to guessing from sqft.
SIZING_FIELDS = ["mw_it", "mw_total", "generator_count", "generator_hp_each",
                 "generator_kw_each", "building_sqft"]
RECALL_FLOOR = 0.50


def report_text(result: dict) -> str:
    if not result["n_verified"]:
        return ("Golden set: 0 hand-verified documents — nothing to score yet.\n"
                "Run `scout golden review` first; precision and recall are measured "
                "against hand-entered truth, and there is no honest way to synthesise it.")

    lines = [f"Golden set: {result['n_verified']} hand-verified documents", "",
             f"{'field':22s} {'precision':>9s} {'recall':>7s} {'fab':>4s} {'wrong':>5s} {'miss':>5s}"]
    for f, s in result["fields"].items():
        if s.tp + s.fabricated + s.wrong + s.missed == 0:
            continue
        p = f"{s.precision:.2f}" if s.precision is not None else "  — "
        r = f"{s.recall:.2f}" if s.recall is not None else "  — "
        lines.append(f"{f:22s} {p:>9s} {r:>7s} {s.fabricated:>4d} {s.wrong:>5d} {s.missed:>5d}")

    lines += ["", "SIZING INPUTS — these decide whether a tonnage number is defensible:"]
    for f in SIZING_FIELDS:
        s = result["fields"].get(f)
        if s is None:
            continue
        seen = s.tp + s.wrong + s.missed
        if not seen:
            lines.append(f"  {f:20s} not stated in any verified document — "
                         f"nothing to recall")
            continue
        r = s.recall or 0.0
        flag = "  << BELOW 50%" if r < RECALL_FLOOR else ""
        lines.append(f"  {f:20s} recall {r:.0%} ({s.tp}/{seen} stated values captured)"
                     f"{flag}")
    low = [f for f in SIZING_FIELDS
           if (s := result["fields"].get(f)) and (s.tp + s.wrong + s.missed)
           and (s.recall or 0.0) < RECALL_FLOOR]
    if low:
        lines += ["", f"RECALL BELOW {RECALL_FLOOR:.0%} on: {', '.join(low)}.",
                  ("Say this out loud rather than working around it: every project relying "
                   "on one of these falls back to a square-footage guess.")]

    if result["fabrications"]:
        lines += ["", "FABRICATIONS (hard failure — fix the prompt and re-run):"]
        for f, s in result["fabrications"].items():
            for ex in s.examples[:10]:
                lines.append(f"  {f}: {ex}")
    else:
        lines += ["", "Zero fabricated values."]
    return "\n".join(lines)
