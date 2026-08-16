"""Fuzzy name resolution for voice-capture proposals against Scout's own
roster tables, so a review_queue row never stores a free-text name as if it
were a resolved identity — see app/pipeline/voice_capture.py.

Misheard proper nouns are the dominant failure mode of a voice pipeline —
worse than transcription quality otherwise, since Whisper is generally
accurate on clear audio but has no way to know "Acco" from "ACCO" from
"Ekko" is the contractor already in this system. rapidfuzz token_sort_ratio
on normalized names is the same technique app/ladder.py and
app/pipeline/resolve.py already use to match extracted names against this
system's own tables — applied here to a name a human HEARD and repeated
into a phone, not one read off a filing.

Every resolver returns ranked candidates for a human to pick from, never an
auto-selected match. Below MATCH_THRESHOLD, nothing is returned at all —
the field stays unresolved and flagged, the same "abstain, don't guess"
discipline app.pipeline.retrofit's masked-APN exclusion and
app.pipeline.ebewe's checksum exclusion already apply elsewhere in this
codebase.
"""
from __future__ import annotations

from dataclasses import dataclass

from rapidfuzz import fuzz
from sqlmodel import select

from app.models import Contact, Contractor, Firm, Project
from app.normalize import normalize_name, normalize_person_name

# Below this score a candidate isn't worth showing -- a low-confidence
# suggestion trains the reviewer to stop reading the list. Matches the floor
# app/importers/accounts_csv.py's own fuzzy account-name matching uses for
# the same reason.
MATCH_THRESHOLD = 60.0
MAX_CANDIDATES = 5


@dataclass
class VoiceMatchCandidate:
    entity_type: str  # contact | firm | project | contractor
    entity_id: int
    label: str
    score: float


def _topn(scored: list[VoiceMatchCandidate]) -> list[VoiceMatchCandidate]:
    scored.sort(key=lambda c: c.score, reverse=True)
    return scored[:MAX_CANDIDATES]


def resolve_contact(session, name: str | None) -> list[VoiceMatchCandidate]:
    if not name:
        return []
    query = normalize_person_name(name)
    if not query:
        return []
    rows = session.exec(select(Contact.id, Contact.name, Contact.company)).all()
    out = []
    for id_, cname, company in rows:
        score = fuzz.token_sort_ratio(query, normalize_person_name(cname))
        if score >= MATCH_THRESHOLD:
            label = f"{cname}" + (f" — {company}" if company else "")
            out.append(VoiceMatchCandidate("contact", id_, label, round(score, 1)))
    return _topn(out)


def resolve_firm(session, name: str | None) -> list[VoiceMatchCandidate]:
    if not name:
        return []
    query = normalize_name(name)
    if not query:
        return []
    rows = session.exec(select(Firm.id, Firm.name, Firm.name_norm)).all()
    out = []
    for id_, fname, norm in rows:
        score = fuzz.token_sort_ratio(query, norm)
        if score >= MATCH_THRESHOLD:
            out.append(VoiceMatchCandidate("firm", id_, fname, round(score, 1)))
    return _topn(out)


def resolve_project(session, name: str | None) -> list[VoiceMatchCandidate]:
    """Scoped to Project only, not RetrofitBuilding -- app.outreach.log_outreach
    (the one Outreach writer, see app/outreach.py) requires a project_id, and
    RetrofitBuilding has no outreach-logging path of its own today. A call
    naming a retrofit building's address rather than a Project name will
    correctly come back unresolved here; the reviewer edits the field by
    hand rather than this silently matching the wrong entity type."""
    if not name:
        return []
    query = normalize_name(name)
    if not query:
        return []
    rows = session.exec(select(Project.id, Project.name, Project.developer)).all()
    out = []
    for id_, pname, developer in rows:
        score = fuzz.token_sort_ratio(query, normalize_name(pname))
        if score >= MATCH_THRESHOLD:
            label = pname + (f" — {developer}" if developer else "")
            out.append(VoiceMatchCandidate("project", id_, label, round(score, 1)))
    return _topn(out)


def resolve_contractor(session, name: str | None) -> list[VoiceMatchCandidate]:
    """Against the CSLB roster (app/pipeline/cslb.py) -- a call naming a
    contractor/subcontractor rather than a manufacturer's-rep contact."""
    if not name:
        return []
    query = normalize_name(name)
    if not query:
        return []
    rows = session.exec(select(Contractor.id, Contractor.business_name, Contractor.city)).all()
    out = []
    for id_, bname, city in rows:
        if not bname:
            continue
        score = fuzz.token_sort_ratio(query, normalize_name(bname))
        if score >= MATCH_THRESHOLD:
            label = bname + (f" — {city}" if city else "")
            out.append(VoiceMatchCandidate("contractor", id_, label, round(score, 1)))
    return _topn(out)


def resolve_all(session, *, contact_name: str | None, firm_name: str | None,
                project_or_building_name: str | None) -> dict[str, list[VoiceMatchCandidate]]:
    """Runs every resolver for one capture's proposed names. Firm and
    contractor are both checked against firm_name -- a caller doesn't
    reliably distinguish "the rep firm" from "the mechanical contractor",
    and showing candidates from both rosters costs nothing extra (the human
    picks the right one, or neither)."""
    return {
        "contact": resolve_contact(session, contact_name),
        "firm": resolve_firm(session, firm_name),
        "contractor": resolve_contractor(session, firm_name),
        "project": resolve_project(session, project_or_building_name),
    }
