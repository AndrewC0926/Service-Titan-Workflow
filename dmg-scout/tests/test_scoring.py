from datetime import timedelta

from app.models import SignalType, Stage, Window, utcnow
from app.pipeline.scoring import (
    certainty,
    classify_window,
    days_to_estimated_bid,
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


def test_size_factor_log_scaled():
    assert size_factor(1_000) == 1.0
    assert size_factor(10_000) == 2.0
    assert size_factor(None) == 0.5
    assert size_factor(10) == 0.25  # floor


def test_days_to_bid(cfg):
    assert days_to_estimated_bid(cfg, Stage.entitlement) == 540
    assert days_to_estimated_bid(cfg, Stage.procurement) == 60
    assert days_to_estimated_bid(cfg, Stage.unknown) == 540
