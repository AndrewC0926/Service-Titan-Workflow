"""An unstated building type must produce no tonnage, and value must follow type.

The board's top row read "3,240 to 162,000 tons" — an 8,100,000 sqft hospital
campus sized across the full 50-2,500 sqft/ton span because no filing said what
the building was. That is not an estimate. It is the arithmetic restating that we
do not know, wearing the costume of a number, and it is unquotable.

Two rules are pinned here:
  * unknown type -> no tonnage at all, with the reason on the row
  * a known type -> a real band, unchanged from before
plus equipment value, which corrects ranking without touching tonnage.
"""
import pytest

from app.config import load_config
from app.models import Category, FacilityType
from app.pipeline.sizing import estimate_equipment_value, estimate_tons

SQFT = 8_100_000


@pytest.fixture()
def c():
    return load_config()


def _tons(c, facility_type, sqft=SQFT):
    return estimate_tons(c, building_sqft=sqft, category=Category.industrial,
                         facility_type=facility_type)


# ---- the 50x band is gone ------------------------------------------------

def test_unknown_type_produces_no_tonnage(c):
    est = _tons(c, FacilityType.unknown)
    assert est.low is None and est.high is None
    assert est.midpoint is None


def test_unknown_type_says_what_is_missing_and_keeps_the_area(c):
    """The row has to stay actionable: one filing stating the use fixes it."""
    est = _tons(c, FacilityType.unknown)
    assert "UNKNOWN TYPE" in est.basis
    assert "8,100,000 sqft" in est.basis
    assert "not stated" in est.basis
    assert est.basis_key == "industrial_unknown_type"
    assert est.low_confidence is True


def test_the_fifty_x_band_can_no_longer_be_produced(c):
    """The specific number that started this: 3,240-162,000 tons."""
    est = _tons(c, FacilityType.unknown)
    assert est.basis is not None
    assert "3,240" not in est.basis and "162,000" not in est.basis


def test_no_unknown_row_in_the_sizing_table(c):
    """A future edit re-adding it would silently bring the 50x band back."""
    table = c.get("sizing.industrial_sqft_per_ton_by_type", {})
    assert "unknown" not in table


# ---- known types still size, and size differently ------------------------

@pytest.mark.parametrize("ftype,lo_per_ton,hi_per_ton", [
    (FacilityType.distribution_fulfillment, 1000, 2500),
    (FacilityType.warehouse_conditioned, 800, 1500),
    (FacilityType.light_manufacturing, 300, 600),
    (FacilityType.heavy_manufacturing, 100, 300),
    (FacilityType.cleanroom, 50, 150),
    (FacilityType.office_rnd, 300, 400),
])
def test_each_known_type_uses_its_own_band(c, ftype, lo_per_ton, hi_per_ton):
    est = _tons(c, ftype, sqft=1_000_000)
    assert est.low == pytest.approx(1_000_000 / hi_per_ton, rel=1e-6)
    assert est.high == pytest.approx(1_000_000 / lo_per_ton, rel=1e-6)
    assert ftype.value in est.basis


def test_type_is_the_whole_story(c):
    """Same building, two types, two completely different jobs."""
    cleanroom = _tons(c, FacilityType.cleanroom, sqft=1_000_000)
    fulfillment = _tons(c, FacilityType.distribution_fulfillment, sqft=1_000_000)
    assert cleanroom.midpoint > fulfillment.midpoint * 10


def test_known_type_bands_are_far_narrower_than_the_old_span(c):
    """The point of the exercise, as a ratio rather than a label."""
    for ftype in (FacilityType.distribution_fulfillment, FacilityType.cleanroom,
                  FacilityType.light_manufacturing):
        est = _tons(c, ftype, sqft=1_000_000)
        assert est.high / est.low <= 3.5, f"{ftype.value} band is still wide"


# ---- equipment value ------------------------------------------------------

def test_at_equal_tonnage_unit_value_decides(c):
    """The field's correction, stated at the tonnage where it is unambiguous.

    An earlier version of this test asserted that a 600 ton cleanroom outvalues a
    5,000 ton fulfillment centre. That was true of the rates I had invented (a 12x
    spread) and is false of the real line card (5.7x). The measured spread is what
    it is; the test now says so rather than the other way round.
    """
    clean = estimate_equipment_value(c, 1000, 1000, FacilityType.cleanroom)
    box = estimate_equipment_value(c, 1000, 1000, FacilityType.distribution_fulfillment)
    assert clean.low / box.low >= 5.0
    assert clean.high / box.high >= 5.0


def test_the_crossover_is_the_measured_spread_not_infinity(c):
    """How much bigger a commodity box must be to match a specialty job.

    This is the number the field input actually buys us: unit value is worth
    5.7x, so a fulfillment centre beats a cleanroom on absolute dollars once it
    is more than ~6x the tonnage — and not before. Ranking on tonnage alone got
    that wrong in both directions.
    """
    clean = estimate_equipment_value(c, 1000, 1000, FacilityType.cleanroom)

    just_under = estimate_equipment_value(c, 5000, 5000, FacilityType.distribution_fulfillment)
    assert just_under.high < clean.high, "5x the tonnage should not yet win"

    well_over = estimate_equipment_value(c, 8000, 8000, FacilityType.distribution_fulfillment)
    assert well_over.high > clean.high, "8x the tonnage should win"


def test_value_is_none_without_tonnage(c):
    assert estimate_equipment_value(c, None, None, FacilityType.cleanroom).low is None


def test_value_is_none_without_a_type(c):
    """No type means no equipment category; inventing one reintroduces the guess."""
    v = estimate_equipment_value(c, 1000, 2000, FacilityType.unknown)
    assert v.low is None and v.high is None


def test_value_basis_says_whether_the_rate_is_verified(c):
    """A line-card figure and an interpolation are not the same claim."""
    verified = estimate_equipment_value(c, 1000, 2000, FacilityType.cleanroom)
    assert "VERIFIED" in verified.basis

    estimated = estimate_equipment_value(c, 1000, 2000, FacilityType.office_rnd)
    assert "ESTIMATED rate" in estimated.basis


def test_table_is_anchored_to_the_two_real_line_card_figures(c):
    """From DMG's VP of sales, and the whole magnitude rests on them.

    VU Flow CO2 chiller ~$8,500/ton (premium specialty) and commodity LG chillers
    ~$1,500/ton, a measured 5.7x spread. Before these the table encoded only an
    ordering, with magnitudes I had invented.
    """
    table = c.get("equipment_value.value_per_ton_by_facility_type", {})
    assert table["cleanroom"]["high"] == 8500, "lost the VU Flow anchor"
    assert table["light_manufacturing"]["low"] == 1500, "lost the LG commodity anchor"

    top = max(b["high"] for b in table.values())
    bottom = min(b["low"] for b in table.values())
    assert top == 8500
    # The measured premium-to-commodity spread, reproduced across the table.
    assert 5.0 <= top / table["distribution_fulfillment"]["high"] <= 6.5

    for name, band in table.items():
        assert "source" in band, f"{name} does not say whether its rate is verified"
        assert band["low"] <= band["high"], name


def test_only_the_anchored_rows_claim_to_be_verified(c):
    """Guards against a later edit quietly promoting an estimate to a fact."""
    table = c.get("equipment_value.value_per_ton_by_facility_type", {})
    verified = {n for n, b in table.items() if "VERIFIED" in b.get("source", "")}
    assert verified == {"cleanroom", "light_manufacturing"}, verified


def test_value_scales_with_tonnage(c):
    a = estimate_equipment_value(c, 100, 200, FacilityType.light_manufacturing)
    b = estimate_equipment_value(c, 200, 400, FacilityType.light_manufacturing)
    assert b.low == pytest.approx(a.low * 2)


def test_value_ordering_matches_the_field_input(c):
    """Custom AHU and central plant above packaged rooftop, at equal tonnage."""
    order = [FacilityType.cleanroom, FacilityType.data_center,
             FacilityType.heavy_manufacturing, FacilityType.office_rnd,
             FacilityType.light_manufacturing, FacilityType.warehouse_conditioned,
             FacilityType.distribution_fulfillment]
    values = [estimate_equipment_value(c, 1000, 1000, t).low for t in order]
    assert values == sorted(values, reverse=True), dict(zip([t.value for t in order], values))
