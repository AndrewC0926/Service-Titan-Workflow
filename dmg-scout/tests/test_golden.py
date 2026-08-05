"""Golden harness logic: scoring math, fabrication detection, review flow."""
from app.golden import FieldScore, review_entry, score


def entry(model: dict, truth: dict) -> dict:
    base_model = {"named_people": [], "named_firms": []}
    base_truth = {"named_people": [], "named_firms": []}
    return {"doc_key": "t:1", "model": {**base_model, **model},
            "truth": {**base_truth, **truth}, "verified": True}


def test_correct_value_counts_tp():
    r = score([entry({"mw_it": 224.0}, {"mw_it": 224.0})])
    assert r["fields"]["mw_it"].tp == 1
    assert not r["fabrications"]


def test_numeric_tolerance_one_percent():
    r = score([entry({"mw_it": 224.0}, {"mw_it": 225.0})])
    assert r["fields"]["mw_it"].tp == 1  # within 1%
    r2 = score([entry({"mw_it": 224.0}, {"mw_it": 300.0})])
    assert r2["fields"]["mw_it"].wrong == 1


def test_fabrication_detected():
    """Model invents an MW figure the document never states — hard failure."""
    r = score([entry({"mw_it": 100.0}, {"mw_it": None})])
    assert r["fields"]["mw_it"].fabricated == 1
    assert "mw_it" in r["fabrications"]


def test_missed_field_is_not_fabrication():
    r = score([entry({"mw_it": None}, {"mw_it": 224.0})])
    assert r["fields"]["mw_it"].missed == 1
    assert not r["fabrications"]


def test_stage_disagreement_is_not_fabrication():
    """stage is a judgment call — wrong is wrong, but not the cardinal sin."""
    r = score([entry({"stage": "design"}, {"stage": "entitlement"})])
    assert r["fields"]["stage"].wrong == 1
    assert "stage" not in r["fabrications"]


def test_named_lists_matched_by_normalized_name():
    r = score([entry(
        {"named_firms": [{"name": "kW Mission Critical Engineering, Inc.", "role": "engineer_of_record"},
                         {"name": "Phantom Firm LLC", "role": "gc"}]},
        {"named_firms": [{"name": "kW Mission Critical Engineering"}]},
    )])
    s = r["fields"]["named_firms"]
    assert s.tp == 1 and s.fabricated == 1
    assert "named_firms" in r["fabrications"]


def test_precision_recall_math():
    s = FieldScore(tp=8, fabricated=1, wrong=1, missed=2)
    assert abs(s.precision - 0.8) < 1e-9
    assert abs(s.recall - 8 / 11) < 1e-9


def test_review_entry_accept_and_correct(tmp_path, monkeypatch):
    import app.golden as g
    monkeypatch.setattr(g, "DOCS_DIR", tmp_path)
    (tmp_path / "doc.txt").write_text("The project is 176 megawatts.")
    e = {"doc_key": "t:1", "source": "test", "url": "u", "title": "T",
         "text_file": "doc.txt",
         "model": {"mw_it": None, "mw_total": 176.0, "county": "Sann Bernardino",
                   "named_people": [], "named_firms": []},
         "truth": None, "verified": False}
    answers = iter(
        # scalar fields in SCALAR_FIELDS order:
        # project_name, developer, county(correct typo), state, mw_it, mw_total(accept),
        # generator_count, hp, kw, sqft, acres, stage, apn, cooling
        ["null", "null", "San Bernardino", "CA", "null", "", "null", "null", "null",
         "null", "null", "unknown", "null", "null",
         "", ""]  # two list fields accepted
    )
    out = review_entry(e, input_fn=lambda _: next(answers), print_fn=lambda *_: None)
    assert out["verified"]
    assert out["truth"]["county"] == "San Bernardino"
    assert out["truth"]["mw_total"] == 176.0
    assert out["truth"]["project_name"] is None


def test_report_names_sizing_recall_and_flags_below_floor():
    """mw_it recall must be stated explicitly, not buried in an alphabetical table:
    a 0% recall there is why 38 of 43 tonnage estimates fell back to sqft guesses."""
    from app.golden import report_text, score
    entries = [
        {"doc_key": "a", "verified": True,
         "model": {"mw_it": None, "building_sqft": 100.0, "stage": "design"},
         "truth": {"mw_it": 40.0, "building_sqft": 100.0, "stage": "design"}},
        {"doc_key": "b", "verified": True,
         "model": {"mw_it": None, "building_sqft": 200.0, "stage": "design"},
         "truth": {"mw_it": 20.0, "building_sqft": 200.0, "stage": "design"}},
    ]
    text = report_text(score(entries))
    assert "mw_it" in text and "recall 0%" in text
    assert "BELOW 50%" in text
    assert "RECALL BELOW 50% on: mw_it" in text
    assert "building_sqft" in text and "recall 100%" in text
    assert "Zero fabricated values." in text


def test_report_says_nothing_to_score_when_unverified():
    from app.golden import report_text, score
    text = report_text(score([{"doc_key": "a", "verified": False, "model": {}, "truth": None}]))
    assert "0 hand-verified" in text and "no honest way to synthesise" in text


def test_tail_fallback_is_detected_by_marker_not_empty_list():
    """The fallback records ["document_head", "tail_fallback"], so testing for an
    empty sections_found matched nothing at all."""
    from app.golden import _no_sections_matched
    from app.models import Signal

    fallback = Signal(signal_type="planning_agenda", extraction_json={
        "sections": {"chunked": True, "sections_found": ["document_head", "tail_fallback"]}})
    real = Signal(signal_type="planning_agenda", extraction_json={
        "sections": {"chunked": True, "sections_found": ["document_head", "utilities_energy"]}})
    unchunked = Signal(signal_type="planning_agenda", extraction_json={
        "sections": {"chunked": False, "sections_found": ["full_document"]}})
    assert _no_sections_matched(fallback) is True
    assert _no_sections_matched(real) is False
    assert _no_sections_matched(unchunked) is False
