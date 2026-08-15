from datetime import timedelta

from app.models import SignalType, Stage, Window, utcnow
from app.pipeline.scoring import (
    certainty,
    certainty_detail,
    classify_window,
    days_to_estimated_bid,
    days_to_estimated_bid_range,
    identity_factor,
    priority_score,
    recency_decay,
    size_factor,
)


def test_the_law_500mw_bid_ranks_below_40mw_nop(cfg):
    """Non-negotiable: a 500 MW project out to bid must rank below a 40 MW NOP."""
    now = utcnow()
    big_late = priority_score(
        cfg,
        [SignalType.bid_invite, SignalType.air_permit_atc, SignalType.ceqa_deir],
        Window.POST_BOD,
        tons_midpoint=500 * 325,
        last_signal_at=now,
        now=now,
    )
    small_early = priority_score(
        cfg,
        [SignalType.ceqa_nop],
        Window.PRE_BOD,
        tons_midpoint=40 * 325,
        last_signal_at=now,
        now=now,
    )
    assert small_early > big_late, (small_early, big_late)


def test_corroboration_beats_single_signal(cfg):
    single = certainty(cfg, [SignalType.ceqa_nop])
    corroborated = certainty(cfg, [SignalType.ceqa_nop, SignalType.abatement_application])
    assert corroborated > single
    # Duplicate same-type signals do NOT count as corroboration
    dup = certainty(cfg, [SignalType.ceqa_nop, SignalType.ceqa_nop])
    assert dup == single


def test_certainty_capped_at_one(cfg):
    many = [SignalType.air_permit_atc, SignalType.faa_7460, SignalType.ceqa_nop,
            SignalType.abatement_application, SignalType.water_will_serve]
    assert certainty(cfg, many) == 1.0


def test_speculative_utility_request_scores_low(cfg):
    assert certainty(cfg, [SignalType.utility_load_request]) < 0.3


def test_window_multipliers_order(cfg):
    m = cfg.get("scoring.window_multipliers")
    assert m["PRE_BOD"] > m["IN_BOD"] > m["POST_BOD"] > m["OPERATING"]


def test_classify_window_stage_mapping():
    assert classify_window(Stage.entitlement) == Window.PRE_BOD
    assert classify_window(Stage.design) == Window.IN_BOD
    assert classify_window(Stage.construction) == Window.POST_BOD
    assert classify_window(Stage.operating) == Window.OPERATING


def test_nod_filing_forces_post_bod():
    assert classify_window(Stage.entitlement, ["NOD"]) == Window.POST_BOD


def test_early_filing_cannot_unring_late_stage():
    assert classify_window(Stage.construction, ["NOP"]) == Window.POST_BOD


def test_recency_decay_halves_at_halflife(cfg):
    now = utcnow()
    stale = now - timedelta(days=180)
    d = recency_decay(cfg, stale, now)
    assert abs(d - 0.5) < 0.01
    assert recency_decay(cfg, now, now) > 0.99


def test_size_factor_log_scaled(cfg):
    assert size_factor(cfg, 1_000) == 1.0
    assert size_factor(cfg, 10_000) == 2.0
    assert size_factor(cfg, None) == 0.5
    assert size_factor(cfg, 10) == 0.25  # floor


def test_size_factor_reads_config_not_hardcoded():
    """The whole point of moving this into config.yaml -- a different
    offset/floor/unknown_default must actually change the result."""
    from app.config import Config
    cfg = Config({"scoring": {"size_factor": {"offset": 1.0, "floor": 0.1, "unknown_default": 0.9}}})
    assert size_factor(cfg, 1_000) == 2.0  # log10(1000) - 1.0 = 2.0, not the old 1.0
    assert size_factor(cfg, 10) == 0.1     # custom floor, not the old 0.25
    assert size_factor(cfg, None) == 0.9   # custom unknown_default, not the old 0.5


def test_days_to_bid(cfg):
    # entitlement is measured (CEQAnet NOP->NOD, {mid, low, high} in config.yaml,
    # not a bare int) -- days_to_estimated_bid reads .mid out of it.
    assert days_to_estimated_bid(cfg, Stage.entitlement) == 365
    assert days_to_estimated_bid(cfg, Stage.procurement) == 60
    assert days_to_estimated_bid(cfg, Stage.unknown) == 365  # unknown -> entitlement fallback


def test_days_to_bid_range_measured_stage_has_a_real_interval(cfg):
    low, high = days_to_estimated_bid_range(cfg, Stage.entitlement)
    assert low == 221
    assert high == 509
    assert low < days_to_estimated_bid(cfg, Stage.entitlement) < high


def test_days_to_bid_range_placeholder_stage_is_none(cfg):
    """A stage still on an invented placeholder gets no fake interval --
    see days_to_estimated_bid_range's docstring for why a zero-width or
    guessed range would misrepresent confidence that doesn't exist."""
    assert days_to_estimated_bid_range(cfg, Stage.procurement) == (None, None)
    assert days_to_estimated_bid_range(cfg, Stage.concept) == (None, None)


def test_identity_factor_fully_identified_is_untouched(cfg):
    assert identity_factor(cfg, "Vantage Data Centers NV12", "Vantage Data Centers NV12, LLC",
                            "Storey") == 1.0


def test_identity_factor_unnamed_prefix_counts_as_missing_name(cfg):
    # "Unnamed ..." is the literal marker resolve.py writes for no stated project_name.
    named = identity_factor(cfg, "Real Project", "Some Developer", "Storey")
    unnamed = identity_factor(cfg, "Unnamed project (Storey)", "Some Developer", "Storey")
    assert unnamed < named == 1.0


def test_identity_factor_penalizes_by_missing_count(cfg):
    one_missing = identity_factor(cfg, "Real Project", "Some Developer", None)
    two_missing = identity_factor(cfg, "Unnamed project (Storey)", None, "Storey")
    three_missing = identity_factor(cfg, None, None, None)
    assert 1.0 > one_missing > two_missing > three_missing > 0


def test_identity_factor_cannot_rank_unnamed_above_fully_identified(cfg):
    """The actual bug: 'Unnamed project (Storey)' outranked fully-identified rows
    with a lower certainty/window/size score. The penalty must be steep enough
    that identity alone can flip that ordering back."""
    now = utcnow()
    unnamed_high_prior = priority_score(
        cfg, [SignalType.abatement_application], Window.PRE_BOD,
        tons_midpoint=None, last_signal_at=now, now=now,
    ) * identity_factor(cfg, "Unnamed project (Storey)", None, "Storey")
    named_lower_prior = priority_score(
        cfg, [SignalType.planning_agenda], Window.PRE_BOD,
        tons_midpoint=None, last_signal_at=now, now=now,
    ) * identity_factor(cfg, "1977 Saturn Data Center Project", "Some LLC", "Los Angeles")
    assert named_lower_prior > unnamed_high_prior


# --- certainty_detail: same math as certainty(), plus the "why" ------------


def test_certainty_detail_value_matches_certainty(cfg):
    """The detail function must never disagree with the plain one -- it's a
    superset, not a parallel reimplementation that can drift."""
    for types in ([SignalType.ceqa_nop], [SignalType.ceqa_nop, SignalType.bid_invite], []):
        value, _detail = certainty_detail(cfg, types)
        assert value == certainty(cfg, types)


def test_certainty_detail_empty_signals_says_so():
    value, detail = certainty_detail(cfg={}, signal_types=[])
    assert value == 0.0
    assert "no linked signals" in detail.lower()


def test_certainty_detail_names_the_strongest_signal_type(cfg):
    _value, detail = certainty_detail(cfg, [SignalType.job_posting, SignalType.bid_invite])
    assert "bid_invite" in detail  # bid_invite has the higher prior (1.0 vs 0.45)


def test_certainty_detail_reports_corroboration_when_it_fires(cfg):
    single = certainty_detail(cfg, [SignalType.ceqa_nop])[1]
    corroborated = certainty_detail(cfg, [SignalType.ceqa_nop, SignalType.abatement_application])[1]
    assert "corroboration" not in single.lower()
    assert "corroboration" in corroborated.lower()


def test_certainty_detail_reports_the_cap(cfg):
    _value, detail = certainty_detail(
        cfg, [SignalType.bid_invite, SignalType.prequal_invite, SignalType.faa_7460, SignalType.abs_issuance])
    assert "capped at 1.00" in detail
