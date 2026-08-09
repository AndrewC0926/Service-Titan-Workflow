"""Water-source risk read: capture the facts a filing states (source,
recycled/reclaimed identified, WUE, opposition), never a binary "water
contested" flag. See app/pipeline/waterrisk.py.

Covers: the pure derived-read function, extraction coercion of the new
nullable-boolean fields, and the resolve._absorb roll-up from Signal onto
Project (fill-only-if-null, sticky-true opposition, derived flag written)."""
from app.models import Category, Project, Signal, SignalType, Stage, TriageResult
from app.pipeline.resolve import _absorb
from app.pipeline.waterrisk import RISK_ELEVATED, RISK_NOISE, water_risk_read
from app.schemas import coerce_extraction


def _sig(**kw):
    base = dict(signal_type=SignalType.ceqa_nop, triage_result=TriageResult.relevant,
               category=Category.data_center, stage=Stage.entitlement)
    return Signal(**{**base, **kw})


def _proj(**kw):
    base = dict(category=Category.data_center, stage=Stage.entitlement, status="active")
    return Project(**{**base, **kw})


# --- water_risk_read: pure function -----------------------------------------


def test_no_opposition_is_no_read_regardless_of_reclaimed_status():
    for reclaimed in (None, True, False):
        flag, basis = water_risk_read(reclaimed_identified=reclaimed, opposition_stated=None)
        assert flag is None and basis is None
        flag, basis = water_risk_read(reclaimed_identified=reclaimed, opposition_stated=False)
        assert flag is None and basis is None


def test_opposition_with_no_reclaimed_access_is_elevated():
    flag, basis = water_risk_read(reclaimed_identified=None, opposition_stated=True)
    assert flag == RISK_ELEVATED
    assert "no recycled/reclaimed supply" in basis.lower() or "no rec" in basis.lower()

    flag, basis = water_risk_read(reclaimed_identified=False, opposition_stated=True)
    assert flag == RISK_ELEVATED


def test_opposition_with_reclaimed_access_is_noise_not_elevated():
    flag, basis = water_risk_read(reclaimed_identified=True, opposition_stated=True)
    assert flag == RISK_NOISE
    assert "potable" in basis.lower()


def test_never_rank_language_present_in_elevated_basis():
    """Not a load-bearing assertion about behavior (nothing here touches
    score) -- guards that the basis text itself keeps disclaiming it, since
    that's the string a human actually reads before deciding to trust it."""
    _, basis = water_risk_read(reclaimed_identified=False, opposition_stated=True)
    assert "not a" in basis.lower() or "never" in basis.lower()


# --- coerce_extraction: nullable booleans -----------------------------------


def test_coerce_extraction_keeps_real_booleans():
    out = coerce_extraction({
        "water_reclaimed_identified": True, "water_opposition_stated": False,
        "summary_one_line": "x", "confidence": 0.5,
    })
    assert out["water_reclaimed_identified"] is True
    assert out["water_opposition_stated"] is False


def test_coerce_extraction_nulls_unstated_booleans():
    out = coerce_extraction({"summary_one_line": "x", "confidence": 0.5})
    assert out["water_reclaimed_identified"] is None
    assert out["water_opposition_stated"] is None


def test_coerce_extraction_rejects_non_boolean_junk_rather_than_coercing():
    """A model returning a stray string/number for a boolean field must null
    out, not get truthy-coerced -- "true"-the-string and 1-the-number are not
    the same claim as the JSON boolean true."""
    out = coerce_extraction({
        "water_reclaimed_identified": "yes", "water_opposition_stated": 1,
        "summary_one_line": "x", "confidence": 0.5,
    })
    assert out["water_reclaimed_identified"] is None
    assert out["water_opposition_stated"] is None


def test_coerce_extraction_keeps_water_text_fields():
    out = coerce_extraction({
        "water_source_stated": "Recycled water from West Basin MWD",
        "water_use_efficiency_stated": "0.20 L/kWh WUE",
        "summary_one_line": "x", "confidence": 0.5,
    })
    assert out["water_source_stated"] == "Recycled water from West Basin MWD"
    assert out["water_use_efficiency_stated"] == "0.20 L/kWh WUE"


# --- resolve._absorb: signal -> project roll-up ------------------------------


def test_absorb_fills_water_source_and_wue_from_signal():
    project = _proj()
    signal = _sig(water_source_stated="On-site groundwater wells",
                  water_use_efficiency_stated="1.1 MGD")
    _absorb(project, signal)
    assert project.water_source_stated == "On-site groundwater wells"
    assert project.water_use_efficiency_stated == "1.1 MGD"


def test_absorb_never_overwrites_known_water_source_with_null():
    project = _proj(water_source_stated="Recycled water from West Basin MWD")
    signal = _sig(water_source_stated=None)  # a later doc that doesn't mention it
    _absorb(project, signal)
    assert project.water_source_stated == "Recycled water from West Basin MWD"


def test_absorb_fills_reclaimed_identified_only_once():
    project = _proj()
    _absorb(project, _sig(water_reclaimed_identified=True))
    assert project.water_reclaimed_identified is True
    # A later signal stating the opposite must not flip an already-known fact.
    _absorb(project, _sig(water_reclaimed_identified=False))
    assert project.water_reclaimed_identified is True


def test_absorb_opposition_is_sticky_true():
    project = _proj()
    _absorb(project, _sig(water_opposition_stated=True))
    assert project.water_opposition_stated is True
    # A later signal that doesn't mention opposition must not clear a
    # documented objection -- silence is weaker evidence than a record.
    _absorb(project, _sig(water_opposition_stated=None))
    assert project.water_opposition_stated is True


def test_absorb_opposition_false_only_fills_when_nothing_known():
    project = _proj()
    _absorb(project, _sig(water_opposition_stated=False))
    assert project.water_opposition_stated is False


def test_absorb_computes_and_stores_the_derived_risk_flag():
    project = _proj()
    _absorb(project, _sig(water_reclaimed_identified=False, water_opposition_stated=True))
    assert project.water_risk_flag == RISK_ELEVATED
    assert project.water_risk_basis

    project2 = _proj()
    _absorb(project2, _sig(water_reclaimed_identified=True, water_opposition_stated=True))
    assert project2.water_risk_flag == RISK_NOISE


def test_absorb_quiet_project_has_no_risk_flag():
    project = _proj()
    _absorb(project, _sig(water_source_stated="Municipal potable supply"))
    assert project.water_risk_flag is None
    assert project.water_risk_basis is None
