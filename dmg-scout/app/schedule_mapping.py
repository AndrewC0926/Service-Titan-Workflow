"""Map an extracted equipment schedule onto DMG's own line card -- for each
tag, which of our lines could serve it, whether a competitor or one of our
own lines holds the basis of design, and which of our lines are named as an
approved equal. See app/pipeline/schedule.py for how a tag gets here and
app/accounts.py for ROLE_ORDER/ProductLine.building_role, the same 13-role
taxonomy this reuses rather than re-deriving.

Reverse of app.accounts.line_offering_by_role: that function starts from a
PROJECT's category/facility_type and asks what the card offers a building
like this in general. This starts from an ACTUAL SCHEDULED TAG on THIS
project's own attached document and asks the narrower, sharper question a
document makes possible: for this specific unit, who's already spec'd, and
is one of ours sitting right there as an acceptable substitute.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from sqlmodel import Session, select

from app.accounts import ROLE_LABELS
from app.competitors import competing_lines_by_role
from app.models import CompetitorLine, ProductLine, ProductLineBranch, Project, RepFirm, ScheduleEntry
from app.normalize import normalize_name

# Ranked so a template/CLI can sort "what to act on first" without its own
# copy of this ordering -- see TagMapping.status's docstring for what each
# value means.
STATUS_ORDER = ("actionable_equal", "gap_no_equal", "no_line_for_role", "we_hold_bod", "unclassified")

STATUS_LABELS = {
    "actionable_equal": "Competitor spec'd, we're an approved equal",
    "gap_no_equal": "Competitor spec'd, no equal of ours named",
    "no_line_for_role": "We carry nothing for this role",
    "we_hold_bod": "We already hold the basis of design",
    "unclassified": "Could not classify a role for this tag",
}


@dataclass
class LineRef:
    id: int
    name: str
    firm: str
    existence_verified: bool = True


@dataclass
class TagMapping:
    entry_id: int
    tag: str
    equipment_type: str | None
    role: str | None
    needs_review: bool
    capacity_value: float | None
    capacity_unit: str | None
    basis_of_design_manufacturer: str | None
    approved_equals: list[str]
    our_lines_for_role: list[LineRef] = field(default_factory=list)
    bod_is_ours: LineRef | None = None
    equal_lines_that_are_ours: list[LineRef] = field(default_factory=list)
    competing_lines: list[CompetitorLine] = field(default_factory=list)
    status: str = "unclassified"

    @property
    def role_label(self) -> str | None:
        return ROLE_LABELS.get(self.role) if self.role else None

    @property
    def status_label(self) -> str:
        return STATUS_LABELS[self.status]


def _line_ref(line: ProductLine) -> LineRef:
    return LineRef(id=line.id, name=line.name, firm=line.firm, existence_verified=line.existence_verified)


def _match_manufacturer(name: str | None, lines_by_norm: dict[str, ProductLine]) -> ProductLine | None:
    """Same normalize_name() the line card itself is keyed on
    (app.accounts.seed_product_lines) -- a manufacturer string extracted
    from a schedule matches a ProductLine only if it normalizes to the
    exact same key that line's own name does. No fuzzy/partial matching:
    an extracted name that doesn't reduce to one of our 70 lines is simply
    not one of ours, not a maybe."""
    if not name:
        return None
    return lines_by_norm.get(normalize_name(name))


def map_project_schedule_to_line_card(session: Session, project_id: int) -> list[TagMapping]:
    """One TagMapping per ScheduleEntry attached to `project_id` (across
    every document), ordered the way they were extracted (by tag). Purely a
    read/compute -- writes nothing, so it's always safe to call from a page
    render and always reflects the CURRENT line card and CURRENT schedule
    entries, not a snapshot taken at extraction time."""
    entries = session.exec(
        select(ScheduleEntry).where(ScheduleEntry.project_id == project_id)
        .order_by(ScheduleEntry.tag)).all()
    if not entries:
        return []

    all_lines = session.exec(select(ProductLine)).all()
    lines_by_role: dict[str, list[ProductLine]] = {}
    lines_by_norm: dict[str, ProductLine] = {}
    for line in all_lines:
        # our_lines_for_role is a RECOMMENDATION ("consider this line") --
        # a line whose existence itself could not be confirmed (VU Flow
        # Environmental, 2026-08-09 research) is excluded from it, per
        # ProductLine.existence_verified's own docstring. lines_by_norm
        # (matching what a document literally names as its own basis of
        # design/approved equal) is NOT filtered here: that is a fact about
        # the document, independent of whether Scout can independently
        # confirm the company.
        if line.existence_verified:
            lines_by_role.setdefault(line.building_role, []).append(line)
        lines_by_norm[line.name_norm] = line
    competing_by_role = competing_lines_by_role(session)

    out: list[TagMapping] = []
    for e in entries:
        our_lines_for_role = [_line_ref(l) for l in lines_by_role.get(e.role, [])]
        bod_line = _match_manufacturer(e.basis_of_design_manufacturer, lines_by_norm)
        equal_lines = []
        seen_ids = set()
        for name in e.approved_equals or []:
            matched = _match_manufacturer(name, lines_by_norm)
            if matched and matched.id not in seen_ids:
                equal_lines.append(_line_ref(matched))
                seen_ids.add(matched.id)

        if not e.role:
            status = "unclassified"
        elif bod_line:
            status = "we_hold_bod"
        elif not our_lines_for_role:
            status = "no_line_for_role"
        elif equal_lines:
            status = "actionable_equal"
        else:
            status = "gap_no_equal"

        out.append(TagMapping(
            entry_id=e.id, tag=e.tag, equipment_type=e.equipment_type, role=e.role,
            needs_review=e.needs_review, capacity_value=e.capacity_value,
            capacity_unit=e.capacity_unit,
            basis_of_design_manufacturer=e.basis_of_design_manufacturer,
            approved_equals=list(e.approved_equals or []),
            our_lines_for_role=our_lines_for_role,
            bod_is_ours=_line_ref(bod_line) if bod_line else None,
            equal_lines_that_are_ours=equal_lines,
            competing_lines=competing_by_role.get(e.role, []) if e.role else [],
            status=status,
        ))
    return out


def actionable(mappings: list[TagMapping]) -> list[TagMapping]:
    """The list worth acting on: a competitor holds the basis of design and
    one of our own lines is named, in the same document, as an acceptable
    equal -- everything needed to go ask for the substitution is already on
    the page."""
    return [m for m in mappings if m.status == "actionable_equal"]


# ---- displacement: the reachable signal from a real equipment schedule ----
#
# actionable_equal (above) needs a document that BOTH names a basis of
# design AND names an approved equal in the same breath -- a spec-section
# construct. Scout has now measured, across three independent real
# documents/sources, that this combination is structurally rare in what a
# rep can obtain for free: a 287-page real public bid spec named zero
# manufacturers (performance-only, FAR 11.104/11.105), a Division 23 master
# template named a basis of design but had no actual tags to attach it to,
# and the SAM.gov Division-23 sweep found zero manufacturer mentions across
# 66 solicitations for the same FAR reason. The document class that DOES
# reliably name a real manufacturer + model is the equipment schedule on
# the drawings (confirmed: 59 of 62 real rows on the rooftop-unit schedule
# grounded a basis of design) -- but a schedule's own "Manufacturer" column
# is not spec-substitution language, so it essentially never also names an
# equal. Displacement is the question a schedule alone CAN answer: who
# holds this basis of design, and do we carry something in the same role --
# joined against Scout's own independently-researched competitor line-card
# map (app.competitors, Phase 5: 7 confirmed rep firms, 66 line
# assignments, 61 confirmed), not against document-internal language.
BRANCH_BY_COUNTY = {
    # See config.yaml's line_card_branches comment (2026-08-20): these 5
    # branches' cards are documented there by county/city LITERALLY, not by
    # inferred regional proximity. Kern is not Fresno even though both sit
    # in the Central Valley; Orange/Riverside/San Bernardino/Imperial are
    # not any of the 5 -- those counties resolve to no branch, on purpose,
    # rather than guessing the nearest one.
    "los angeles": "DMG Los Angeles",
    "san diego": "DMG San Diego",
    "san luis obispo": "DMG Central Coast",
    "fresno": "DMG Central Valley",
}

COMPETITOR_STATE_LABELS = {
    "rep_firm": "Rep firm",
    "factory_direct": "Factory direct -- no rep to displace",
    "unknown": "Unknown -- no competitive research on file",
}


@dataclass
class BranchResolution:
    branch: str | None      # one of the 5 branch names, or None if unresolved
    note: str | None        # explains an unresolved branch; None when resolved


def resolve_branch(project: Project) -> BranchResolution:
    """Never a guess: only Hawaii (by project.state) and the 4 CA counties
    literally named in config.yaml's line_card_branches comment resolve to
    a branch. Everything else says so and falls back to the company-wide
    card rather than picking the geographically nearest branch."""
    if project.state and project.state.strip().upper() == "HI":
        return BranchResolution("DMG Hawaii", None)
    county = (project.county or "").strip().lower()
    branch = BRANCH_BY_COUNTY.get(county)
    if branch:
        return BranchResolution(branch, None)
    if not project.county:
        return BranchResolution(
            None, "Project has no county on file -- branch coverage cannot be resolved; "
                 "showing the full, company-wide card instead.")
    return BranchResolution(
        None, f"{project.county} does not literally match one of the 5 branches with a card on "
             f"file (Los Angeles, San Diego, San Luis Obispo/Central Coast, Fresno/Central Valley, "
             f"Hawaii) -- showing the full, company-wide card rather than guessing which branch's "
             f"territory this county falls under.")


def _match_competitor(bod_name: str, competing_lines: list[CompetitorLine]) -> CompetitorLine | None:
    """Same discipline as _match_manufacturer above: exact normalized match
    first. CompetitorLine sometimes carries a disambiguating qualifier
    after the brand -- confirmed in Scout's own Phase 5 data, e.g. "YORK
    Applied (chillers, heat pumps)" and "YORK Applied (AHUs, DOAS, ERV, fan
    coils, WSHP)" as two separate role-scoped rows for the same
    manufacturer -- so a bare "YORK" on a schedule is matched against the
    FIRST WORD of each candidate's normalized name, not a substring search
    anywhere in it. `competing_lines` is already scoped to this tag's own
    role by the caller (TagMapping.competing_lines), so there is nothing
    here that could match a same-named competitor line filed under a
    different role."""
    norm = normalize_name(bod_name)
    if not norm:
        return None
    for cl in competing_lines:
        if normalize_name(cl.manufacturer) == norm:
            return cl
    for cl in competing_lines:
        tokens = normalize_name(cl.manufacturer).split()
        if tokens and tokens[0] == norm:
            return cl
    return None


def _county_covered(cl: CompetitorLine, project: Project) -> bool:
    """True when this competitor row's own confirmed coverage doesn't rule
    out the project's county. An empty covered_counties means no
    county-level research was ever done for this row -- treated as "no
    restriction to apply", NOT as "covers everywhere" (see CompetitorLine's
    docstring), so that returns True same as before this field existed. A
    non-empty list is a real, sourced boundary (e.g. Sigler SoCal
    Engineering names 5 of Scout's 7 territory counties and excludes
    Imperial and Kern) -- a project whose county isn't in that list, or
    that has no county on file at all, is NOT confirmed covered and must
    not resolve to this rep firm."""
    if not cl.covered_counties:
        return True
    county = (project.county or "").strip().lower()
    if not county:
        return False
    return county in {c.strip().lower() for c in cl.covered_counties}


@dataclass
class DisplacementRow:
    entry_id: int
    tag: str
    role: str
    needs_review: bool
    bod_manufacturer: str
    competitor_state: str              # rep_firm | factory_direct | unknown
    competitor_rep_firm: str | None    # set only when competitor_state == rep_firm
    competitor_unconfirmed: bool       # the matched CompetitorLine's own status == unconfirmed
    our_lines: list[LineRef]           # branch-restricted when branch resolved; company-wide + branch_note otherwise
    branch: str | None
    branch_note: str | None
    role_gap: bool                     # True when our_lines is empty -- a card gap, not a call to make

    @property
    def role_label(self) -> str:
        return ROLE_LABELS.get(self.role, self.role)

    @property
    def competitor_state_label(self) -> str:
        label = COMPETITOR_STATE_LABELS[self.competitor_state]
        if self.competitor_state == "rep_firm":
            label = f"{self.competitor_rep_firm} (rep firm)"
        if self.competitor_unconfirmed:
            label += " -- unconfirmed"
        return label


def resolve_displacement(session: Session, project: Project,
                         mappings: list[TagMapping]) -> list[DisplacementRow]:
    """One DisplacementRow per tag where a COMPETITOR (not us) holds a
    grounded basis of design. Deliberately excludes: tags with no grounded
    basis_of_design_manufacturer at all (nothing to join -- null over
    inference), tags where we already hold the basis of design
    (m.bod_is_ours -- nothing to displace), and tags with no classified
    role (no role to match a line or a branch card against)."""
    resolution = resolve_branch(project)
    branch_covered_ids: set[int] | None = None
    if resolution.branch:
        rows = session.exec(
            select(ProductLineBranch).where(
                ProductLineBranch.branch == resolution.branch,
                ProductLineBranch.status == "confirmed_covered")
        ).all()
        branch_covered_ids = {r.product_line_id for r in rows}

    rep_firm_names: dict[int, str] = {}

    def _rep_firm_name(rep_firm_id: int | None) -> str:
        if rep_firm_id is None:
            return "unknown rep firm"
        if rep_firm_id not in rep_firm_names:
            firm = session.get(RepFirm, rep_firm_id)
            rep_firm_names[rep_firm_id] = firm.name if firm else "unknown rep firm"
        return rep_firm_names[rep_firm_id]

    out: list[DisplacementRow] = []
    for m in mappings:
        if not m.basis_of_design_manufacturer or m.bod_is_ours or not m.role:
            continue

        cl = _match_competitor(m.basis_of_design_manufacturer, m.competing_lines)
        if cl is not None and not _county_covered(cl, project):
            cl = None
        if cl is None:
            competitor_state, competitor_rep_firm, competitor_unconfirmed = "unknown", None, False
        else:
            competitor_unconfirmed = cl.status == "unconfirmed"
            if cl.channel == "factory_direct":
                competitor_state, competitor_rep_firm = "factory_direct", None
            else:
                competitor_state = "rep_firm"
                competitor_rep_firm = _rep_firm_name(cl.rep_firm_id)

        our_lines = (
            [l for l in m.our_lines_for_role if l.id in branch_covered_ids]
            if branch_covered_ids is not None else m.our_lines_for_role
        )

        out.append(DisplacementRow(
            entry_id=m.entry_id, tag=m.tag, role=m.role, needs_review=m.needs_review,
            bod_manufacturer=m.basis_of_design_manufacturer,
            competitor_state=competitor_state, competitor_rep_firm=competitor_rep_firm,
            competitor_unconfirmed=competitor_unconfirmed,
            our_lines=our_lines, branch=resolution.branch, branch_note=resolution.note,
            role_gap=len(our_lines) == 0,
        ))
    return out


def role_gaps(rows: list[DisplacementRow]) -> list[DisplacementRow]:
    """A competitor holds the basis of design and we carry nothing in that
    role (at this project's branch, if resolved) -- not a call to make, a
    line-card gap worth telling Andy about, kept out of the displaceable
    list below so the two never get read as the same kind of finding."""
    return [r for r in rows if r.role_gap]


def displaceable(rows: list[DisplacementRow]) -> list[DisplacementRow]:
    """A competitor holds the basis of design and we carry at least one
    line in that same role -- the reachable, schedule-only signal: go find
    out whether that line was ever actually considered."""
    return [r for r in rows if not r.role_gap]
