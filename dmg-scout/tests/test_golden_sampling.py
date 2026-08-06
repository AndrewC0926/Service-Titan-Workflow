"""The golden set must look like the corpus, or it does not measure extraction.

Strict source-priority ordering gave a set that was 19 of 30 GOED; after CEQAnet was
re-backfilled the same rule would have filled all 30 slots with CEQAnet. Both are
skews, and a skewed sample measures one adapter rather than the board.
"""
from app.golden import GOLDEN_PATH, collect, load_golden
from app.models import (
    Category, RawDocument, Signal, SignalType, Stage, TriageResult,
)


def _doc(db_session, source, uid, chars=2000, doctype=None, sections=None):
    doc = RawDocument(source=source, source_uid=uid, url=f"https://x/{source}/{uid}",
                      title=f"{source} {uid}", raw_text="x" * chars,
                      content_hash=f"{source}-{uid}",
                      triage_result=TriageResult.relevant,
                      meta={"document_type": doctype} if doctype else {})
    db_session.add(doc)
    db_session.commit()
    sig = Signal(raw_document_id=doc.id, signal_type=SignalType.ceqa_nop,
                 category=Category.data_center, stage=Stage.entitlement,
                 project_name=f"P{uid}", county="Storey", state="NV",
                 extraction_json={"sections": sections} if sections else {})
    db_session.add(sig)
    db_session.commit()
    return doc


def _isolate(tmp_path, monkeypatch):
    """Point the golden file at tmp so a real evals/golden.jsonl is never touched."""
    import app.golden as g
    monkeypatch.setattr(g, "GOLDEN_PATH", tmp_path / "golden.jsonl")
    monkeypatch.setattr(g, "DOCS_DIR", tmp_path / "docs")


def test_sample_is_proportional_to_each_source(db_session, tmp_path, monkeypatch):
    """60 goed / 20 ceqanet / 20 civicplus over 20 slots should land near 12/4/4 —
    not 20 of whichever source sorts first. Asserted as proportions rather than exact
    counts, since the per-source floor legitimately shifts a slot or two."""
    _isolate(tmp_path, monkeypatch)
    for i in range(60):
        _doc(db_session, "goed", f"g{i}")
    for i in range(20):
        _doc(db_session, "ceqanet", f"c{i}")
    for i in range(20):
        _doc(db_session, "civicplus", f"p{i}")

    assert collect(db_session, limit=20) == 20
    from app.golden import load_golden as lg
    counts: dict[str, int] = {}
    for e in lg():
        counts[e["source"]] = counts.get(e["source"], 0) + 1
    assert sum(counts.values()) == 20
    assert set(counts) == {"goed", "ceqanet", "civicplus"}
    # goed holds 60% of the corpus and must dominate, but not monopolise.
    assert counts["goed"] == max(counts.values())
    assert 9 <= counts["goed"] <= 13
    assert 3 <= counts["ceqanet"] <= 6
    assert 3 <= counts["civicplus"] <= 6
    # The failure being guarded against: one source taking every slot.
    assert counts["goed"] < 20


def test_no_source_with_documents_is_shut_out(db_session, tmp_path, monkeypatch):
    """A source contributing a couple of documents still gets a slot; under the old
    ordering a low-priority source could be entirely absent."""
    _isolate(tmp_path, monkeypatch)
    for i in range(40):
        _doc(db_session, "goed", f"g{i}")
    _doc(db_session, "ceqanet", "c0")
    _doc(db_session, "edgar", "e0")

    collect(db_session, limit=10)
    from app.golden import load_golden as lg
    sources = {e["source"] for e in lg()}
    assert sources == {"goed", "ceqanet", "edgar"}


def test_head_tail_fallbacks_come_first_within_their_source(db_session, tmp_path,
                                                            monkeypatch):
    """Chunking showed the model only the ends of these, so a missed MW may be a
    chunking bug rather than an absent value. They must be in the set."""
    _isolate(tmp_path, monkeypatch)
    fallback = {"chunked": True,
                "sections_found": ["document_head", "tail_fallback"],
                "original_chars": 90_000, "selected_chars": 34_050}
    for i in range(20):
        _doc(db_session, "civicplus", f"p{i}")
    _doc(db_session, "civicplus", "fallback1", chars=90_000, sections=fallback)

    collect(db_session, limit=4)
    from app.golden import load_golden as lg
    entries = lg()
    assert any(e["head_tail_fallback"] for e in entries), \
        "the fallback document did not make the sample"


def test_env_documents_lead_their_source(db_session, tmp_path, monkeypatch):
    """Within CEQAnet, NOP/DEIR/EIR carry the engineering numbers, so they should be
    sampled ahead of notices of exemption."""
    _isolate(tmp_path, monkeypatch)
    for i in range(10):
        _doc(db_session, "ceqanet", f"noe{i}", doctype="NOE")
    _doc(db_session, "ceqanet", "nop1", doctype="NOP")

    collect(db_session, limit=2)
    from app.golden import load_golden as lg
    assert any("nop1" in e["doc_key"] for e in lg())


def test_collect_is_idempotent(db_session, tmp_path, monkeypatch):
    """Re-running must not duplicate entries already hand-verified."""
    _isolate(tmp_path, monkeypatch)
    for i in range(10):
        _doc(db_session, "goed", f"g{i}")
    first = collect(db_session, limit=5)
    second = collect(db_session, limit=5)
    from app.golden import load_golden as lg
    keys = [e["doc_key"] for e in lg()]
    assert first == 5
    assert len(keys) == len(set(keys)) == first + second


def test_limit_is_reached_even_when_a_source_runs_dry(db_session, tmp_path,
                                                      monkeypatch):
    """Quotas are proportional, but a shortfall must be backfilled rather than
    silently returning fewer documents than asked for."""
    _isolate(tmp_path, monkeypatch)
    for i in range(9):
        _doc(db_session, "goed", f"g{i}")
    _doc(db_session, "ceqanet", "c0")
    assert collect(db_session, limit=10) == 10


def test_worksheet_shows_every_asserted_value_with_a_blank_verdict(db_session, tmp_path,
                                                                   monkeypatch):
    """The offline handoff. Every field gets a row whether or not it was extracted,
    because a null the document contradicts is as interesting as a wrong value."""
    from pathlib import Path
    from app.golden import write_worksheet

    _isolate(tmp_path, monkeypatch)
    _doc(db_session, "ceqanet", "c1", doctype="NOP")
    collect(db_session, limit=1)

    out = tmp_path / "ws.md"
    assert write_worksheet(out) == 1
    text = out.read_text()
    assert "| field | model asserted | your verdict |" in text
    for f in ("mw_it", "mw_total", "generator_count", "building_sqft", "stage",
              "county", "project_name", "named_people"):
        assert f"| {f} |" in text, f"{f} missing from the worksheet"
    assert "*(null)*" in text          # unextracted fields still get a row
    assert "evals/docs/" in text       # the source text is reachable


def test_worksheet_marks_head_tail_fallbacks(db_session, tmp_path, monkeypatch):
    from pathlib import Path
    from app.golden import write_worksheet

    _isolate(tmp_path, monkeypatch)
    _doc(db_session, "civicplus", "p1", chars=90_000,
         sections={"chunked": True,
                   "sections_found": ["document_head", "tail_fallback"],
                   "original_chars": 90_000, "selected_chars": 34_050})
    collect(db_session, limit=1)
    out = tmp_path / "ws.md"
    write_worksheet(out)
    assert "head+tail fallback" in out.read_text()


def test_fill_rates_count_populated_fields(db_session, tmp_path, monkeypatch):
    from app.golden import fill_rates
    _isolate(tmp_path, monkeypatch)
    _doc(db_session, "goed", "g1")
    collect(db_session, limit=1)
    rates = fill_rates()
    assert rates["project_name"]["filled"] == 1
    assert rates["mw_it"]["filled"] == 0        # never set by the fixture
    assert rates["mw_it"]["rate"] == 0.0
