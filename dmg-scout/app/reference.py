"""Static field-reference content for the /reference sheet -- equipment,
formulas, abbreviations and role definitions a rep needs to hold a
conversation with an engineer without having designed the system.

Deliberately NOT pipeline-fed: no source, no staleness, nothing here can go
stale in the way a scraped signal can, so it carries no source-health entry,
no assumptions-register entry, and no "data as of" stamp. Basis is ASHRAE
(Handbook of Fundamentals for heat-transfer forms, 62.1 for ventilation,
the ASHRAE service-life database for the life ranges) -- see the citation in
reference.html's footer, which is the only place that basis needs to live.

TWO EXCEPTIONS are database-backed (see app/web/main.py's reference_index
route):

"pitches" (app.models.LinePitch) shows CONFIRMED pitches only, grouped by
role, plus a count of drafts still awaiting review. A draft (unreviewed,
generated) pitch never appears here -- this page is where a rep learns the
card, and an unverified generated sentence has no business being read back
as if it were confirmed. See app/pipeline/line_pitch.py's module docstring
for the generation discipline behind every row this tab can ever show.

"ahj_a2l" (app.models.AhjA2lGuidance) shows every row of the AHJ A2L
register -- see app/pipeline/ahj_a2l.py's module docstring for how it's
loaded (Phase A research, `scout seed-ahj-a2l`, quarterly manual refresh,
never a live fetcher). Unlike "pitches," this tab deliberately shows EVERY
status including NOT_REACHED (checked_at NULL) -- hiding the unfinished
rows would make 212-of-212 coverage look complete when 47 of them were
never actually checked; see that module's own docstring for why.

Content adapted from a standalone reference sheet the user hand-authored
(hvac-field-reference.html) into this app's own template and component
classes.
"""
from __future__ import annotations

TAB_ORDER = ("systems", "equipment", "numbers", "abbreviations", "roles", "pitches", "ahj_a2l")

TAB_LABELS = {
    "systems": "Systems",
    "equipment": "Equipment",
    "numbers": "Numbers",
    "abbreviations": "Abbreviations",
    "roles": "Roles",
    "pitches": "Pitches",
    "ahj_a2l": "AHJ A2L guidance",
}

# equipment_type (RetrofitBuilding.equipment_type, set by
# app/pipeline/retrofit.py:infer_equipment_type) -> tooltip text. Used
# wherever that field is already rendered elsewhere in the app (retrofit
# board, building detail, retrofit report) so the same abbreviation-def
# pattern the board's own column headers use also covers equipment terms.
EQUIPMENT_TOOLTIPS: dict[str, str] = {
    "packaged_rooftop": "Packaged rooftop unit (RTU) — fan, coil, compressor and "
        "condenser in one cabinet on a roof curb.",
    "split_dx": "Split system / mini-split — outdoor condensing unit paired with "
        "one or more indoor evaporator units, refrigerant piped directly between them.",
    "water_cooled_chiller": "Water-cooled chiller — makes chilled water using a "
        "water-cooled condenser; paired with a cooling tower.",
    "air_cooled_chiller": "Air-cooled chiller — makes chilled water using an "
        "air-cooled condenser; no cooling tower needed.",
    "boiler": "Boiler — makes hot water or steam for heating coils and reheat.",
    "cooling_tower": "Cooling tower — rejects a chiller's condenser heat to the "
        "atmosphere by evaporation.",
    "air_handling_unit": "Air handling unit (AHU) — fan and coil in a cabinet, "
        "fed by a central chilled/hot water plant.",
    "vav_terminal": "VAV terminal box — modulates airflow to a zone, often with "
        "a reheat coil.",
}


def equipment_tooltip(equipment_type: str | None) -> str:
    """title="" text for an equipment_type value; '' when there's nothing to say
    (unset, or a value that predates/postdates this table -- never guess)."""
    if not equipment_type:
        return ""
    return EQUIPMENT_TOOLTIPS.get(equipment_type, "")


# M-500 role table content, one row per app.accounts.ROLE_ORDER value (all 13,
# not just the 10 an earlier informal pass covered). "equipment" and
# "said_out_loud" are prose for a rep, not a repeat of the line card itself --
# see reference.html's footer disclaimer for the "verify before quoting" note
# that covers this table.
ROLE_REFERENCE: dict[str, dict[str, str]] = {
    "air_handling": {
        "equipment": "AHU, RTU, DOAS, custom units",
        "said_out_loud": "“How many CFM and what static?”",
    },
    "cooling_generation": {
        "equipment": "Chillers, thermal storage",
        "said_out_loud": "“What tonnage, and kW per ton?”",
    },
    "heat_rejection": {
        "equipment": "Cooling towers, fluid coolers, adiabatic",
        "said_out_loud": "“What is the approach, and can we go dry?”",
    },
    "air_distribution_terminal": {
        "equipment": "VAV boxes, diffusers, grilles",
        "said_out_loud": "“What is the NC rating at that throw?”",
    },
    "fans_ventilation": {
        "equipment": "Exhaust fans, lab exhaust",
        "said_out_loud": "“What is the discharge velocity?”",
    },
    "dampers_life_safety": {
        "equipment": "Fire, smoke, combination dampers",
        "said_out_loud": "“Is it UL 555 rated and does it need OSP?”",
    },
    "controls_valves": {
        "equipment": "VFDs, actuators, BMS points",
        "said_out_loud": "“What protocol does it speak?”",
    },
    "indoor_air_quality": {
        "equipment": "Filtration, UV, bipolar ionization",
        "said_out_loud": "“Do we hit ASHRAE 241?”",
    },
    "humidification": {
        "equipment": "Steam, ultrasonic and wetted-media humidifiers",
        "said_out_loud": "“What's the grain depression, and is it steam or evaporative?”",
    },
    "acoustics_seismic": {
        "equipment": "Isolation, restraint",
        "said_out_loud": "“Who is doing the OSHPD anchorage?”",
    },
    "energy_recovery": {
        "equipment": "Wheels, plate exchangers",
        "said_out_loud": "“What effectiveness are we crediting?”",
    },
    "water_treatment": {
        "equipment": "Chemical feed, filtration, softeners on boiler/condenser loops",
        "said_out_loud": "“What's the water treatment program — chemical or non-chemical?”",
    },
    "heating_specialty": {
        "equipment": "Boilers, unit heaters, radiant, heat-pump heating",
        "said_out_loud": "“What's the heating source, and is it electrification-ready?”",
    },
}
