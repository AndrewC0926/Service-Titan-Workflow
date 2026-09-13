"""Block 4C Item 4 (Master Plan v3.6 section 45): "Ask the room."

"A rep posts a one-line question... It lands in Radar for anyone with
that account or a note mentioning it... The answer becomes a note under
their name." This module is the routing (who sees a question) and the
two writes (post a question, answer one) -- app/web/main.py's /room
routes and the Today page are the only callers.

Routing is config-driven (this item's own words): which roles ALWAYS see
every question, regardless of whether they have anything to do with its
anchor, comes from config.yaml's `ask_the_room.always_notify_roles`
(default ["manager"], matching the item's literal "and to managers").
"""
from __future__ import annotations

from sqlmodel import Session, select

from app.access_log import user_role
from app.config import Config
from app.models import (
    ASK_ROOM_ANCHOR_TYPES, Contact, DecisionNote, NoteType, Opportunity, Outcome, ProjectSignal, RoomQuestion,
    utcnow,
)


def _opportunity_ids_for_anchor(session: Session, anchor_type: str, anchor_id: int) -> list[int]:
    """Every Opportunity that "references" this account/contractor/
    project/building -- account and building are direct FKs on
    Opportunity itself; contractor is indirect via its Contact; project is
    indirect via its Signal's ProjectSignal link (Opportunity has no
    project_id of its own -- see app.models.Opportunity's own anchor
    fields)."""
    if anchor_type == "account":
        stmt = select(Opportunity.id).where(Opportunity.account_id == anchor_id)
    elif anchor_type == "building":
        stmt = select(Opportunity.id).where(Opportunity.building_id == anchor_id)
    elif anchor_type == "contractor":
        contact_ids = session.exec(select(Contact.id).where(Contact.contractor_id == anchor_id)).all()
        if not contact_ids:
            return []
        stmt = select(Opportunity.id).where(Opportunity.contact_id.in_(contact_ids))
    elif anchor_type == "project":
        signal_ids = session.exec(select(ProjectSignal.signal_id).where(ProjectSignal.project_id == anchor_id)).all()
        if not signal_ids:
            return []
        stmt = select(Opportunity.id).where(Opportunity.signal_id.in_(signal_ids))
    else:
        raise ValueError(f"unknown anchor_type {anchor_type!r} -- must be one of {sorted(ASK_ROOM_ANCHOR_TYPES)}")
    return list(session.exec(stmt).all())


def users_for_question(session: Session, cfg: Config, anchor_type: str, anchor_id: int) -> set[str]:
    """Every username who should see this question on Today: whoever owns,
    logged an Outcome on, or wrote a Note against an Opportunity that
    references the same account/contractor/project/building, PLUS every
    user in an "always notify" role (config-driven, default ["manager"]
    -- this item's own "and to managers"). The question's own author is
    never excluded here -- Today's own render skips a question for its
    author separately, so this function's contract stays simple: "who
    knows about this thing," not "who should be surprised by it."""
    if anchor_type not in ASK_ROOM_ANCHOR_TYPES:
        raise ValueError(f"unknown anchor_type {anchor_type!r} -- must be one of {sorted(ASK_ROOM_ANCHOR_TYPES)}")

    opp_ids = _opportunity_ids_for_anchor(session, anchor_type, anchor_id)
    users: set[str] = set()

    if opp_ids:
        owners = session.exec(
            select(Opportunity.owner_user).where(Opportunity.id.in_(opp_ids), Opportunity.owner_user.is_not(None))
        ).all()
        users.update(owners)
        outcome_users = session.exec(select(Outcome.user).where(Outcome.opportunity_id.in_(opp_ids))).all()
        users.update(outcome_users)

    # DecisionNote: direct anchor match for account/building/contractor,
    # PLUS any note anchored to one of the same opportunities above (the
    # "references the same X" wording covers a note written against the
    # Opportunity itself, not just one written directly against the
    # account/building/contractor row).
    note_filters = []
    if anchor_type == "account":
        note_filters.append(DecisionNote.account_id == anchor_id)
    elif anchor_type == "building":
        note_filters.append(DecisionNote.building_id == anchor_id)
    elif anchor_type == "contractor":
        note_filters.append(DecisionNote.contractor_id == anchor_id)
    elif anchor_type == "project":
        note_filters.append(DecisionNote.project_id == anchor_id)
    if opp_ids:
        note_filters.append(DecisionNote.opportunity_id.in_(opp_ids))
    if note_filters:
        from sqlalchemy import or_
        note_authors = session.exec(select(DecisionNote.author).where(or_(*note_filters))).all()
        users.update(note_authors)

    always_notify_roles = set(cfg.get("ask_the_room.always_notify_roles", ["manager"]))
    for username in _all_configured_usernames(cfg):
        if user_role(cfg, username) in always_notify_roles:
            users.add(username)

    users.discard(None)
    return users


def _all_configured_usernames(cfg: Config) -> list[str]:
    return [entry["username"] for entry in cfg.get("dashboard.users", []) or [] if entry.get("username")]


def open_questions_for_user(session: Session, cfg: Config, username: str) -> list[RoomQuestion]:
    """Every unanswered RoomQuestion this user should see -- their own
    posts included (a rep should see their own open question sitting
    there unanswered, same as anyone else's), newest first."""
    open_qs = session.exec(
        select(RoomQuestion).where(RoomQuestion.answered_note_id.is_(None)).order_by(RoomQuestion.created_at.desc())
    ).all()
    return [q for q in open_qs if q.author == username or username in users_for_question(
        session, cfg, q.anchor_type, q.anchor_id)]


def post_question(session: Session, *, anchor_type: str, anchor_id: int, text: str, author: str) -> RoomQuestion:
    if anchor_type not in ASK_ROOM_ANCHOR_TYPES:
        raise ValueError(f"unknown anchor_type {anchor_type!r} -- must be one of {sorted(ASK_ROOM_ANCHOR_TYPES)}")
    text = text.strip()
    if not text:
        raise ValueError("a question needs actual text")
    question = RoomQuestion(anchor_type=anchor_type, anchor_id=anchor_id, text=text, author=author)
    session.add(question)
    session.flush()
    return question


_ANCHOR_KWARG = {"account": "account_id", "contractor": "contractor_id",
                 "project": "project_id", "building": "building_id"}


def answer_question(session: Session, *, question_id: int, answer_text: str, answerer: str) -> DecisionNote:
    """The answer becomes a real DecisionNote(note_type=intel) anchored to
    the SAME thing the question was about, under the answerer's own name
    -- "the answer becomes a note under their name," this item's own
    words. Written before this question is marked answered, so a crash
    between the two leaves the question open (safe: someone can answer
    again) rather than silently swallowing an answer that was never
    actually recorded."""
    from app.pipeline.notes import log_note

    question = session.get(RoomQuestion, question_id)
    if question is None:
        raise ValueError(f"no RoomQuestion #{question_id}")
    if question.answered_note_id is not None:
        raise ValueError(f"RoomQuestion #{question_id} was already answered (note #{question.answered_note_id})")
    answer_text = answer_text.strip()
    if not answer_text:
        raise ValueError("an answer needs actual text")

    anchor_kwargs = {_ANCHOR_KWARG[question.anchor_type]: question.anchor_id}
    note = log_note(session, note_type=NoteType.intel, lead_source="relationship", author=answerer,
                    free_text=f'Q: "{question.text}" (asked by {question.author})\nA: {answer_text}',
                    **anchor_kwargs)
    question.answered_note_id = note.id
    session.add(question)
    session.flush()
    return note
