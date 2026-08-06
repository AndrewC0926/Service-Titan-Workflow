import pytest

from app.models import Category, FacilityType
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
    """A 1,000,000 sqft conditioned warehouse is ~667-1,250 tons, not the ~24,000
    that data-center watts-per-sqft would produce."""
    est = estimate_tons(cfg, building_sqft=1_000_000, category=Category.industrial,
                        facility_type=FacilityType.warehouse_conditioned)
    assert est.basis_key == "industrial_sqft"
    assert est.low_confidence is True
    assert 600 <= est.low <= 700
    assert 1_200 <= est.high <= 1_300
    dc = estimate_tons(cfg, building_sqft=1_000_000, category=Category.data_center)
    assert dc.high > est.high * 5  # the two paths must not be comparable


def test_industrial_ignores_stated_mw(cfg):
    """Industrial load is envelope + ventilation. A stated MW is a service size or
    a process rating, and must not drive tonnage."""
    est = estimate_tons(cfg, mw_total=500, building_sqft=73_000,
                        category=Category.industrial,
                        facility_type=FacilityType.light_manufacturing)
    assert est.basis_key == "industrial_sqft"
    assert est.high < 1_000          # 73,000/300 = 243, nowhere near 500 MW of load


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


# --- facility type drives the industrial band ------------------------------


def test_crocs_fulfillment_centre_is_not_sized_like_a_factory(cfg):
    """A 1,000,000 sqft fulfillment centre at 1,000-2,500 sqft/ton is 400-1,000
    tons. Sized on one shared industrial band it read 1,000-2,857, and under
    data-center watts it read 13,406-35,344."""
    est = estimate_tons(cfg, building_sqft=1_000_000, category=Category.industrial,
                        facility_type=FacilityType.distribution_fulfillment)
    assert 380 <= est.low <= 420
    assert 950 <= est.high <= 1050
    assert "distribution_fulfillment" in est.basis
    assert est.low_confidence is True


@pytest.mark.parametrize("ftype,lo,hi", [
    (FacilityType.distribution_fulfillment, 40, 100),
    (FacilityType.warehouse_conditioned, 67, 125),
    (FacilityType.light_manufacturing, 167, 333),
    (FacilityType.heavy_manufacturing, 333, 1000),
    (FacilityType.cleanroom, 667, 2000),
    (FacilityType.office_rnd, 250, 333),
])
def test_each_facility_type_uses_its_own_band(cfg, ftype, lo, hi):
    """100,000 sqft, one type at a time. Cleanroom must be ~20x a fulfillment
    centre of identical area — that spread is the whole reason the field exists."""
    est = estimate_tons(cfg, building_sqft=100_000, category=Category.industrial,
                        facility_type=ftype)
    assert round(est.low) == lo and round(est.high) == hi


def test_unknown_type_produces_no_tonnage_at_all(cfg):
    """Superseded the old "full span" behaviour, deliberately.

    This used to assert 40-2,000 tons for 100,000 sqft: the whole 50x span,
    offered as an estimate. On an 8,100,000 sqft campus that rendered as
    "3,240 to 162,000 tons" at the top of the board, which is not a measurement
    of anything — it is the arithmetic restating that nobody said what the
    building was. A row that cannot be quoted should say so.
    """
    est = estimate_tons(cfg, building_sqft=100_000, category=Category.industrial,
                        facility_type=FacilityType.unknown)
    assert est.low is None and est.high is None
    assert est.basis_key == "industrial_unknown_type"
    assert "UNKNOWN TYPE" in est.basis
    # The area survives, so the row stays actionable rather than blank.
    assert "100,000 sqft" in est.basis
    assert est.low_confidence is True

    # Every typed band still produces a real number for the same area.
    for ftype in (FacilityType.distribution_fulfillment, FacilityType.cleanroom,
                  FacilityType.heavy_manufacturing, FacilityType.office_rnd):
        typed = estimate_tons(cfg, building_sqft=100_000, category=Category.industrial,
                              facility_type=ftype)
        assert typed.low is not None and typed.high is not None


def test_facility_type_does_not_affect_data_centers(cfg):
    """Data centers size off IT load, not the industrial sqft/ton table."""
    a = estimate_tons(cfg, mw_it=40, category=Category.data_center,
                      facility_type=FacilityType.cleanroom)
    b = estimate_tons(cfg, mw_it=40, category=Category.data_center,
                      facility_type=FacilityType.unknown)
    assert a.low == b.low and a.high == b.high and a.basis_key == "stated_it"


def test_not_knowing_the_type_cannot_pay(cfg):
    """The Elsinore Heights regression, now closed by construction.

    With an arithmetic midpoint the unknown band centred on 5,661 tons — denser
    than a cleanroom — so Elsinore Heights leapt from 0.396 to 0.673 and took the
    top of the industrial board purely because nobody stated its type. The
    geometric mean fixed the inflation; removing the band removes the failure
    mode entirely. An unstated type now has no midpoint, so it cannot contribute
    a size factor to the score at all, let alone a winning one.
    """
    area = 555_060
    unknown = estimate_tons(cfg, building_sqft=area, category=Category.industrial,
                            facility_type=FacilityType.unknown)
    assert unknown.midpoint is None, "an unstated type can still influence ranking"

    typed = {ft: estimate_tons(cfg, building_sqft=area, category=Category.industrial,
                               facility_type=ft)
             for ft in (FacilityType.distribution_fulfillment,
                        FacilityType.warehouse_conditioned,
                        FacilityType.light_manufacturing,
                        FacilityType.heavy_manufacturing,
                        FacilityType.cleanroom,
                        FacilityType.office_rnd)}
    # Every stated type still yields a usable centre — only the unstated one does not.
    for e in typed.values():
        assert e.midpoint is not None and e.midpoint > 0


def test_industrial_facility_type_of_data_center_is_a_contradiction(cfg):
    """Triage already ruled this is not a computing facility. Fall to unknown
    deliberately rather than by a dict miss — and unknown now means no tonnage."""
    est = estimate_tons(cfg, building_sqft=555_060, category=Category.industrial,
                        facility_type=FacilityType.data_center)
    same = estimate_tons(cfg, building_sqft=555_060, category=Category.industrial,
                         facility_type=FacilityType.unknown)
    assert est.low == same.low and est.high == same.high
    assert est.low is None
    assert "UNKNOWN TYPE" in est.basis


def test_geometric_midpoint_barely_moves_narrow_bands(cfg):
    """The change must not quietly reprice every data center: a stated-IT band is
    +/-10%, where geometric and arithmetic agree to well under a percent."""
    est = estimate_tons(cfg, mw_it=100)
    arithmetic = (est.low + est.high) / 2
    assert abs(est.midpoint - arithmetic) / arithmetic < 0.01
