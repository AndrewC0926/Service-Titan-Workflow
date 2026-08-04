import pytest

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
