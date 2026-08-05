"""The grounding audit catches invented numbers, which is the failure that matters."""
from app.grounding import _variants, audit_signal, audit_text, audit_corpus
from app.models import Category, RawDocument, Signal, SignalType, Stage, TriageResult


_seq = iter(range(1, 10_000))


def _pair(db_session, text, **fields):
    n = next(_seq)
    doc = RawDocument(source="ceqanet", source_uid=f"u{n}", url=f"https://x/{n}",
                      title="T", raw_text=text, content_hash=f"h{n}",
                      triage_result=TriageResult.relevant)
    db_session.add(doc); db_session.commit()
    sig = Signal(raw_document_id=doc.id, signal_type=SignalType.ceqa_nop,
                 category=Category.data_center, stage=Stage.permitting, **fields)
    db_session.add(sig); db_session.commit()
    return sig, doc


def test_amperesand_style_fabrication_is_caught(db_session):
    """The regression this exists for: a stated MW with no basis in the document."""
    sig, doc = _pair(db_session, "A 73,000 sq ft solid-state transformer factory.",
                     mw_total=500.0, building_sqft=73000.0)
    out = {f.field: f for f in audit_signal(sig, doc)}
    assert out["mw_total"].grounded is False
    assert out["building_sqft"].grounded is True     # 73,000 is right there
    assert "73,000" in out["mw_total"].nearest


def test_vernon_style_grounded_value_passes(db_session):
    sig, doc = _pair(db_session,
                     "a 99 MW emergency only power system ... forty (40) diesel generators",
                     mw_total=99.0, generator_count=40)
    out = {f.field: f for f in audit_signal(sig, doc)}
    assert out["mw_total"].grounded is True
    assert out["generator_count"].grounded is True


def test_comma_and_million_spellings_count_as_grounded(db_session):
    sig, doc = _pair(db_session, "two buildings totaling 1,100,000 sq ft",
                     building_sqft=1100000.0)
    assert audit_signal(sig, doc)[0].grounded is True

    sig2, doc2 = _pair(db_session, "roughly 1.1 million square feet of space",
                       building_sqft=1100000.0)
    assert audit_signal(sig2, doc2)[0].grounded is True


def test_small_integers_are_flagged_weak_not_treated_as_evidence(db_session):
    """A '2' matches almost any document by chance, so a match proves nothing."""
    sig, doc = _pair(db_session, "Item 2 on the agenda for the 2026 session.",
                     building_count=2)
    f = audit_signal(sig, doc)[0]
    assert f.grounded is True and f.weak is True


def test_nulls_are_not_audited(db_session):
    """A missing value is acceptable; only assertions are audited."""
    sig, doc = _pair(db_session, "no numbers here at all")
    assert audit_signal(sig, doc) == []


def test_report_distinguishes_invention_from_misreading(db_session):
    _pair(db_session, "A 73,000 sq ft factory.", mw_total=500.0)
    text = audit_text(audit_corpus(db_session))
    assert "UNGROUNDED ASSERTIONS (1)" in text
    assert "mw_total = 500" in text


def test_clean_corpus_says_what_it_does_not_prove(db_session):
    _pair(db_session, "a 99 MW system", mw_total=99.0)
    text = audit_text(audit_corpus(db_session))
    assert "rules out invention, NOT misreading" in text


def test_variants_cover_how_filings_write_numbers():
    v = _variants(1100000.0)
    assert "1100000" in v and "1,100,000" in v and "1.1 million" in v
