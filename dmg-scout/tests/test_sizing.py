from app.pipeline.sizing import estimate_tons, genset_mw_to_it_mw, hp_to_kw


def test_vantage_nv1_sanity(cfg):
    """224 MW IT should land roughly 67,000-90,000 tons installed."""
    est = estimate_tons(cfg, mw_it=224)
    assert est.low is not None and est.high is not None
    assert 65_000 <= est.low <= 70_000, est.low
    assert 85_000 <= est.high <= 92_000, est.high
    assert "224" in est.basis


def test_one_mw_it_band(cfg):
    est = estimate_tons(cfg, mw_it=1)
    assert est.low == 300 and est.high == 400


def test_hp_conversion(cfg):
    assert abs(hp_to_kw(1000, cfg) - 745.7) < 0.01


def test_genset_derating(cfg):
    assert abs(genset_mw_to_it_mw(140, cfg) - 100.0) < 0.01


def test_generator_fleet_path(cfg):
    # 40 x 3,000 HP gensets = 89.5 MW standby -> ~63.9 MW IT -> ~19-26k tons
    est = estimate_tons(cfg, generator_count=40, generator_hp_each=3000)
    assert est.mw_it is not None and 60 < est.mw_it < 68
    assert 18_000 < est.low < 21_000
    assert "gensets" in est.basis


def test_kw_beats_hp_when_both(cfg):
    est = estimate_tons(cfg, generator_count=10, generator_hp_each=3000, generator_kw_each=2500)
    # kW value should win: 10 x 2.5 MW = 25 MW standby
    assert "2500 kW" in est.basis


def test_sqft_path_flagged_low_confidence(cfg):
    est = estimate_tons(cfg, building_sqft=500_000)
    assert est.low_confidence
    assert est.low is not None
    assert "LOW CONFIDENCE" in est.basis


def test_stated_it_beats_everything(cfg):
    est = estimate_tons(cfg, mw_it=50, mw_total=200, generator_count=100, generator_hp_each=4000)
    assert est.mw_it == 50
    assert est.basis.startswith("stated IT load")


def test_no_input_returns_nulls(cfg):
    est = estimate_tons(cfg)
    assert est.low is None and est.high is None and est.basis is None
    assert est.midpoint is None
