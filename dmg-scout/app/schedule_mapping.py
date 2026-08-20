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
from app.models import CompetitorLine, ProductLine, ScheduleEntry
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
    return LineRef(id=line.id, name=line.name, firm=line.firm)


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
