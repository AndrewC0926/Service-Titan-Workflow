"""Invariant 4, applied past APN: a field that cannot be compared abstains.

The APN fix taught the scorer that non-overlap of a free-text parcel list is not
disagreement. This is the audit that asked which other fields have the same shape
— on both sides of resolve, because blocking can hide a pair from the scorer just
as effectively as the scorer can misjudge it.

Findings, with measured incidence on the 2026-08-06 corpus:
  blocking, status filter  LIVE   4 signal->merged-project pairs
  blocking, APN equality   latent 13 of 294 APN-bearing signals
  scoring, MW units        latent 0 (nothing carries mw_it yet)
  scoring, county/state    latent 0 (no cross-state county name in CA+NV)
  scoring, empty name      latent 0
Latent is not the same as harmless: every one of these is a term that votes with
confidence on a value it has no basis to judge.
"""
from sqlmodel import select

from app.models import Category, Project, Signal, SignalType, Stage, TriageResult
from app.pipeline.resolve import _blocked_candidates, pair_similarity


def _sig(**kw):
    base = dict(signal_type=SignalType.ceqa_nop, triage_result=TriageResult.relevant,
                category=Category.data_center, stage=Stage.entitlement)
    return Signal(**{**base, **kw})


def _proj(**kw):
    base = dict(category=Category.data_center, stage=Stage.entitlement, status="active")
    return Project(**{**base, **kw})


# --- blocking: the status filter (LIVE) -------------------------------------

def test_merged_project_is_not_a_blocking_candidate(db_session):
    """The live one. A merged-away project stayed reachable via SCH, APN and geo.

    Signal 503 still reaches project #963 that way on the real board, and #963 is
    the duplicate a merge cleaned up after the concurrent-resolve bug. An SCH
    agreement short-circuits pair_similarity to 1.0, so the next resolve would
    link the signal straight back onto the merged row and rebuild the duplicate.
    """
    merged = _proj(name="First Industrial Commerce Center II", sch_number="2024050123",
                   apn_parcel="4090-021-032", county="Los Angeles", state="CA",
                   latitude=34.0, longitude=-118.2, status="merged")
    db_session.add(merged)
    db_session.commit()
    sig = _sig(project_name="First Industrial Commerce Center", sch_number="2024050123",
               apn_parcel="4090-021-032", county="Los Angeles", state="CA",
               latitude=34.0, longitude=-118.2)
    db_session.add(sig)
    db_session.commit()
    assert _blocked_candidates(db_session, sig, 5.0) == []


def test_active_project_still_blocks_on_every_key(db_session):
    """The filter must not throw the baby out: active projects still reach."""
    for key in ("sch", "apn", "geo"):
        p = _proj(name=f"Campus {key}", county="Nowhere", state="CA",
                  sch_number="2024050999" if key == "sch" else None,
                  apn_parcel="5090-021-032" if key == "apn" else None,
                  latitude=35.0 if key == "geo" else None,
                  longitude=-119.0 if key == "geo" else None)
        db_session.add(p)
        db_session.commit()
        sig = _sig(project_name="Something Else Entirely",
                   sch_number=p.sch_number, apn_parcel=p.apn_parcel,
                   latitude=p.latitude, longitude=p.longitude)
        db_session.add(sig)
        db_session.commit()
        assert p.id in [c.id for c in _blocked_candidates(db_session, sig, 5.0)], key
        db_session.delete(p)
        db_session.delete(sig)
        db_session.commit()


# --- blocking: APN overlap rather than string equality ----------------------

def test_blocking_uses_parsed_parcel_overlap(db_session):
    """The same clerk-formatting problem the scorer already knows about.

    The scorer treats a shared parcel as near-dispositive; blocking compared the
    raw strings, so it could keep that pair away from the scorer entirely.
    """
    p = _proj(name="Vernon Backup Generating Facility",
              apn_parcel="4090-021-032 through -034", county="Los Angeles", state="CA")
    db_session.add(p)
    db_session.commit()
    sig = _sig(project_name="Vernon Facility",
               apn_parcel="4090-021-032, 4090-021-033, 4090-021-034",
               county="San Diego", state="CA")  # different county: APN is the only key
    db_session.add(sig)
    db_session.commit()
    assert p.id in [c.id for c in _blocked_candidates(db_session, sig, 5.0)]


def test_unrelated_parcels_do_not_block(db_session):
    p = _proj(name="Somewhere Else", apn_parcel="7426-029-006", county="Kern", state="CA")
    db_session.add(p)
    db_session.commit()
    sig = _sig(project_name="Different Site", apn_parcel="4090-021-032",
               county="San Diego", state="CA")
    db_session.add(sig)
    db_session.commit()
    assert _blocked_candidates(db_session, sig, 5.0) == []


# --- scoring: megawatts must be compared like with like ---------------------

def test_it_megawatts_do_not_vote_against_total_megawatts():
    """One campus described two ways must not score its own match down.

    IT load runs ~60-75% of facility total, so a 100 MW IT signal against a 140 MW
    total project used to contribute a 0.71 ratio — a vote against, computed from
    two numbers that were never the same quantity.
    """
    sig = _sig(project_name="Sierra Campus", county="Washoe", state="NV", mw_it=100.0)
    proj = _proj(name="Sierra Campus", county="Washoe", state="NV", mw_total=140.0)
    with_mixed = pair_similarity(sig, proj, 5.0)
    # identical inputs minus the incomparable MW pair
    proj_no_mw = _proj(name="Sierra Campus", county="Washoe", state="NV")
    assert with_mixed == pair_similarity(sig, proj_no_mw, 5.0)


def test_like_megawatts_still_compare():
    sig = _sig(project_name="Sierra Campus", county="Washoe", state="NV", mw_total=100.0)
    near = _proj(name="Sierra Campus", county="Washoe", state="NV", mw_total=100.0)
    far = _proj(name="Sierra Campus", county="Washoe", state="NV", mw_total=10.0)
    assert pair_similarity(sig, near, 5.0) > pair_similarity(sig, far, 5.0)


# --- scoring: county agreement is only meaningful inside a state ------------

def test_same_county_name_in_two_states_is_not_agreement():
    """"Washington County" is a real county in eighteen states."""
    sig = _sig(project_name="Alpha Project", county="Washington", state="UT")
    same = _proj(name="Alpha Project", county="Washington", state="UT")
    other = _proj(name="Alpha Project", county="Washington", state="OR")
    assert pair_similarity(sig, same, 5.0) > pair_similarity(sig, other, 5.0)


def test_county_still_agrees_when_a_state_is_unknown():
    """Abstaining on state must not turn a real county match into a mismatch."""
    sig = _sig(project_name="Alpha Project", county="Washoe", state=None)
    proj = _proj(name="Alpha Project", county="Washoe", state="NV")
    assert pair_similarity(sig, proj, 5.0) > 0.9


# --- scoring: a name that does not survive normalization abstains -----------

def test_name_that_normalizes_to_nothing_abstains():
    """normalize_name is built for companies: 'Phase II LLC' reduces to ''.

    Comparing that scored 0 at weight 2.0 and set strong_evidence, so an
    unparseable name both voted against the match and certified the pair as
    well-evidenced enough to auto-merge on.
    """
    from app.normalize import normalize_name
    assert normalize_name("Phase II LLC") == ""
    sig = _sig(project_name="Phase II LLC", county="Washoe", state="NV",
               developer_or_owner="Vantage Data Centers")
    proj = _proj(name="Sierra Campus", county="Washoe", state="NV",
                 developer="Vantage Data Centers")
    sim = pair_similarity(sig, proj, 5.0)
    # county + developer only is weak evidence, and weak evidence is capped below
    # auto-merge so a human or the adjudicator decides
    from app.pipeline.resolve import WEAK_EVIDENCE_CAP
    assert sim <= WEAK_EVIDENCE_CAP


def test_real_names_still_score_normally():
    sig = _sig(project_name="Sierra Campus", county="Washoe", state="NV")
    proj = _proj(name="Sierra Campus", county="Washoe", state="NV")
    assert pair_similarity(sig, proj, 5.0) > 0.9
