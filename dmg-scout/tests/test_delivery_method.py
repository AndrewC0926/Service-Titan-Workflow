"""Project delivery method LLM hint: extraction coercion, and the
resolve._absorb roll-up from Signal onto Project.delivery_method_llm_hint
(fill-only-if-null, same discipline as water_source_stated -- see
test_water_risk.py). Display only, as of Build Plan v2.1 Block 2's WS3.1
decision -- see Project.delivery_method_llm_hint's docstring in
app/models.py. For the field a RULE may actually read, see
tests/test_procurement_delivery.py and tests/test_call_target.py."""
from app.delivery import DELIVERY_METHOD_ABBR, DELIVERY_METHOD_LABELS, DELIVERY_METHOD_NOTES
from app.models import Category, Project, Signal, SignalType, Stage, TriageResult
from app.pipeline.resolve import _absorb
from app.schemas import coerce_extraction


def _sig(**kw):
    base = dict(signal_type=SignalType.ceqa_nop, triage_result=TriageResult.relevant,
               category=Category.data_center, stage=Stage.entitlement)
    return Signal(**{**base, **kw})


def _proj(**kw):
    base = dict(category=Category.data_center, stage=Stage.entitlement, status="active")
    return Project(**{**base, **kw})


def test_coerce_extraction_keeps_delivery_method_text():
    out = coerce_extraction({"delivery_method": "design_build"})
    assert out["delivery_method"] == "design_build"


def test_coerce_extraction_nulls_unstated_delivery_method():
    out = coerce_extraction({})
    assert out["delivery_method"] is None


def test_absorb_fills_delivery_method_from_signal(db_session):
    project = _proj()
    _absorb(db_session, project, _sig(delivery_method="design_bid_build"))
    assert project.delivery_method_llm_hint == "design_bid_build"


def test_absorb_never_overwrites_known_delivery_method_with_null(db_session):
    project = _proj(delivery_method_llm_hint="cm_at_risk")
    _absorb(db_session, project, _sig(delivery_method=None))  # a later doc that doesn't mention it
    assert project.delivery_method_llm_hint == "cm_at_risk"


def test_absorb_never_overwrites_known_delivery_method_with_a_different_one(db_session):
    # First-stated-value wins -- a second, conflicting filing must not
    # silently flip the field.
    project = _proj(delivery_method_llm_hint="design_build")
    _absorb(db_session, project, _sig(delivery_method="design_bid_build"))
    assert project.delivery_method_llm_hint == "design_build"


def test_every_delivery_method_has_a_label_abbr_and_note():
    for key in ("design_bid_build", "design_build", "design_assist",
               "cm_at_risk", "progressive_design_build"):
        assert key in DELIVERY_METHOD_LABELS
        assert key in DELIVERY_METHOD_ABBR
        assert key in DELIVERY_METHOD_NOTES
        assert DELIVERY_METHOD_NOTES[key]  # non-empty
