"""Four real CEQAnet project pairs that silently fragmented the board.

Each pair is one project filed twice — same State Clearinghouse number, different
document in the series (MND then NOD, EIR then NOD). They split into eight
project rows because `pair_similarity` compared the free-text APN field as an
exact string, and CEQAnet writes the same parcels differently on every filing:

    "4090-021-032 through -034"   vs  "4090-021-032, 4090-021-033, 4090-021-034"
    "209-411-02, ... -34 & -35"   vs  "209-411-02, ... -34 and -35"

APN carries weight 3.0 — the heaviest term in the function — and scored those as
DISAGREEMENT, dragging pairs with byte-identical names from an auto-merge 1.0 down
to ~0.59, into the adjudication band, where the LLM rejected them.

Nothing errored. The board just doubled those rows and every rate computed over
it was wrong.

These four are pinned so the failure is loud next time. If SCH parsing breaks, or
the APN term goes back to punishing unparseable input, these fail immediately
instead of the board quietly fragmenting again.

Data below is copied verbatim from production rows (projects 541/544, 575/601,
622/608, 491/538) on 2026-08-05.
"""
from __future__ import annotations

import pytest
from sqlmodel import select

from app.models import Category, Project, RawDocument, Signal, SignalType, Stage, TriageResult
from app.pipeline.resolve import pair_similarity, parse_apns, run_resolve

# (label, sch, name, county, apn_a, apn_b)
REAL_PAIRS = [
    ("2555 W. 190th Street", "2021020220",
     "2555 W. 190th Street Industrial Warehouse Project", "Los Angeles",
     "4090-021-032 through -034",
     "4090-021-032, 4090-021-033, 4090-021-034"),
    ("El Camino", "2023080369",
     "El Camino Project", "San Bernardino",
     "209-411-02, -03, -04, -23, -24, -32, -34 & -35",
     "209-411-02, -03, -04, -23, -24, -32, -34 and -35"),
    ("Cargo Solutions Hesperia", "2025060948",
     "Cargo Solutions Truck Warehouse and Truck Stop Hesperia Project",
     "San Bernardino",
     "3064-591-17, -18, -12, and -13 and 3064-631-01",
     "3064-591-17, -18, -12, -13 and 3064-631-01"),
    ("KPAC Coil Avenue", "2025041295",
     "KPAC Coil Avenue Freezer Expansion Project", "Los Angeles",
     "7426029006-007, 742603004-015,74",
     "7426029007"),
]


def _add_filing(session, sch, name, county, apn, doc_type, stage):
    doc = RawDocument(
        source="ceqanet", source_uid=f"{sch}:{doc_type}",
        url=f"https://ceqanet.lci.ca.gov/{sch}",
        title=name, raw_text=f"SCH Number: {sch}\n{name}",
        content_hash=f"{sch}:{doc_type}:hash",
        triage_result=TriageResult.relevant,
        meta={"sch_number": sch, "county": county,
              "triage_category": Category.industrial.value},
    )
    session.add(doc)
    session.flush()
    sig = Signal(
        raw_document_id=doc.id, signal_type=SignalType.planning_agenda,
        category=Category.industrial, project_name=name, county=county,
        state="CA", apn_parcel=apn, sch_number=sch, stage=stage,
        summary_one_line=name,
    )
    session.add(sig)
    session.flush()
    return sig


@pytest.mark.parametrize("label,sch,name,county,apn_a,apn_b", REAL_PAIRS,
                         ids=[p[0] for p in REAL_PAIRS])
def test_real_pair_resolves_to_one_project(db_session, cfg, label, sch, name,
                                           county, apn_a, apn_b):
    """Two filings of one project must land on one row, with no LLM involved."""
    _add_filing(db_session, sch, name, county, apn_a, "MND", Stage.entitlement)
    _add_filing(db_session, sch, name, county, apn_b, "NOD", Stage.permitting)

    run_resolve(db_session, cfg, use_llm=False)

    projects = db_session.exec(select(Project)).all()
    assert len(projects) == 1, (
        f"{label}: expected 1 project, got {len(projects)} — "
        f"{[p.name for p in projects]}. SCH matching or the APN term regressed."
    )
    assert projects[0].sch_number == sch
    # The later filing must still advance the stage, i.e. the merge kept both.
    assert projects[0].stage == Stage.permitting


@pytest.mark.parametrize("label,sch,name,county,apn_a,apn_b", REAL_PAIRS,
                         ids=[p[0] for p in REAL_PAIRS])
def test_matching_sch_is_dispositive(label, sch, name, county, apn_a, apn_b):
    """A shared SCH scores 1.0 even when every other field disagrees.

    Guards the short-circuit specifically: if it is removed, the differing APN
    strings drop these back into the adjudication band around 0.59.
    """
    sig = Signal(signal_type=SignalType.planning_agenda, category=Category.industrial,
                 project_name="totally different name", county="Kern", state="CA",
                 apn_parcel=apn_a, sch_number=sch, stage=Stage.entitlement)
    proj = Project(name=name, category=Category.industrial, county=county,
                   state="CA", apn_parcel=apn_b, sch_number=sch)
    assert pair_similarity(sig, proj, 5.0) == 1.0


@pytest.mark.parametrize("label,sch,name,county,apn_a,apn_b", REAL_PAIRS,
                         ids=[p[0] for p in REAL_PAIRS])
def test_apn_never_lowers_a_score(label, sch, name, county, apn_a, apn_b):
    """The invariant that fixes the bug: APN is positive-only evidence.

    Strip the SCH and compare the real, differently-punctuated APN strings
    against the same pair with no APN at all. The APN term may raise the score
    or stay silent; it must never drag the pair down. It used to cost these
    exact pairs ~0.41 of similarity, which is what pushed them out of
    auto-merge and into an LLM that rejected them.
    """
    def score(apn_x, apn_y):
        sig = Signal(signal_type=SignalType.planning_agenda,
                     category=Category.industrial, project_name=name,
                     county=county, state="CA", apn_parcel=apn_x,
                     stage=Stage.entitlement)
        proj = Project(name=name, category=Category.industrial, county=county,
                       state="CA", apn_parcel=apn_y)
        return pair_similarity(sig, proj, 5.0)

    baseline = score(None, None)
    with_apns = score(apn_a, apn_b)
    assert with_apns >= baseline, (
        f"{label}: adding APNs {apn_a!r} vs {apn_b!r} dropped similarity from "
        f"{baseline:.3f} to {with_apns:.3f}. APN must never vote against a match."
    )
    assert baseline >= cfg_auto(), (
        f"{label}: identical name + county alone scored {baseline:.3f}, which "
        f"should auto-merge."
    )


def cfg_auto() -> float:
    from app.config import load_config
    return load_config().get("resolution.auto_merge_threshold", 0.88)


def test_parse_apns_reads_the_real_strings():
    """Every real APN string yields parcel numbers.

    Overlap is asserted only where the two filings genuinely list a shared lot.
    KPAC is deliberately excluded: its first string is truncated mid-field
    ("7426029006-007, 742603004-015,74") and lists different lots from its pair,
    so the parser correctly finds no overlap and the term abstains. That pair is
    held together by SCH, which is the point of having SCH.
    """
    for label, _, _, _, apn_a, apn_b in REAL_PAIRS:
        a, b = parse_apns(apn_a), parse_apns(apn_b)
        assert a and b, f"{label}: failed to parse {apn_a!r} / {apn_b!r}"
        if label != "KPAC Coil Avenue":
            assert a & b, f"{label}: no overlap between {apn_a!r} and {apn_b!r}"


def test_parse_apns_abstains_rather_than_disagreeing():
    """Unreadable input yields an empty set, which callers treat as no opinion."""
    assert parse_apns(None) == set()
    assert parse_apns("") == set()
    assert parse_apns("see attached exhibit") == set()
    # bare suffix fragments are not parcel numbers
    assert parse_apns("-034") == set()


def test_different_sch_does_not_force_a_split(db_session, cfg):
    """Two SCH numbers can cover one development, so a mismatch must abstain.

    If disagreement scored 0.0 the way APN used to, we would have replaced one
    fragmentation bug with another.
    """
    sig = Signal(signal_type=SignalType.planning_agenda, category=Category.industrial,
                 project_name="Wonderful Logistics Center West", county="Kern",
                 state="CA", sch_number="2025010101", stage=Stage.entitlement)
    proj = Project(name="Wonderful Logistics Center West", category=Category.industrial,
                   county="Kern", state="CA", sch_number="2024129999")
    assert pair_similarity(sig, proj, 5.0) >= cfg.get(
        "resolution.auto_merge_threshold", 0.88)
