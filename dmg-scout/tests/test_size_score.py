"""score_breakdown(): the score explained, term by term, and the batched
signal-type fetch the board uses so a hover breakdown on 300+ rows doesn't
mean 300+ extra queries. See app/pipeline/size_score.py."""
from datetime import timedelta

from app.models import Category, Project, ProjectSignal, Signal, SignalType, Stage, Window, utcnow
from app.pipeline.size_score import (
    ScoreBreakdown,
    project_score_breakdown,
    run_size_score,
    score_breakdown,
    signal_types_by_project,
)


def _proj(**kw):
    base = dict(category=Category.data_center, stage=Stage.entitlement, status="active")
    return Project(**{**base, **kw})


# --- score_breakdown: pure function -----------------------------------------


def test_score_breakdown_total_matches_priority_score_times_identity(cfg):
    """Cross-check against the actual scoring pipeline's own formula
    (app/pipeline/size_score.py:run_size_score) -- this must not be a
    parallel reimplementation that quietly disagrees with what's stored."""
    from app.pipeline.scoring import identity_factor, priority_score

    now = utcnow()
    signal_types = [SignalType.ceqa_nop, SignalType.abatement_application]
    window = Window.PRE_BOD
    tons_mid = 1900.0
    last_signal_at = now - timedelta(days=45)

    bd = score_breakdown(
        cfg, signal_types=signal_types, window=window, tons_midpoint=tons_mid,
        last_signal_at=last_signal_at, name="Real Project", developer="Real Dev",
        county="Los Angeles", spillover_mw=None, spillover_enabled=False, now=now,
    )
    expected = priority_score(cfg, signal_types, window, tons_mid, last_signal_at, now=now) \
        * identity_factor(cfg, "Real Project", "Real Dev", "Los Angeles")
    assert abs(bd.total - round(expected, 4)) < 1e-6


def test_score_breakdown_includes_spillover_only_when_enabled_and_present(cfg):
    now = utcnow()
    with_spillover = score_breakdown(
        cfg, signal_types=[SignalType.ceqa_nop], window=Window.PRE_BOD, tons_midpoint=1000,
        last_signal_at=now, name="P", developer="D", county="C",
        spillover_mw=400, spillover_enabled=True, now=now,
    )
    assert any(t.label == "Spillover" for t in with_spillover.terms)

    disabled = score_breakdown(
        cfg, signal_types=[SignalType.ceqa_nop], window=Window.PRE_BOD, tons_midpoint=1000,
        last_signal_at=now, name="P", developer="D", county="C",
        spillover_mw=400, spillover_enabled=False, now=now,
    )
    assert not any(t.label == "Spillover" for t in disabled.terms)

    no_mw = score_breakdown(
        cfg, signal_types=[SignalType.ceqa_nop], window=Window.PRE_BOD, tons_midpoint=1000,
        last_signal_at=now, name="P", developer="D", county="C",
        spillover_mw=None, spillover_enabled=True, now=now,
    )
    assert not any(t.label == "Spillover" for t in no_mw.terms)


def test_score_breakdown_as_text_shows_every_term_and_the_total():
    bd = ScoreBreakdown(terms=[], total=0.5)
    from app.pipeline.size_score import ScoreTerm
    bd.terms = [ScoreTerm("Certainty", 0.9, "0.90 base"), ScoreTerm("Window", 0.7, "PRE-BOD -> x0.70")]
    text = bd.as_text()
    assert "Certainty: 0.90 base" in text
    assert "Window: PRE-BOD -> x0.70" in text
    assert "= 0.5000" in text


def test_score_breakdown_unknown_size_says_so(cfg):
    now = utcnow()
    bd = score_breakdown(
        cfg, signal_types=[SignalType.ceqa_nop], window=Window.PRE_BOD, tons_midpoint=None,
        last_signal_at=now, name="P", developer="D", county="C", now=now,
    )
    size_term = next(t for t in bd.terms if t.label == "Size")
    assert "unknown" in size_term.detail.lower()


def test_score_breakdown_no_dated_signal_says_so(cfg):
    bd = score_breakdown(
        cfg, signal_types=[SignalType.ceqa_nop], window=Window.PRE_BOD, tons_midpoint=1000,
        last_signal_at=None, name="P", developer="D", county="C",
    )
    recency_term = next(t for t in bd.terms if t.label == "Recency")
    assert "no dated signal" in recency_term.detail.lower()


# --- signal_types_by_project: batched fetch ---------------------------------


def test_signal_types_by_project_groups_correctly(db_session, cfg):
    p1 = _proj(name="P1")
    p2 = _proj(name="P2")
    db_session.add(p1)
    db_session.add(p2)
    db_session.commit()
    db_session.refresh(p1)
    db_session.refresh(p2)

    s1 = Signal(signal_type=SignalType.ceqa_nop, category=Category.data_center, stage=Stage.entitlement)
    s2 = Signal(signal_type=SignalType.bid_invite, category=Category.data_center, stage=Stage.entitlement)
    db_session.add(s1)
    db_session.add(s2)
    db_session.commit()
    db_session.refresh(s1)
    db_session.refresh(s2)

    db_session.add(ProjectSignal(project_id=p1.id, signal_id=s1.id))
    db_session.add(ProjectSignal(project_id=p1.id, signal_id=s2.id))
    db_session.commit()

    out = signal_types_by_project(db_session, [p1.id, p2.id])
    assert set(out[p1.id]) == {SignalType.ceqa_nop, SignalType.bid_invite}
    assert out[p2.id] == []


def test_signal_types_by_project_empty_input():
    assert signal_types_by_project(None, []) == {}


# --- project_score_breakdown: single-project convenience wrapper -----------


def test_project_score_breakdown_uses_stored_tons_midpoint(db_session, cfg):
    """RATCHET-adjacent transparency bug, fixed here: run_size_score() feeds
    priority_score() TonsEstimate.midpoint, the GEOMETRIC mean (see that
    property's own docstring -- size_factor is log10-scaled, so arithmetic
    is the wrong centre for a band spanning orders of magnitude).
    project_score_breakdown() must read the same midpoint through the same
    property, not reimplement (low+high)/2 under the same name -- that
    arithmetic reimplementation is exactly what silently disagreed with
    Project.score before this fix."""
    project = _proj(name="Stored Tons Project", developer="Dev", county="Los Angeles",
                    window=Window.PRE_BOD, tons_estimate_low=1000, tons_estimate_high=2000,
                    last_signal_at=utcnow())
    db_session.add(project)
    db_session.commit()
    db_session.refresh(project)

    bd = project_score_breakdown(db_session, cfg, project)
    size_term = next(t for t in bd.terms if t.label == "Size")
    assert "1,414" in size_term.detail  # geometric mean of 1000-2000 (sqrt(1000*2000)), not 1,500


def test_project_score_breakdown_matches_run_size_score_end_to_end(db_session, cfg):
    """The whole point of project_score_breakdown(): its product must equal
    Project.score for a project run_size_score() actually scored -- a wide
    tons band (not the narrow one test_size_score_end_to_end already
    covers) is exactly the case that silently disagreed before the
    geometric-midpoint fix, per TonsEstimate.midpoint's own docstring."""
    project = _proj(name="Wide Band Project", developer="Dev", county="Los Angeles")
    db_session.add(project)
    db_session.commit()
    signal = Signal(signal_type=SignalType.ceqa_nop, category=Category.industrial,
                    stage=Stage.entitlement, building_sqft=2_000_000,
                    confidence=0.9, summary_one_line="s")
    db_session.add(signal)
    db_session.commit()
    db_session.add(ProjectSignal(project_id=project.id, signal_id=signal.id,
                                 match_confidence=1.0, match_method="direct"))
    db_session.commit()

    run_size_score(db_session, cfg)
    db_session.refresh(project)

    bd = project_score_breakdown(db_session, cfg, project)
    assert bd.total == project.score


def test_project_score_breakdown_no_signals_still_returns_a_breakdown(db_session, cfg):
    project = _proj(name="No Signals Project")
    db_session.add(project)
    db_session.commit()
    db_session.refresh(project)

    bd = project_score_breakdown(db_session, cfg, project)
    cert_term = next(t for t in bd.terms if t.label == "Certainty")
    assert cert_term.value == 0.0
