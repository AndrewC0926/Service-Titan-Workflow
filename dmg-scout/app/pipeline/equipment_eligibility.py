"""Block 4B-prep Item 3: eligible lines for buildings and facilities.

A config map from a building's EQUIPMENT CLASS to the line-card roles it can
plausibly be sold into, reusing the SAME role/OSP eligibility engine the
rest of Scout already uses (app.accounts.ROLE_ORDER / ProductLine.
building_role / ProductLine.oshpd_osp) -- not a second one. For an
HCAI-governed (hospital) facility, "eligible" means a line with a current
OSP, not just a line that exists in the right role -- same rule
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

vav_terminal has NO home in the eight EquipmentClass values this item asks
for (rooftop_packaged/split_dx/vrf/chiller/cooling_tower/boiler/ahu/
unknown) -- a VAV terminal is a distribution/mixing box, not any of those
central-plant or unitary equipment types. Mapped to `unknown` (ABSTAIN) on
the same never-guess discipline as everywhere else, not forced into `ahu`
just because both touch air handling.

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


# EquipmentClass -> the app.accounts.ROLE_ORDER role(s) it is plausibly sold
# into -- built from the SAME CATEGORY_TO_ROLE table the rest of the line
# card already uses (app/accounts.py), not a new, second judgment call:
# rooftop_units/air_handling -> air_handling, vrf_split/chillers_cooling ->
# cooling_generation, cooling_towers -> heat_rejection, heaters ->
# heating_specialty. vrf and split_dx share cooling_generation on the same
# "vrf_split rolls up to cooling_generation" precedent app.pipeline.hcai.
# hospital_capability_gaps already established. `unknown` maps to no role
# at all -- ABSTAIN, never a guessed role.
EQUIPMENT_CLASS_TO_ROLES: dict[EquipmentClass, tuple[str, ...]] = {
    EquipmentClass.rooftop_packaged: ("air_handling",),
    EquipmentClass.split_dx: ("cooling_generation",),
    EquipmentClass.vrf: ("cooling_generation",),
    EquipmentClass.chiller: ("cooling_generation",),
    EquipmentClass.cooling_tower: ("heat_rejection",),
    EquipmentClass.boiler: ("heating_specialty",),
    EquipmentClass.ahu: ("air_handling",),
    EquipmentClass.unknown: (),
}


def eligible_lines_for_equipment_class(
    session: Session, equipment_class: EquipmentClass, osp_required: bool = False,
) -> list[ProductLine]:
    """Lines whose building_role matches this equipment class's role(s),
    reusing the OSP register (ProductLine.oshpd_osp) exactly as
    app.accounts.line_offering_by_role already does for project-sourced
    signals: for an OSP-required (hospital) facility, "eligible" means a
    line with a CURRENT OSP, not merely a line in the right role. `unknown`
    always returns an empty list -- ABSTAIN, never a guess at what role an
    unclassified building's equipment might occupy."""
    roles = EQUIPMENT_CLASS_TO_ROLES.get(equipment_class, ())
    if not roles:
        return []
    lines = session.exec(
        select(ProductLine).where(ProductLine.building_role.in_(roles))
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
