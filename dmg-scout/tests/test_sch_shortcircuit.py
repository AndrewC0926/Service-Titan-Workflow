"""Does the SCH key actually work end to end?

Written because #961/#963 looked like it should have been impossible. Run 2
processed signal 503 twenty-one seconds after run 1 committed project #961 with
the same SCH number. Blocking should have surfaced it, the short-circuit should
have returned 1.0, and it should have auto-merged. Instead it reached the LLM and
came back no_match.

Two things could explain that: blocking did not surface a project with a matching
SCH, or the short-circuit did not fire. Both are defects in the mechanism Phase A
was built on, so both get tested directly rather than reasoned about.

Each layer is asserted separately so a failure says which one broke:
  1. _blocked_candidates surfaces a project with the same SCH
  2. pair_similarity returns exactly 1.0 for that pair
  3. run_resolve auto-merges it, with no LLM call at all
"""
from sqlmodel import select

from app.models import Category, Project, ProjectSignal, Signal, SignalType, Stage
from app.pipeline.resolve import _blocked_candidates, pair_similarity, run_resolve

SCH = "2026010975"


def _project(session, **kw):
    p = Project(name=kw.pop("name", "First Industrial Commerce Center II"),
                category=kw.pop("category", Category.industrial),
                developer=kw.pop("developer", "First Industrial Realty Trust, Inc."),
                county=kw.pop("county", "Riverside"), state=kw.pop("state", "CA"),
                sch_number=kw.pop("sch_number", SCH), status="active",
                stage=Stage.entitlement, **kw)
    session.add(p)
    session.commit()
    return p


def _signal(session, **kw):
    s = Signal(raw_document_id=None, signal_type=SignalType.ceqa_nop,
               category=kw.pop("category", Category.industrial),
               project_name=kw.pop("project_name", "First Industrial Commerce Center II"),
               developer_or_owner=kw.pop("developer_or_owner",
                                         "First Industrial Realty Trust, Inc."),
               county=kw.pop("county", "Riverside"), state=kw.pop("state", "CA"),
               sch_number=kw.pop("sch_number", SCH), stage=Stage.entitlement,
               summary_one_line="x", **kw)
    session.add(s)
    session.commit()
    return s


# ---- layer 1: blocking ---------------------------------------------------

def test_blocking_surfaces_a_project_with_the_same_sch(db_session, cfg):
    p = _project(db_session)
    sig = _signal(db_session)
    got = _blocked_candidates(db_session, sig, radius_km=5)
    assert p.id in {c.id for c in got}


def test_blocking_finds_it_on_sch_alone_when_nothing_else_matches(db_session, cfg):
    """SCH must reach the scorer even with no county, developer or coordinates.

    This is the case the SCH blocking key was added for: a filing whose county
    came back null would otherwise be blocked out of its own project.
    """
    p = _project(db_session, county=None, developer=None)
    sig = _signal(db_session, county=None, developer_or_owner=None,
                  project_name="Something Else Entirely")
    got = _blocked_candidates(db_session, sig, radius_km=5)
    assert p.id in {c.id for c in got}, "SCH alone did not reach the scorer"


# ---- layer 2: the short-circuit -----------------------------------------

def test_pair_similarity_short_circuits_to_exactly_one(db_session, cfg):
    p = _project(db_session)
    sig = _signal(db_session)
    assert pair_similarity(sig, p, radius_km=5) == 1.0


def test_short_circuit_beats_every_disagreement(db_session, cfg):
    """A matching SCH must win even when everything else disagrees.

    If any other term can drag a matching SCH below auto_merge_threshold, the
    key is not a key.
    """
    p = _project(db_session, name="Totally Different Name", county="Kern",
                 developer="Someone Else LLC", apn_parcel="111-111-111")
    sig = _signal(db_session, project_name="Nothing Like It", county="Imperial",
                  developer_or_owner="Another Party Inc", apn_parcel="999-999-999")
    assert pair_similarity(sig, p, radius_km=5) == 1.0


def test_whitespace_around_the_sch_still_matches(db_session, cfg):
    p = _project(db_session, sch_number=f"  {SCH} ")
    sig = _signal(db_session, sch_number=f"{SCH}\n")
    assert pair_similarity(sig, p, radius_km=5) == 1.0


def test_a_different_sch_abstains_rather_than_scoring_zero(db_session, cfg):
    """Two SCH numbers can cover one development, so a mismatch must not veto."""
    p = _project(db_session, sch_number="2020010101")
    sig = _signal(db_session, sch_number="2026010975")
    # Everything else agrees, so an abstaining mismatch should still score high.
    assert pair_similarity(sig, p, radius_km=5) > 0.55


# ---- layer 3: end to end through resolve --------------------------------

def test_resolve_auto_merges_on_sch_without_calling_the_llm(db_session, cfg):
    """The full path #961/#963 should have taken.

    use_llm=True deliberately: adjudicate() would raise if reached, because there
    is no API key in tests and no network. Reaching it at all is the bug.
    """
    p = _project(db_session)
    sig = _signal(db_session)

    called = []

    import app.pipeline.resolve as mod
    original = mod.adjudicate

    def tripwire(*a, **k):
        called.append(1)
        return original(*a, **k)

    mod.adjudicate = tripwire
    try:
        stats = run_resolve(db_session, cfg, use_llm=True)
    finally:
        mod.adjudicate = original

    assert not called, "SCH match went to the LLM instead of auto-merging"
    assert stats["auto_linked"] == 1, stats
    assert stats["new_projects"] == 0, "created a second project despite the SCH match"

    links = db_session.exec(
        select(ProjectSignal).where(ProjectSignal.signal_id == sig.id)).all()
    assert len(links) == 1 and links[0].project_id == p.id
    assert links[0].match_method == "blocking+fuzzy"
    assert len(db_session.exec(select(Project)).all()) == 1


def test_resolve_merges_even_when_only_the_sch_agrees(db_session, cfg):
    """The hostile version: nothing but the SCH is shared."""
    p = _project(db_session, name="Totally Different Name", county="Kern",
                 developer="Someone Else LLC")
    sig = _signal(db_session, project_name="Nothing Like It", county="Imperial",
                  developer_or_owner="Another Party Inc")

    stats = run_resolve(db_session, cfg, use_llm=False)

    assert stats["auto_linked"] == 1, stats
    assert len(db_session.exec(select(Project)).all()) == 1, "the board fragmented"
    link = db_session.exec(
        select(ProjectSignal).where(ProjectSignal.signal_id == sig.id)).one()
    assert link.project_id == p.id
