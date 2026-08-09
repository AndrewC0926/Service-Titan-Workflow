"""The assumptions register: every entry must be honestly classified and
every value must be read live from config, not hand-copied. See
app/assumptions.py."""
from app.assumptions import (
    MEASURED,
    PLACEHOLDER,
    RULE_OF_THUMB,
    SOURCE_LABELS,
    SOURCE_ORDER,
    STATED,
    assumptions_by_group,
    load_assumptions,
    source_tally,
)


def test_every_assumption_has_a_valid_source_type(cfg):
    for a in load_assumptions(cfg):
        assert a.source_type in SOURCE_LABELS, f"{a.name} has unrecognized source_type {a.source_type!r}"


def test_every_assumption_has_a_non_empty_value_and_detail(cfg):
    for a in load_assumptions(cfg):
        assert a.value and a.value != "None", f"{a.name} has an empty/missing value"
        assert a.source_detail, f"{a.name} has no source_detail -- an unexplained classification is not honest"


def test_required_groups_are_present(cfg):
    """The user's explicit 'at minimum' list: score weights, days-to-bid,
    equipment value/ton, spillover weight+saturation, identity penalties,
    service life bands by ownership, sqft/ton facility bands, adjacency edges."""
    names = {a.name for a in load_assumptions(cfg)}
    required_substrings = [
        "Window multiplier",
        "Days to estimated bid",
        "Equipment value per ton",
        "Spillover weight",
        "Spillover saturation",
        "Identity penalty",
        "private commercial ownership",
        "federal ownership",
        "state municipal ownership",
        "Industrial sqft/ton",
        "Category adjacency edges",
    ]
    for needle in required_substrings:
        assert any(needle in name for name in names), f"missing required assumption: {needle!r}"


def test_state_municipal_service_life_is_flagged_as_a_placeholder_with_no_source(cfg):
    """The specific finding the user called out: interpolated with no
    source at all, and the register must say so, not dress it up as
    'estimated' the same way a real interpolation-between-two-anchors is."""
    entry = next(a for a in load_assumptions(cfg) if "state municipal ownership" in a.name)
    assert entry.source_type == PLACEHOLDER
    assert entry.verified is False
    assert "no source" in entry.source_detail.lower()


def test_federal_service_life_is_stated_not_measured(cfg):
    entry = next(a for a in load_assumptions(cfg) if "federal ownership" in a.name)
    assert entry.source_type == STATED
    assert "contractor" in entry.source_detail.lower()
    assert entry.last_reviewed is not None and "2026-08-07" in entry.last_reviewed


def test_equipment_value_per_ton_is_measured_with_mixed_verified_detail(cfg):
    """Two real anchors, everything else interpolated -- the entry must say
    both halves of that, not average them into a single false confidence
    level."""
    entry = next(a for a in load_assumptions(cfg) if "Equipment value per ton" in a.name)
    assert entry.source_type == MEASURED
    assert "VERIFIED" in entry.source_detail
    assert "interpolated" in entry.source_detail.lower()
    assert entry.verified is None  # mixed per-row, not a single answer


def test_industrial_sqft_per_ton_attributed_to_named_person(cfg):
    entry = next(a for a in load_assumptions(cfg) if "Industrial sqft/ton" in a.name)
    assert entry.source_type == STATED
    assert "Andrew" in entry.source_detail


def test_retrofit_sqft_per_ton_is_rule_of_thumb_not_conflated_with_new_construction(cfg):
    """Two genuinely different tables (new-construction sizing vs. retrofit
    tonnage estimate) must appear as two separate entries, not merged."""
    names = [a.name for a in load_assumptions(cfg)]
    assert any("new-construction sizing" in n for n in names)
    assert any("retrofit tonnage estimate" in n for n in names)
    entry = next(a for a in load_assumptions(cfg) if "retrofit tonnage estimate" in a.name)
    assert entry.source_type == RULE_OF_THUMB


def test_size_factor_formula_flagged_as_not_config_tunable(cfg):
    """The one constant that isn't even in config.yaml -- must be visible
    on the register anyway, with config_path=None disclosed as such."""
    entry = next(a for a in load_assumptions(cfg) if a.name == "Size factor formula")
    assert entry.config_path is None


def test_source_tally_sums_to_total_and_respects_order(cfg):
    assumptions = load_assumptions(cfg)
    tally = source_tally(assumptions)
    assert sum(n for _t, _label, n in tally) == len(assumptions)
    seen_order = [t for t, _label, _n in tally]
    assert seen_order == [t for t in SOURCE_ORDER if t in seen_order]


def test_source_tally_omits_zero_count_categories(cfg):
    tally = source_tally([])
    assert tally == []


def test_assumptions_by_group_covers_every_assumption(cfg):
    assumptions = load_assumptions(cfg)
    grouped = assumptions_by_group(cfg)
    assert sum(len(v) for v in grouped.values()) == len(assumptions)


def test_placeholder_is_the_honest_majority_not_hidden(cfg):
    """Not a normative assertion about what SHOULD be true -- a regression
    guard on the finding itself. If this ever flips because someone fixed
    the config, that's good news and the test should be updated; if it
    silently stops being true because a future edit reclassifies entries
    without justification, this catches it."""
    assumptions = load_assumptions(cfg)
    placeholder_n = sum(1 for a in assumptions if a.source_type == PLACEHOLDER)
    assert placeholder_n >= len(assumptions) / 2
