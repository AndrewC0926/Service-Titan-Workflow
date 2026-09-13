"""Block 4B-prep Item 3 (line map rewritten in Block 4B-prep-3 Item 2):
eligible lines for buildings and facilities.

A direct, named map from a building's EQUIPMENT CLASS to the specific
line(s) DMG's own real card assigns to it (EQUIPMENT_CLASS_TO_LINE_NAMES,
matched by ProductLine.name_norm) -- NOT a role/category lookup, which the
real card measurably breaks (see that map's own comment: split_dx and
chiller sharing a "cooling_generation" role tag let a chiller line get
offered as a split-DX replacement). The OSP register is still reused
exactly as the rest of Scout already uses it (ProductLine.oshpd_osp): for
an HCAI-governed (hospital) facility, "eligible" means a line with a
current OSP, not just a line named for the class -- same rule
app.accounts.line_offering_by_role and app.pipeline.hcai.
hospital_capability_gaps already enforce for project-sourced signals.

EQUIPMENT CLASS SOURCES, measured directly, not assumed:

- RetrofitBuilding.equipment_type (app.pipeline.retrofit.infer_equipment_type)
  is permit-verified TEXT for the "recently_active" population only -- one
  of eight values: packaged_rooftop, split_dx, water_cooled_chiller,
  air_cooled_chiller, boiler, cooling_tower, air_handling_unit,
  vav_terminal, or null. The "replacement_candidate" population (the ONLY
  population unified_signals() currently sources -- see
  app.pipeline.signals_feed._retrofit_replacement_candidates) has
  equipment_type ALWAYS null BY DESIGN: absence of a permit is the
  signal, so there is no permit text to infer equipment from. This means
  RETROFIT_TYPE_TO_CLASS below has REAL rows to map today, but none of
  them currently reach a live Signal -- disclosed, not silently worked
  around by widening _retrofit_replacement_candidates' own population,
  which was not asked for here.

- Ab802Building carries NO equipment-class-bearing field at all -- checked
  directly (its own fields are property USE types and fuel-consumption
  totals for energy benchmarking, e.g. primary_property_type,
  all_property_use_types -- never HVAC equipment identity). AB 802
  equipment-class coverage is therefore 0% by construction, not a gap in
  this module's own mapping logic.

- Ab869Plan/HospitalBuilding (the ab869_plan signal source, HCAI-governed)
  likewise carry no equipment-class field -- verified the same way.
  osp_required=True is still the correct rule for these sources should a
  real equipment class ever become knowable for one (a future HCAI-side
  equipment export, say); it is written here on principle, not because
  today's data can exercise it.

vav_terminal has NO home in EquipmentClass's values (rooftop_packaged/
split_dx/vrf/water_source_heat_pump/chiller/cooling_tower/boiler/ahu/
unknown) -- a VAV terminal is a distribution/mixing box, not any of those
central-plant or unitary equipment types. Mapped to `unknown` (ABSTAIN) on
the same never-guess discipline as everywhere else, not forced into `ahu`
just because both touch air handling. water_source_heat_pump (added
Block 4B-prep-3, this module's own follow-up correction) has no home in
RETROFIT_TYPE_TO_CLASS either -- same "defined, unreached by any real
retrofit permit today" status as `vrf` below.

No retrofit permit today can ever classify as `vrf` either: infer_
equipment_type's own keyword patterns (app/pipeline/retrofit.py) have no
VRF pattern -- split_dx's regex ("split systems", "heat pumps") is the
closest match a real permit description hits. `vrf` exists in
EQUIPMENT_CLASS_TO_ROLES purely because the item's own value list names it
and because app.pipeline.hcai's existing "vrf_split rolls up to
cooling_generation" precedent gives it an unambiguous role -- not because
any building today can be classified into it.
"""
from __future__ import annotations

from enum import Enum

from sqlmodel import Session, select

from app.models import Ab802Building, ProductLine, RetrofitBuilding


class EquipmentClass(str, Enum):
    rooftop_packaged = "rooftop_packaged"
    split_dx = "split_dx"
    vrf = "vrf"
    # Block 4B-prep-3 Item 2 correction: a real, distinct class on DMG's
    # card, not a split_dx/vrf synonym -- ClimateMaster is a water-source
    # heat pump line, never a mini-split/VRF one (see this module's own
    # correction note on EQUIPMENT_CLASS_TO_LINE_NAMES below). No
    # RetrofitBuilding.equipment_type value maps to this today (the same
    # "defined on principle, unreached by any real permit" status vrf
    # already has -- see module docstring) -- added because Andrew named
    # it directly, not because today's data can exercise it.
    water_source_heat_pump = "water_source_heat_pump"
    chiller = "chiller"
    cooling_tower = "cooling_tower"
    boiler = "boiler"
    ahu = "ahu"
    unknown = "unknown"  # ABSTAIN -- no eligible-lines lookup is ever done for this value


# RetrofitBuilding.equipment_type (app.pipeline.retrofit.infer_equipment_type's
# own eight values) -> EquipmentClass. water_cooled_chiller and
# air_cooled_chiller both collapse to `chiller` -- this item's own value
# list has one chiller class, not two, and DMG's line card does not carry
# a water/air-cooled distinction at the building_role level either.
RETROFIT_TYPE_TO_CLASS: dict[str, EquipmentClass] = {
    "packaged_rooftop": EquipmentClass.rooftop_packaged,
    "split_dx": EquipmentClass.split_dx,
    "water_cooled_chiller": EquipmentClass.chiller,
    "air_cooled_chiller": EquipmentClass.chiller,
    "boiler": EquipmentClass.boiler,
    "cooling_tower": EquipmentClass.cooling_tower,
    "air_handling_unit": EquipmentClass.ahu,
    "vav_terminal": EquipmentClass.unknown,  # no home in this item's class list -- see module docstring
}


def equipment_class_from_retrofit_type(equipment_type: str | None) -> EquipmentClass:
    if equipment_type is None:
        return EquipmentClass.unknown
    return RETROFIT_TYPE_TO_CLASS.get(equipment_type, EquipmentClass.unknown)


# Block 4B-prep-3 Item 2: DMG's own real line card, named directly by
# Andrew -- NOT the generic building_role/category lookup this map
# replaces (that lookup, EQUIPMENT_CLASS_TO_ROLES, was removed outright,
# not left dead). The old role-based lookup was measurably wrong on the
# real card: split_dx and chiller both
# resolved to "cooling_generation" and so both matched EVERY line tagged
# that role, including ClimaCool -- a chillers_cooling line -- being
# offered as a split-DX replacement it has no business being offered for
# (confirmed directly: 6 of Block 4B-prep-2's ten promoted Opportunities
# had a split_dx building matched to "ClimaCool" as its eligible line).
# name_norm-matched, never role-matched, going forward.
#
# split_dx: LG only -- CORRECTED (was wrongly widened to every line
# sharing category='vrf_split' on the real card: ClimateMaster,
# Islandaire, Hitachi, Engineered Comfort -- an unconfirmed inference
# from a shared category tag, not a named fact, and it was wrong for at
# least ClimateMaster, confirmed directly by Andrew: LG's own single-zone
# and Multi V S products are what actually cover split_dx/vrf on this
# card; ClimateMaster is a water-source heat pump line, a real but
# DIFFERENT equipment class (see EquipmentClass.water_source_heat_pump).
# Islandaire/Hitachi/Engineered Comfort dropped too, not reassigned
# anywhere -- the same category tag that wrongly implied ClimateMaster
# belonged here gives no real basis for keeping the other three either,
# and this module never guesses past what's actually been confirmed.
# vrf: LG too -- "Multi V S" (named directly by Andrew) is LG's own real
# VRF product line, the same catalog row as split_dx's "single-zone"
# products, not a second, different line.
# water_source_heat_pump: ClimateMaster -- named directly by Andrew ("maps
# to water_source_heat_pump only").
# rooftop_packaged: AAON, LG -- named directly.
# chiller: ClimaCool -- named directly, "only where OSP is not required."
# ClimaCool's own oshpd_osp=False on the real card makes that fall out of
# the existing osp_required filter below automatically; no special case
# needed, and no second chiller line is added just to have one for the
# OSP-required branch (that branch correctly returns empty -- ABSTAIN --
# until DMG actually confirms an OSP-registered chiller line).
# cooling_tower: Marley -- "SPX/Marley" in the instruction; Marley is the
# catalog line, SPX is that line's parent company, not a separate card
# entry, so this maps to the one real row.
# boiler: no lines -- ABSTAIN until a boiler line is confirmed on the
# card, never guessed at Cambridge/Suburban/Markel/IEC (heating_specialty)
# standing in for one just because they share a role tag.
# ahu: Energy Labs, AAON, ClimateCraft -- named directly.
EQUIPMENT_CLASS_TO_LINE_NAMES: dict[EquipmentClass, tuple[str, ...]] = {
    EquipmentClass.rooftop_packaged: ("aaon", "lg"),
    EquipmentClass.split_dx: ("lg",),
    EquipmentClass.vrf: ("lg",),
    EquipmentClass.water_source_heat_pump: ("climatemaster",),
    EquipmentClass.chiller: ("climacool",),
    EquipmentClass.cooling_tower: ("marley",),
    EquipmentClass.boiler: (),
    EquipmentClass.ahu: ("energy labs", "aaon", "climatecraft"),
    EquipmentClass.unknown: (),
}


def eligible_lines_for_equipment_class(
    session: Session, equipment_class: EquipmentClass, osp_required: bool = False,
) -> list[ProductLine]:
    """Lines DMG's own real card assigns to this equipment class (see
    EQUIPMENT_CLASS_TO_LINE_NAMES -- matched by name_norm, not by a shared
    building_role/category tag). `unknown` and `boiler` (no confirmed
    boiler line on the card yet) both always return an empty list --
    ABSTAIN, never a guess. For an OSP-required (hospital) facility,
    "eligible" still means a line with a CURRENT OSP (ProductLine.
    oshpd_osp), same rule app.accounts.line_offering_by_role already
    enforces for project-sourced signals -- ClimaCool's own oshpd_osp=
    False is what makes "chiller -> ClimaCool only where OSP is not
    required" true, with no special case in this function for it."""
    names = EQUIPMENT_CLASS_TO_LINE_NAMES.get(equipment_class, ())
    if not names:
        return []
    lines = session.exec(
        select(ProductLine).where(ProductLine.name_norm.in_(names))
        .order_by(ProductLine.value_tier, ProductLine.name)
    ).all()
    if osp_required:
        lines = [line for line in lines if line.oshpd_osp is True]
    return lines


def equipment_class_coverage_report(session: Session) -> dict:
    """Measured, not assumed, coverage of the new EquipmentClass mapping
    across every real source that could carry equipment-class evidence
    today -- see module docstring for why AB 802's own share is 0% by
    construction rather than a bug in this report."""
    retrofit_rows = session.exec(select(RetrofitBuilding)).all()
    by_population: dict[str, dict] = {}
    for pop in ("recently_active", "replacement_candidate"):
        rows = [r for r in retrofit_rows if r.population == pop]
        classified = [equipment_class_from_retrofit_type(r.equipment_type) for r in rows]
        known = sum(1 for c in classified if c != EquipmentClass.unknown)
        by_class: dict[str, int] = {}
        for c in classified:
            by_class[c.value] = by_class.get(c.value, 0) + 1
        by_population[pop] = {
            "total": len(rows),
            "known_equipment_class": known,
            "coverage_pct": round(100 * known / len(rows), 1) if rows else 0.0,
            "by_class": by_class,
        }

    ab802_total = len(session.exec(select(Ab802Building.id)).all())
    return {
        "retrofit_buildings": by_population,
        "ab802_buildings": {
            "total": ab802_total,
            "known_equipment_class": 0,
            "coverage_pct": 0.0,
            "reason": "Ab802Building carries no equipment-class-bearing field at all "
                      "(property use types and fuel consumption only) -- see module docstring.",
        },
    }
