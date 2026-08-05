import pytest

from app.models import Category
from app.pipeline.sizing import estimate_tons, genset_mw_to_it_mw, hp_to_kw


def test_vantage_nv1_sanity(cfg):
    """224 MW IT: midpoint 224*325 = 72,800 tons; band inside the 60-92k sanity range."""
    est = estimate_tons(cfg, mw_it=224)
    assert est.low is not None and est.high is not None
    mid = 224 * 325
    assert est.low < mid < est.high
    assert 60_000 <= est.low <= 72_800
    assert 72_800 <= est.high <= 92_000
    assert "224" in est.basis
    assert est.basis_key == "stated_it"
    assert not est.low_confidence


def test_hp_conversion(cfg):
    assert abs(hp_to_kw(1000, cfg) - 745.7) < 0.01


def test_genset_derating(cfg):
    assert abs(genset_mw_to_it_mw(140, cfg) - 100.0) < 0.01


def test_band_width_derives_from_basis(cfg):
    """The three defect: bands must widen as the input gets more indirect,
    never a fixed low/high ratio across bases."""
    stated = estimate_tons(cfg, mw_it=100)
    total = estimate_tons(cfg, mw_total=140)
    gensets = estimate_tons(cfg, generator_count=40, generator_hp_each=3000)
    sqft = estimate_tons(cfg, building_sqft=1_000_000)

    def rel_width(e):
        return (e.high - e.low) / e.midpoint

    assert rel_width(stated) < rel_width(total) < rel_width(gensets) < rel_width(sqft)
    # And the old fixed 0.75 low/high ratio is gone:
    ratios = {round(e.low / e.high, 3) for e in (stated, total, gensets, sqft)}
    assert len(ratios) == 4


@pytest.mark.parametrize("kwargs,expected_key", [
    ({"mw_it": 100}, "stated_it"),
    ({"mw_total": 140}, "stated_total"),
    ({"generator_count": 40, "generator_hp_each": 3000}, "gensets"),
    ({"building_sqft": 500_000}, "sqft"),
])
def test_each_basis_path(cfg, kwargs, expected_key):
    est = estimate_tons(cfg, **kwargs)
    assert est.basis_key == expected_key
    assert est.low is not None and est.low < est.high
    assert est.low_confidence == (expected_key == "sqft")


def test_generator_fleet_math(cfg):
    # 40 x 3,000 HP = 89.5 MW standby -> ~63.9 MW IT
    est = estimate_tons(cfg, generator_count=40, generator_hp_each=3000)
    assert est.mw_it is not None and 60 < est.mw_it < 68
    assert "gensets" in est.basis


def test_kw_beats_hp_when_both(cfg):
    est = estimate_tons(cfg, generator_count=10, generator_hp_each=3000, generator_kw_each=2500)
    assert "2500 kW" in est.basis


def test_sqft_flagged_low_confidence(cfg):
    est = estimate_tons(cfg, building_sqft=500_000)
    assert est.low_confidence
    assert "LOW CONFIDENCE" in est.basis


def test_stated_it_beats_everything(cfg):
    est = estimate_tons(cfg, mw_it=50, mw_total=200, generator_count=100, generator_hp_each=4000)
    assert est.mw_it == 50
    assert est.basis_key == "stated_it"


def test_no_input_returns_nulls(cfg):
    est = estimate_tons(cfg)
    assert est.low is None and est.high is None and est.basis is None
    assert est.midpoint is None


# --- category-aware sizing and the implausible-MW guard -------------------


def test_industrial_sized_from_floor_area_not_it_watts(cfg):
    """A 1,000,000 sqft warehouse is ~1,000-2,900 tons, not the ~24,000 that
    data-center watts-per-sqft would produce. Getting this wrong would put every
    industrial row above every real data center."""
    est = estimate_tons(cfg, building_sqft=1_000_000, category=Category.industrial)
    assert est.basis_key == "industrial_sqft"
    assert est.low_confidence is True
    assert 500 <= est.low <= 1_500
    assert 2_000 <= est.high <= 3_500
    dc = estimate_tons(cfg, building_sqft=1_000_000, category=Category.data_center)
    assert dc.high > est.high * 5  # the two paths must not be comparable


def test_industrial_ignores_stated_mw(cfg):
    """Industrial load is envelope + ventilation. A stated MW is a service size or
    a process rating, and must not drive tonnage."""
    est = estimate_tons(cfg, mw_total=500, building_sqft=73_000,
                        category=Category.industrial)
    assert est.basis_key == "industrial_sqft"
    assert est.high < 1_000


def test_industrial_with_no_area_is_unknown_not_guessed(cfg):
    est = estimate_tons(cfg, mw_total=10, category=Category.industrial)
    assert est.low is None and est.high is None


def test_amperesand_implausible_stated_mw_is_rejected(cfg):
    """The regression this guard exists for: 500 MW stated on a 73,000 sqft
    transformer factory is 6,849 W/sqft. Trusted, it became 357 MW IT and 136,964
    tons — the largest number on the board — and escaped every low-confidence flag
    precisely because it was 'stated'."""
    est = estimate_tons(cfg, mw_total=500, building_sqft=73_000,
                        category=Category.data_center)
    assert est.basis_key == "sqft"            # fell back off the stated figure
    assert est.low_confidence is True
    assert est.rejected_inputs and "implausible" in est.rejected_inputs[0]
    assert "6,849 W/sqft" in est.rejected_inputs[0]
    assert est.high < 5_000                   # nowhere near 136,964


def test_plausible_stated_mw_still_wins(cfg):
    """The guard must not fire on real data centers: 40 MW in 200,000 sqft is
    200 W/sqft, squarely normal for dense white space."""
    est = estimate_tons(cfg, mw_total=40, building_sqft=200_000,
                        category=Category.data_center)
    assert est.basis_key == "stated_total"
    assert est.rejected_inputs is None


def test_stated_mw_with_no_area_is_trusted(cfg):
    """No square footage means nothing to sanity-check against; the guard must not
    reject a figure it cannot evaluate."""
    est = estimate_tons(cfg, mw_total=500, category=Category.data_center)
    assert est.basis_key == "stated_total"
    assert est.rejected_inputs is None


def test_rejected_mw_falls_back_to_sqft_and_says_so(cfg):
    """Rejecting the stated figure must not silently produce a clean-looking sqft
    estimate: the discard is named in the basis so the fallback can never be
    mistaken for a first-choice number."""
    est = estimate_tons(cfg, mw_it=900, building_sqft=1_000,
                        category=Category.data_center)
    assert est.basis_key == "sqft"
    assert est.low_confidence is True
    assert "900,000 W/sqft" in est.basis and "fell back to" in est.basis
