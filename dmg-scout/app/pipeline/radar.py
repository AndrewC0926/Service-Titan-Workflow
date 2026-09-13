"""Block 4C Item 5 (Master Plan v3.6 section 42): "Two modes on the same
data." Guide is Today exactly as it already is -- no code here touches
it. Radar is four panels, all scoped to one user's own book, no
suggested calls: what changed since their last visit (new Signals on
their own buildings or contractors), their Opportunities by weakest why,
deadline exposure across those same Opportunities, and their Notes and
patterns.

Mode is a per-user PREFERENCE, not a role -- persisted in UserPreference,
flippable by the user themselves (POST /today/mode), never config-driven
the way role is."""
from __future__ import annotations

from datetime import datetime

from sqlmodel import Session, select

from app.config import Config
from app.models import Contact, DecisionNote, Opportunity, RetrofitBuilding, UserPreference, utcnow

VALID_MODES = frozenset({"guide", "radar"})
DEFAULT_MODE = "guide"

# Master Plan v3.6 section 42/43's fuller Radar also names "quiet accounts
# and whitespace by line from NetSuite" -- this item's own four panels
# don't include that one, but the same customer-master gap would block it
# if a future panel tried. Shown explicitly wherever that gap is real,
# never a silently empty section pretending nothing is missing.
CUSTOMER_MASTER_NOTE = "not loaded -- no NetSuite order history or whitespace-by-line data exists yet"


def get_mode(session: Session, username: str) -> str:
    pref = session.get(UserPreference, username)
    return pref.mode if pref else DEFAULT_MODE


def set_mode(session: Session, username: str, mode: str) -> UserPreference:
    if mode not in VALID_MODES:
        raise ValueError(f"unknown mode {mode!r} -- must be one of {sorted(VALID_MODES)}")
    pref = session.get(UserPreference, username)
    if pref is None:
        pref = UserPreference(username=username, mode=mode)
    else:
        pref.mode = mode
    pref.updated_at = utcnow()
    session.add(pref)
    session.flush()
    return pref


def record_radar_visit(session: Session, username: str) -> datetime | None:
    """Stamps "now" as this user's last Radar visit and returns the
    PREVIOUS value (None on a first-ever visit) -- callers compute "since
    last visit" against the returned value BEFORE this call updates it,
    so a panel never compares a visit against itself."""
    pref = session.get(UserPreference, username)
    previous = pref.last_radar_visit_at if pref else None
    if pref is None:
        pref = UserPreference(username=username, mode=DEFAULT_MODE)
    pref.last_radar_visit_at = utcnow()
    pref.updated_at = utcnow()
    session.add(pref)
    session.flush()
    return previous


def _own_opportunities(session: Session, username: str) -> list[Opportunity]:
    return session.exec(select(Opportunity).where(Opportunity.owner_user == username)).all()


def new_signals_panel(session: Session, username: str, since: datetime | None) -> list[dict]:
    """"New Signals on their buildings or contractors" -- their own
    buildings via Opportunity.building_id, directly.

    Contractor-anchored: NOT covered by this panel today, disclosed
    rather than faked. app.contractors.buildings_past_service_life_near_
    contractor (the established "buildings near this contractor's yard"
    concept, Block 4B Item 6) returns plain dicts keyed by apn/address for
    an xlsx export, not RetrofitBuilding.id -- there is no cheap join back
    to a real building id from it today, and re-deriving its haversine
    query a second way just to recover ids would duplicate real logic for
    one panel. Flagged as the natural next step, not silently built.

    since=None (a user's first-ever Radar visit) returns every signal on
    their buildings, not none -- there is no "last visit" to compare
    against yet, and an empty first panel would look like nothing is
    happening when the honest answer is "we don't know your history
    yet.\""""
    from app.pipeline.signals_feed import unified_signals

    opportunities = _own_opportunities(session, username)
    building_ids = {o.building_id for o in opportunities if o.building_id}
    if not building_ids:
        return []

    signals = unified_signals(session)
    out = []
    for fs in signals:
        if fs.building_id not in building_ids:
            continue
        if since is not None and (fs.trigger_date is None or fs.trigger_date < since):
            continue
        out.append({
            "building_id": fs.building_id, "trigger_type": fs.trigger_type.value,
            "trigger_date": fs.trigger_date, "evidence": fs.evidence,
        })
    return sorted(out, key=lambda r: r["trigger_date"] or datetime.min, reverse=True)


def weakest_why_panel(session: Session, username: str) -> list[dict]:
    """Their own Opportunities, weakest-why first -- same ranking Pipeline
    itself sorts by (app.pipeline.reason_block.weakest_why_rank), scoped
    to owner_user == username. No suggested calls, no ranking beyond
    "here is your own book, ordered" -- this item's own "they decide.\""""
    from app.models import Account, Ab869Plan, ReasonBlock
    from app.pipeline.reason_block import weakest_of, weakest_why_rank

    opportunities = _own_opportunities(session, username)
    if not opportunities:
        return []
    opp_ids = [o.id for o in opportunities]
    reason_blocks = session.exec(select(ReasonBlock).where(ReasonBlock.opportunity_id.in_(opp_ids))).all()
    blocks_by_opp: dict[int, list] = {}
    for rb in reason_blocks:
        blocks_by_opp.setdefault(rb.opportunity_id, []).append(rb)

    account_ids = [o.account_id for o in opportunities if o.account_id]
    building_ids = [o.building_id for o in opportunities if o.building_id]
    accounts = {a.id: a for a in session.exec(select(Account).where(Account.id.in_(account_ids)))} if account_ids else {}
    buildings = {b.id: b for b in session.exec(select(RetrofitBuilding).where(RetrofitBuilding.id.in_(building_ids)))} if building_ids else {}

    rows = []
    for o in opportunities:
        strengths = [b.strength for b in blocks_by_opp.get(o.id, [])]
        account_or_building = (
            accounts[o.account_id].name if o.account_id in accounts else
            (buildings[o.building_id].address or buildings[o.building_id].apn) if o.building_id in buildings else
            "ABSTAIN"
        )
        rows.append({
            "opportunity_id": o.id, "account_or_building": account_or_building,
            "weakest_why": weakest_of(strengths).value if len(strengths) == 3 else None,
            "sort_key": weakest_why_rank(strengths) if len(strengths) == 3 else (99, 99),
        })
    rows.sort(key=lambda r: r["sort_key"])
    return rows


def deadline_exposure_panel(session: Session, username: str) -> list[dict]:
    """Deadline exposure across their Opportunities -- app.pipeline.
    deadlines.deadlines_by_regulation() filtered to rows whose source_id
    matches a building_id or facility_perm_id one of their own
    Opportunities anchors on (source_id is str(building.id) for the three
    RetrofitBuilding-anchored regulations and plan.perm_id for AB 869 --
    see that function's own row-building code, reused here rather than
    re-derived a second way)."""
    from app.pipeline.deadlines import deadlines_by_regulation

    opportunities = _own_opportunities(session, username)
    own_ids = {str(o.building_id) for o in opportunities if o.building_id}
    own_ids |= {o.facility_perm_id for o in opportunities if o.facility_perm_id}
    if not own_ids:
        return []
    out = []
    for regulation, rows in deadlines_by_regulation(session).items():
        for row in rows:
            if row.source_id in own_ids:
                out.append(row)
    return sorted(out, key=lambda r: (r.date is None, r.date or datetime.max))


def notes_and_patterns_panel(session: Session, username: str) -> dict:
    """Their own Notes (author == username, most recent first) and their
    own patterns -- app.pipeline.team_patterns.team_patterns(user=...),
    the SAME functions the manager view uses, so the two views can never
    define "pattern" two different ways."""
    from app.pipeline.team_patterns import team_patterns

    notes = session.exec(
        select(DecisionNote).where(DecisionNote.author == username).order_by(DecisionNote.created_at.desc()).limit(20)
    ).all()
    return {"notes": notes, **team_patterns(session, user=username)}


def radar_data(session: Session, cfg: Config, username: str) -> dict:
    """Everything the /  route needs when this user's mode is "radar" --
    one call, same shape discipline as app.pipeline.notify.today_brief()
    for Guide. Also records this visit (see record_radar_visit) and
    returns the PREVIOUS visit time under "since", so the template can say
    plainly how far back "since last visit" reaches, or that this is the
    first visit."""
    since = record_radar_visit(session, username)
    return {
        "mode": "radar", "since": since,
        "new_signals": new_signals_panel(session, username, since),
        "weakest_why": weakest_why_panel(session, username),
        "deadline_exposure": deadline_exposure_panel(session, username),
        "notes_and_patterns": notes_and_patterns_panel(session, username),
        "customer_master_note": CUSTOMER_MASTER_NOTE,
    }
