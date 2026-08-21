"""Extraction schema shared by the LLM layer and the pipeline. Nulls mean 'not stated'."""
from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

# Imported, not duplicated: ScheduleEntryExtraction.role is classified into
# the SAME 13-role taxonomy /lines and /project already group DMG's own line
# card by (see app/accounts.py's own module comment for why 13 roles, not
# the 18 finer accounts.adjacency categories). One canonical list; a second,
# hand-copied one here would drift the moment a role is added or renamed.
from app.accounts import ROLE_ORDER

EXTRACTION_JSON_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "project_name": {"type": ["string", "null"]},
        "developer_or_owner": {"type": ["string", "null"]},
        "jurisdiction": {"type": ["string", "null"]},
        "county": {"type": ["string", "null"]},
        "state": {"type": ["string", "null"]},
        "street_address": {"type": ["string", "null"]},
        "apn_parcel": {"type": ["string", "null"]},
        "latitude": {"type": ["number", "null"]},
        "longitude": {"type": ["number", "null"]},
        "mw_it": {"type": ["number", "null"]},
        "mw_total": {"type": ["number", "null"]},
        "generator_count": {"type": ["integer", "null"]},
        "generator_hp_each": {"type": ["number", "null"]},
        "generator_kw_each": {"type": ["number", "null"]},
        # Only when the document itself splits the generator fleet into
        # critical/dedicated vs house — do not infer a split from an
        # undifferentiated count. See EXTRACT_SYSTEM.
        "generator_critical_count": {"type": ["integer", "null"]},
        "generator_critical_mw_each": {"type": ["number", "null"]},
        "generator_house_count": {"type": ["integer", "null"]},
        "generator_house_mw_each": {"type": ["number", "null"]},
        "building_sqft": {"type": ["number", "null"]},
        "acres": {"type": ["number", "null"]},
        "building_count": {"type": ["integer", "null"]},
        "cooling_type": {
            "type": ["string", "null"],
            "enum": ["air_cooled", "water_cooled", "evaporative", "closed_loop", "liquid", "unknown", None],
        },
        "water_acre_feet_per_year": {"type": ["number", "null"]},
        # The water-source facts a filing states, separate from whether the
        # project is publicly contested over water -- see
        # app/pipeline/waterrisk.py for why the distinction is the whole point.
        "water_source_stated": {"type": ["string", "null"]},
        # True only when the filing identifies a recycled/reclaimed/non-potable
        # supply (purple pipe, tertiary-treated effluent, NPW, "recycled water
        # from [district]"). False only when the filing explicitly states a
        # potable/municipal-domestic source with no recycled component. Null
        # when water source is not discussed -- never guessed from context.
        "water_reclaimed_identified": {"type": ["boolean", "null"]},
        # As stated, with its units -- a WUE figure (e.g. "0.20 L/kWh"), a
        # stated daily/annual use volume, or similar. Free text on purpose:
        # WUE is reported in incompatible units across filings (L/kWh, gal/MWh,
        # gal/day), and forcing a single numeric field would silently drop or
        # misrepresent whichever unit the filing actually used.
        "water_use_efficiency_stated": {"type": ["string", "null"]},
        # True only when the document itself records public opposition,
        # objection, or a hearing continuance citing water use/availability.
        # This is NOT "is water controversial in general" -- only what this
        # specific filing states happened. Null when not discussed.
        "water_opposition_stated": {"type": ["boolean", "null"]},
        # Drives the sqft-per-ton band for industrial buildings, where load is
        # envelope and process rather than IT. The spread across these types is
        # 50x — a cleanroom and a distribution warehouse of identical area are not
        # remotely the same job — so guessing it is worse than leaving it null.
        "facility_type": {
            "type": ["string", "null"],
            "enum": ["distribution_fulfillment", "warehouse_conditioned",
                     "light_manufacturing", "heavy_manufacturing", "cleanroom",
                     "office_rnd", "data_center", "unknown", None],
        },
        "stage": {
            "type": ["string", "null"],
            "enum": ["concept", "entitlement", "design", "permitting", "procurement",
                     "construction", "operating", "unknown", None],
        },
        "filing_type": {"type": ["string", "null"]},
        # Who selects the mechanical equipment -- see EXTRACT_SYSTEM's
        # delivery_method section in app/llm.py for the full extraction rules.
        # Null unless the filing itself names the delivery method; never
        # inferred from project type, agency, or stage.
        "delivery_method": {
            "type": ["string", "null"],
            "enum": ["design_bid_build", "design_build", "design_assist", "cm_at_risk",
                     "progressive_design_build", None],
        },
        "event_date": {"type": ["string", "null"], "description": "ISO date YYYY-MM-DD"},
        "named_people": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "title": {"type": ["string", "null"]},
                    "org": {"type": ["string", "null"]},
                    "phone": {"type": ["string", "null"]},
                    "email": {"type": ["string", "null"]},
                },
                "required": ["name"],
            },
        },
        "named_firms": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "role": {
                        "type": "string",
                        "enum": ["engineer_of_record", "gc", "mech_contractor", "developer",
                                 "consultant", "unknown"],
                    },
                },
                "required": ["name", "role"],
            },
        },
        "summary_one_line": {"type": "string"},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
    },
    "required": ["summary_one_line", "confidence"],
    "additionalProperties": False,
}

NUMERIC_FIELDS = [
    "latitude", "longitude", "mw_it", "mw_total", "generator_hp_each", "generator_kw_each",
    "generator_critical_mw_each", "generator_house_mw_each",
    "building_sqft", "acres", "water_acre_feet_per_year",
]
INT_FIELDS = ["generator_count", "generator_critical_count", "generator_house_count", "building_count"]
STR_FIELDS = [
    "project_name", "developer_or_owner", "jurisdiction", "county", "state", "street_address",
    "apn_parcel", "cooling_type", "filing_type", "facility_type", "delivery_method",
    "water_source_stated", "water_use_efficiency_stated",
]
# Nullable booleans -- tri-state (True / False / not stated), never defaulted
# to False for "not stated". First fields of this shape in the schema; see
# coerce_extraction's strict isinstance check, since a model returning a
# stray string/number here must null out, not silently coerce to a truthy bool.
BOOL_FIELDS = ["water_reclaimed_identified", "water_opposition_stated"]


def coerce_extraction(data: dict) -> dict:
    """Defensive cleanup of model output: coerce types, drop unknown keys, clamp confidence."""
    out: dict = {}
    for f in STR_FIELDS:
        v = data.get(f)
        out[f] = str(v).strip() if v not in (None, "") else None
    for f in NUMERIC_FIELDS:
        v = data.get(f)
        try:
            out[f] = float(v) if v is not None else None
        except (TypeError, ValueError):
            out[f] = None
    for f in INT_FIELDS:
        v = data.get(f)
        try:
            out[f] = int(v) if v is not None else None
        except (TypeError, ValueError):
            out[f] = None
    for f in BOOL_FIELDS:
        v = data.get(f)
        out[f] = v if isinstance(v, bool) else None
    out["stage"] = data.get("stage") or "unknown"
    out["event_date"] = data.get("event_date") or None
    out["named_people"] = [p for p in (data.get("named_people") or []) if isinstance(p, dict) and p.get("name")]
    out["named_firms"] = [f for f in (data.get("named_firms") or []) if isinstance(f, dict) and f.get("name")]
    out["summary_one_line"] = str(data.get("summary_one_line") or "")[:300]
    try:
        out["confidence"] = min(1.0, max(0.0, float(data.get("confidence", 0.0))))
    except (TypeError, ValueError):
        out["confidence"] = 0.0
    return out


class OutreachCallExtraction(BaseModel):
    """Structured facts pulled from ONE voice-captured call note. See
    app/pipeline/voice_capture.py.

    Real Pydantic validation, deliberately distinct from coerce_extraction
    above -- this is a review_queue PROPOSAL a human confirms before
    anything is written, so a field that fails to validate should raise and
    surface as an error on the capture, not silently clamp to null the way
    coerce_extraction does for the fully-automated extract pipeline. Fields
    the transcript doesn't clearly support are None -- the model is
    instructed never to infer them (see VOICE_CAPTURE_SYSTEM in app/llm.py)
    and this model does not backfill a guess either.

    Names are NOT resolved here -- contact_name/firm_name/
    project_or_building_name are exactly what the caller said, misheard
    proper nouns and all. app.voice_match fuzzy-matches these against
    Scout's own contacts/firms/projects and puts the resolution decision in
    front of the human; this model's job is only to say clearly what the
    transcript supports, not to guess which existing record it refers to.

    model_json_schema() (extra="forbid" -> additionalProperties: false) IS
    the Anthropic tool input_schema for constrained decoding -- see
    VOICE_CAPTURE_TOOL in app/llm.py. One schema, not two hand-maintained
    copies that could drift: the same definition both constrains what the
    model can emit and validates what it did emit.
    """
    model_config = ConfigDict(extra="forbid")

    contact_name: str | None = Field(
        default=None, description="The person's name exactly as stated, e.g. as heard by "
                                  "the caller -- do not correct or normalize spelling.")
    firm_name: str | None = Field(default=None, description="The company/firm name, if named.")
    project_or_building_name: str | None = Field(
        default=None, description="The project or building name/address discussed, if named.")
    outcome: str | None = Field(
        default=None, max_length=1000,
        description="What actually happened on this specific call -- 1-3 sentences, "
                    "specific to the deal/project discussed, not a generic call recap.")
    stage: str | None = Field(
        default=None, max_length=100,
        description="The deal/relationship stage as the caller described it (e.g. "
                    "'prequalified', 'bid submitted', 'awarded', 'lost') -- verbatim or "
                    "closely paraphrased, never inferred from context alone.")
    next_action: str | None = Field(default=None, max_length=300)
    next_action_date: date | None = Field(
        default=None, description="ISO date (YYYY-MM-DD), only if a specific date or "
                                  "clear timeframe was actually stated. Null otherwise.")
    confidence: float = Field(ge=0.0, le=1.0)

    @field_validator("contact_name", "firm_name", "project_or_building_name", "outcome",
                     "stage", "next_action", mode="before")
    @classmethod
    def _blank_to_none(cls, v):
        """The model sometimes emits "" or "unknown"/"not stated" instead of
        the null the schema asks for -- treat those as null rather than
        raising, since they carry the same meaning the schema intends."""
        if v is None:
            return None
        s = str(v).strip()
        return None if s == "" or s.lower() in ("unknown", "not stated", "n/a", "none") else s


class ScheduleEntryExtraction(BaseModel):
    """One equipment-schedule line item pulled from ONE uploaded project
    document (a drawing set, a Division 23 spec section, or mechanical
    sheets). See app/pipeline/schedule.py.

    Same discipline as OutreachCallExtraction above: real Pydantic
    validation, nulls over guesses, nothing corrected or normalized here
    (a tag is transcribed exactly as printed, not renumbered or reformatted)
    -- app.grounding-style checks run AFTER this against the document's own
    text, and that is where fabrication gets caught, not here.
    """
    model_config = ConfigDict(extra="forbid")

    tag: str = Field(
        description="The equipment tag exactly as printed on the schedule or drawing, e.g. "
                    "'AHU-1', 'RTU-3', 'CH-1' -- never invented, renumbered, or normalized.")
    equipment_type: str | None = Field(
        default=None,
        description="What kind of equipment this is, in the document's own words or the "
                    "schedule table's own heading (e.g. 'Rooftop Unit', 'Air Handling Unit', "
                    "'Chiller', 'Fan Coil Unit', 'VAV Box', 'Exhaust Fan'). Null if the "
                    "document doesn't state or clearly imply a type for this tag.")
    role: Literal[ROLE_ORDER] | None = Field(  # type: ignore[valid-type]
        default=None,
        description="Which of these 13 building roles equipment_type belongs to -- a "
                    "CLASSIFICATION judgment, not something to quote or ground: air_handling "
                    "(AHUs, RTUs, packaged units), cooling_generation (chillers, VRF/split "
                    "condensing units, heat pumps), heat_rejection (cooling towers, dry coolers, "
                    "condensers rejecting heat outdoors), air_distribution_terminal (VAV/CAV "
                    "boxes, diffusers, grilles, fan coils, chilled beams), fans_ventilation "
                    "(exhaust/supply fans, HVLS fans, air curtains), dampers_life_safety "
                    "(fire/smoke dampers, louvers), controls_valves (BAS, valves, sensors), "
                    "indoor_air_quality (filtration, UV, air cleaning), humidification "
                    "(humidifiers/dehumidifiers), acoustics_seismic (sound attenuation, seismic "
                    "restraint hardware), energy_recovery (ERVs, heat-recovery wheels/coils), "
                    "water_treatment, heating_specialty (boilers, unit heaters, and anything "
                    "that doesn't clearly fit the other 12). Null if equipment_type itself is "
                    "null or genuinely doesn't fit any of these (rare -- most HVAC equipment "
                    "fits one).")
    capacity_value: float | None = Field(
        default=None,
        description="The primary capacity/size number for this unit, exactly as printed -- "
                    "never converted, computed, or estimated from another field.")
    capacity_unit: str | None = Field(
        default=None,
        description="The unit capacity_value is stated in, exactly as printed (e.g. 'tons', "
                    "'MBH', 'kW', 'HP', 'gpm'). Null if capacity_value is null.")
    capacity_btuh: float | None = Field(
        default=None,
        description="ONLY fill this when the SAME headline capacity for this tag is stated a "
                    "SECOND time in BTU/H (or MBH -- multiply by 1000) elsewhere in that tag's "
                    "own entry, e.g. a spec block that states both 'Total Capacity 92,700 "
                    "BTU/H' and 'Nom Tons 8' for one unit -- these describe the same capacity "
                    "in two units, not two different facts, so both get recorded rather than "
                    "picked between. Do NOT fill this from a schedule column that is a "
                    "DIFFERENT quantity from capacity_value even if it is also in "
                    "BTU/H-like units -- e.g. a 'Cooling Coil Total Capacity (Net)' schedule "
                    "column is a net/rated performance number, not a restatement of a "
                    "'Nominal Tons' column, and must not be copied here. Null unless the "
                    "document itself restates the identical headline number in BTU/H.")
    airflow_cfm: float | None = Field(
        default=None,
        description="Airflow in CFM, only if the document states it in CFM directly for this "
                    "tag -- never converted from another unit or another field.")
    basis_of_design_manufacturer: str | None = Field(
        default=None,
        description="The manufacturer this document explicitly identifies as the basis of "
                    "design, or the specified/named manufacturer, for THIS SPECIFIC tag. Null "
                    "if the document lists multiple manufacturers for this tag with none "
                    "singled out, or if a manufacturer is named but not tied to this tag.")
    approved_equals: list[str] = Field(
        default_factory=list,
        description="Manufacturers this document explicitly lists as an approved equal / "
                    "acceptable substitute / acceptable manufacturer / 'or equal' for THIS "
                    "tag (or for the equipment category this tag belongs to, if the document "
                    "states equals at that level rather than per-tag). Empty list if none are "
                    "named -- never inferred from general industry knowledge.")
    source_quote: str = Field(
        description="A short (under 150 characters) VERBATIM excerpt copied exactly from the "
                    "document -- character for character, not paraphrased -- that shows this "
                    "row's tag and supports its key data. This is the proof the row came from "
                    "the text; a row without a real quote from the document is not usable.")
    source_page: int | None = Field(
        default=None,
        description="The page number (1-indexed, within the pages you were shown) this row "
                    "appears on, if you can determine it. Null if not determinable.")
    confidence: float = Field(
        ge=0.0, le=1.0,
        description="How clearly the document supports this specific row. Score low for "
                    "anything read from a rotated/merged table cell, an abbreviation whose "
                    "meaning you had to infer, or a row split across a page break or column.")

    @field_validator("tag", "source_quote", mode="before")
    @classmethod
    def _strip_required(cls, v):
        return str(v).strip() if v is not None else v

    @field_validator("equipment_type", "capacity_unit", "basis_of_design_manufacturer", mode="before")
    @classmethod
    def _blank_to_none(cls, v):
        """Same reasoning as OutreachCallExtraction._blank_to_none above --
        the model sometimes emits '' or 'unknown'/'n/a' instead of the null
        the schema asks for."""
        if v is None:
            return None
        s = str(v).strip()
        return None if s == "" or s.lower() in ("unknown", "not stated", "n/a", "none") else s


class EquipmentScheduleExtraction(BaseModel):
    """Every equipment-schedule row found in ONE uploaded project document.
    See ScheduleEntryExtraction and app/pipeline/schedule.py."""
    model_config = ConfigDict(extra="forbid")

    entries: list[ScheduleEntryExtraction] = Field(
        default_factory=list,
        description="One entry per distinct equipment tag found in an equipment schedule "
                    "table, drawing callout, or spec section. Empty list if the document "
                    "contains no equipment schedule at all -- never invent entries from a "
                    "general narrative mention of equipment with no tag or schedule behind it.")
