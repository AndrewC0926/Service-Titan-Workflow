"""Extraction schema shared by the LLM layer and the pipeline. Nulls mean 'not stated'."""
from __future__ import annotations

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
    "apn_parcel", "cooling_type", "filing_type", "facility_type",
]


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
