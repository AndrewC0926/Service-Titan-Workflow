"""Regulatory trigger engine. See app/pipeline/regulatory.py and
docs/CHARTER.md invariant 13 (unverified entries never reach customer-facing
output)."""
from datetime import date

from app.pipeline.regulatory import (
    customer_facing_triggers, infer_refrigerant, parse_equipment_from_work_desc,
    sb1206_status,
)


def test_all_shipped_triggers_are_verified(cfg):
    """The real config.yaml -- every entry that exists must be verified, or
    it should not have been added at all."""
    all_triggers = cfg.get("regulatory_triggers", {})
    assert all_triggers, "expected the regulatory_triggers block to be populated"
    visible = customer_facing_triggers(cfg)
    assert visible == all_triggers, (
        "an unverified entry is sitting in config.yaml — either verify it or remove it")


def test_gas_furnace_ban_is_not_a_trigger(cfg):
    """Explicit instruction: neither the CARB nor SCAQMD rulemaking is
    adopted, so it must not appear as a trigger at all (not even flagged
    'proposed') -- see docs/CHARTER.md's watch-items section."""
    triggers = cfg.get("regulatory_triggers", {})
    for key, entry in triggers.items():
        assert "furnace" not in key.lower()
        assert "furnace" not in entry.get("name", "").lower()


def test_unverified_entry_is_excluded_not_caveated(cfg):
    fake_cfg_data = dict(cfg.data)
    fake_cfg_data["regulatory_triggers"] = {
        **cfg.get("regulatory_triggers", {}),
        "made_up_rule": {"name": "Not Real", "verification": {"status": "needs_review"}},
    }
    from app.config import Config
    fake_cfg = Config(fake_cfg_data)
    visible = customer_facing_triggers(fake_cfg)
    assert "made_up_rule" not in visible


def test_infer_refrigerant_r410a_window(cfg):
    assert infer_refrigerant(2015)["refrigerant"] == "R-410A"
    assert infer_refrigerant(2010)["refrigerant"] == "R-410A"
    assert infer_refrigerant(2024)["refrigerant"] == "R-410A"


def test_infer_refrigerant_outside_window_is_null_not_guessed(cfg):
    assert infer_refrigerant(2005)["refrigerant"] is None
    assert infer_refrigerant(2025)["refrigerant"] is None
    assert infer_refrigerant(None)["refrigerant"] is None


def test_sb1206_fires_for_r410a_install_year(cfg):
    result = sb1206_status(cfg, install_year=2018, as_of=date(2031, 1, 1))
    assert result is not None
    assert result["trigger"] == "sb_1206_hfc_phasedown"
    assert result["status"] == "in_effect"
    assert result["gwp_max"] == 1500


def test_sb1206_upcoming_before_effective_date(cfg):
    result = sb1206_status(cfg, install_year=2018, as_of=date(2026, 1, 1))
    assert result is not None
    assert result["status"] == "upcoming"
    assert result["gwp_max"] == 1500
    assert result["effective_date"] == "2030-01-01"


def test_sb1206_none_when_refrigerant_not_inferrable(cfg):
    assert sb1206_status(cfg, install_year=2005) is None
    assert sb1206_status(cfg, install_year=None) is None


def test_sb1206_none_before_2200_tier_binds(cfg):
    """As of any date before Jan 1 2025, the first tier (2,200 GWP) hasn't
    bound yet either, but R-410A (2,088 GWP) is already under that ceiling
    the moment it applies -- so no tier ever actually restricts R-410A until
    the 1,500 GWP tier in 2030. Confirms the engine reads GWP correctly
    rather than firing on any tier's existence."""
    result = sb1206_status(cfg, install_year=2015, as_of=date(2025, 6, 1))
    # 2,200 GWP tier is active; R-410A (2,088) is BELOW that ceiling, so it
    # isn't restricted by the active tier -- only by the upcoming 1,500 tier.
    assert result["status"] == "upcoming"
    assert result["gwp_max"] == 1500


def test_parse_equipment_real_permit_samples():
    cases = [
        ("REPLACE (2) 6-ton HEAT PUMP PACKAGE UNIT - ROOF", 2, 6.0),
        ("install 4 new RTUs", 4, None),
        ("HVAC-4 TON REFRIERATION SYSTEMS FOR WALK IN COOLER/FREEZER 4-COMPRESSORS", None, 4.0),
        ("2 RTUs, 1 Walk-in cooler", 2, None),
        ("Like-for-like swap out of TWO 5-ton units. No alterations will be made.", 2, 5.0),
        ("REMOVE AND REPLACE (2) 30-TON ROOFTOP PACKAGE UNITS WITH (2) 30-TON PA", 2, 30.0),
    ]
    for text, count, tons in cases:
        result = parse_equipment_from_work_desc(text)
        assert result["count"] == count, text
        assert result["tons_each"] == tons, text


def test_parse_equipment_no_match_stays_null():
    result = parse_equipment_from_work_desc("Electrical panel upgrade, 200A service")
    assert result == {"count": None, "tons_each": None}


def test_parse_equipment_empty_string():
    assert parse_equipment_from_work_desc("") == {"count": None, "tons_each": None}
    assert parse_equipment_from_work_desc(None) == {"count": None, "tons_each": None}
