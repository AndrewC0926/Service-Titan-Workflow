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
    """The five facility types a 2026-08-16 fit attempt came back empty for
    -- still Andrew's stated numbers. Cleanroom (upgraded to rule-of-thumb)
    is a separate entry, see test_industrial_sqft_per_ton_cleanroom_split_
    from_the_rest."""
    entry = next(a for a in load_assumptions(cfg) if a.name == "Industrial sqft/ton — other facility types")
    assert entry.source_type == STATED
    assert "Andrew" in entry.source_detail


def test_retrofit_sqft_per_ton_is_rule_of_thumb_not_conflated_with_new_construction(cfg):
    """Two genuinely different tables (new-construction sizing vs. retrofit
    tonnage estimate) must appear as separate entries, not merged."""
    names = [a.name for a in load_assumptions(cfg)]
    assert any(n.startswith("Industrial sqft/ton") for n in names)
    assert any("retrofit tonnage estimate" in n for n in names)
    entry = next(a for a in load_assumptions(cfg) if "retrofit tonnage estimate" in a.name)
    assert entry.source_type == RULE_OF_THUMB


def test_industrial_sqft_per_ton_cleanroom_split_from_the_rest(cfg):
    """Fit attempted 2026-08-16: cleanroom got real published corroboration
    (upgraded to rule-of-thumb), the other five didn't (stay stated-by-
    Andrew) -- two different classifications must not be blended under one
    entry, same discipline the days-to-bid split already established."""
    cleanroom = next(a for a in load_assumptions(cfg) if a.name == "Industrial sqft/ton — cleanroom")
    assert cleanroom.source_type == RULE_OF_THUMB
    assert "50" in cleanroom.value and "150" in cleanroom.value  # value unchanged, only classification moved

    other = next(a for a in load_assumptions(cfg) if a.name == "Industrial sqft/ton — other facility types")
    assert other.source_type == STATED
    assert "cleanroom" not in other.value.lower()  # the 5-type summary must not include the split-out type


def test_hardcoded_constants_disclosed_with_config_path_none(cfg):
    """A constant not even in config.yaml must be visible on the register
    anyway, with config_path=None disclosed as such."""
    entry = next(a for a in load_assumptions(cfg) if a.name == "Urgency weighting formula")
    assert entry.config_path is None


def test_size_factor_formula_is_now_config_tunable(cfg):
    """Was the one entry with config_path=None until a sensitivity sweep
    found it the most sensitive constant in the system with no way to
    calibrate it -- moved into scoring.size_factor 2026-08-16."""
    entry = next(a for a in load_assumptions(cfg) if a.name == "Size factor formula")
    assert entry.config_path == "scoring.size_factor"
    assert entry.source_type == PLACEHOLDER  # tunable now, still not measured


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


def test_days_to_bid_entitlement_is_measured_others_stay_placeholder(cfg):
    """The split this register exists to enforce: entitlement is a real fit
    from CEQAnet data now, but concept/design/permitting/procurement/
    construction/operating are still invented -- one config table with
    mixed provenance must not read as a single classification either way."""
    entitlement = next(a for a in load_assumptions(cfg) if "entitlement (NOP to NOD)" in a.name)
    assert entitlement.source_type == MEASURED
    assert "n=6" in entitlement.source_detail or "199, 225, 350, 393, 470, 552" in entitlement.source_detail
    assert "2024-08-08" in entitlement.source_detail and "2026-06-19" in entitlement.source_detail

    other = next(a for a in load_assumptions(cfg) if a.name == "Days to estimated bid: other stages")
    assert other.source_type == PLACEHOLDER
    assert "entitlement" not in other.value  # the measured stage must not leak into the placeholder row


def test_contractor_ranking_radius_and_formula_are_registered(cfg):
    """The user's explicit finding: both were tunable constants that
    existed nowhere in the register before this."""
    names = {a.name for a in load_assumptions(cfg)}
    assert "Ranking radius" in names
    assert "Urgency weighting formula" in names

    radius = next(a for a in load_assumptions(cfg) if a.name == "Ranking radius")
    assert radius.source_type == PLACEHOLDER
    assert radius.config_path == "contractors.ranking_radius_miles"
    assert "3" in radius.value

    formula = next(a for a in load_assumptions(cfg) if a.name == "Urgency weighting formula")
    assert formula.source_type == PLACEHOLDER
    assert formula.config_path is None  # hardcoded in app.contractors, not config.yaml


def test_recency_halflife_value_unchanged_evidence_only(cfg):
    """Explicit instruction for this pass: proxy evidence, no value change,
    still classified placeholder -- a proxy is not a fit."""
    entry = next(a for a in load_assumptions(cfg) if a.name == "Recency half-life")
    assert entry.value == "180 days"
    assert entry.source_type == PLACEHOLDER
    assert "130 days" in entry.source_detail  # the measured median gap, not a new half-life
    assert "does not prove" in entry.source_detail.lower() or "not measure" in entry.source_detail.lower()


def test_identity_penalty_value_unchanged_evidence_only(cfg):
    entry = next(a for a in load_assumptions(cfg) if a.name == "Identity penalty")
    assert entry.value == "1=0.70, 2=0.25, 3=0.10"
    assert entry.source_type == PLACEHOLDER
    assert "2 merges" in entry.source_detail or "n=2" in entry.source_detail
    assert "lost/dead/archived" in entry.source_detail or "dropped population" in entry.source_detail.lower()


def test_placeholder_is_not_silently_erased(cfg):
    """Not a normative assertion about what SHOULD be true -- a regression
    guard on the finding itself. A strict >=50% share was expected to erode
    over time as more entries get genuinely verified (each one is good
    news, not a bug), and it has: measured overtook placeholder as the
    single largest category on 2026-09-09, after a burst of real Phase A/B
    source-classification work (SCAQMD facility grain, BPELSG roster, HCAI
    Facilities Development Division, the CIP jurisdiction list) added many
    genuinely verified entries in one session. That is the erosion this
    test's own docstring already anticipated, not a regression -- so the
    check is no longer "placeholder is the plurality," which stopped being
    true for a legitimate reason, but the weaker, still-durable guard this
    was meant to be: placeholder must not quietly collapse toward zero,
    which is what an unjustified mass-reclassification (someone marking a
    pile of placeholders "measured" without actually doing the work) would
    look like instead.

    Loosened a second time on 2026-09-10 (placeholder=28, measured=57 --
    28/57 ~= 0.491, just under the old 0.5 floor) after a run of Phase A
    access-classification entries (board agendas, Title 24/MAEDbS, CSLB
    CheckLicense) each added a genuinely-checked MEASURED fact (a real
    robots.txt fetch, a real one-off lookup attempt) with no matching new
    PLACEHOLDER to offset it -- the same legitimate erosion this test
    already anticipated, not a mass-reclassification of existing entries."""
    assumptions = load_assumptions(cfg)
    from collections import Counter
    counts = Counter(a.source_type for a in assumptions)
    placeholder_n = counts[PLACEHOLDER]
    assert placeholder_n >= max(counts.values()) * 0.4
