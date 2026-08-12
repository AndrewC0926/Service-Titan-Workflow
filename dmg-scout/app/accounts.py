"""Accounts module: line-card coverage for the ~100 dormant accounts, dollar-
ranked gap scoring against a tunable adjacency table, and replacement windows
off the existing ownership-branched service-life table. Three questions per
account — what they have, what they should have, what is forcing them to buy —
answered by three functions here: owned_categories/compute_gaps and
account_replacement_windows.

Deliberately separate from the project pipeline: nothing in this module reads
or writes Project, Signal, or anything under app/pipeline/. The only bridge is
Account.firm_id, an optional link to the existing Firm roster, used solely to
show live Scout projects naming this account on its brief.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime

from sqlmodel import Session, select

from app.config import Config
from app.firms import match_firm
from app.models import (
    ACTIVE_STATUSES,
    Account,
    AccountCoverage,
    Category,
    FacilityType,
    Firm,
    ProductLine,
    Project,
    ProjectFirm,
    ProjectSignal,
    SelectionTool,
    Signal,
    utcnow,
)
from app.normalize import normalize_name
from app.replacement import ServiceLife, UnknownEquipment, replacement_basis, service_life

log = logging.getLogger(__name__)

BOUGHT = "bought"
QUOTED_NOT_WON = "quoted_not_won"
NEVER_QUOTED = "never_quoted"
UNKNOWN = "unknown"
COVERAGE_STATUSES = (BOUGHT, QUOTED_NOT_WON, NEVER_QUOTED, UNKNOWN)

ACCOUNT_TYPES = (
    "mechanical_contractor", "service_contractor", "gc", "owner", "developer",
    "distributor", "engineer",
)


# ---- line card: role in a building ----------------------------------------
#
# The line card's PRIMARY organization is "what role does this fill in a
# building" — the way a rep actually thinks, and how lines combine on a job
# (AAON's rooftop pulls Titus diffusers and Yaskawa VFDs with it) — not the 18
# finer categories accounts.adjacency scores account gaps on above. Those
# categories keep doing exactly what they already do; this is a SEPARATE,
# coarser grouping layered on top for the /lines view only.
#
# ROLE_ORDER is the tab order on that page, in the order specified when the
# view was requested. ROLE_LABELS is the display text. CATEGORY_TO_ROLE is
# the default category -> role mapping; ROLE_OVERRIDE_BY_LINE corrects the
# handful of lines (by exact line_card `name`) whose actual product mix
# doesn't match their category's default role — e.g. Pottorff's category is
# air_distribution (grilles/registers/dampers as a group) but its own
# description is "fire/smoke dampers and louvers", which is a life-safety
# product, not a terminal-device one. Correct EITHER table here, then re-run
# `scout seed-lines` — same workflow as correcting category/value_tier in
# config.yaml, just in this file because there are only ~10 overrides against
# 70 lines and putting them in config.yaml would mean touching all 70 entries
# to add a mostly-redundant `role:` key. resolve_building_role() is what
# actually applies these at seed time.
ROLE_ORDER = (
    "air_handling", "cooling_generation", "heat_rejection", "air_distribution_terminal",
    "fans_ventilation", "dampers_life_safety", "controls_valves", "indoor_air_quality",
    "humidification", "acoustics_seismic", "energy_recovery", "water_treatment",
    "heating_specialty",
)

ROLE_LABELS = {
    "air_handling": "Air handling",
    "cooling_generation": "Cooling generation",
    "heat_rejection": "Heat rejection",
    "air_distribution_terminal": "Air distribution & terminal",
    "fans_ventilation": "Fans & ventilation",
    "dampers_life_safety": "Dampers & life safety",
    "controls_valves": "Controls & valves",
    "indoor_air_quality": "Indoor air quality",
    "humidification": "Humidification",
    "acoustics_seismic": "Acoustics & seismic",
    "energy_recovery": "Energy recovery",
    "water_treatment": "Water treatment",
    "heating_specialty": "Heating & specialty",
}

CATEGORY_TO_ROLE = {
    "rooftop_units": "air_handling",
    "air_handling": "air_handling",
    "vrf_split": "cooling_generation",
    "chillers_cooling": "cooling_generation",
    "cooling_towers": "heat_rejection",
    "controls": "controls_valves",
    "air_distribution": "air_distribution_terminal",
    "fans_exhaust": "fans_ventilation",
    "process_exhaust": "fans_ventilation",
    "filtration_iaq": "indoor_air_quality",
    "acoustics_vibration": "acoustics_seismic",
    "heaters": "heating_specialty",
    "humidification_dehumidification": "humidification",
    "specialty_cooling": "heating_specialty",
    "accessories": "air_distribution_terminal",
    "residential_light_commercial": "fans_ventilation",
    "water_treatment": "water_treatment",
    "monitoring_sensors": "controls_valves",
}

# name -> role, ONLY for lines that diverge from CATEGORY_TO_ROLE's default —
# see the module-level comment above for why these ten and not the other 60.
ROLE_OVERRIDE_BY_LINE = {
    "Aldes": "energy_recovery",                # "ventilation / ERV" -- the ERV is the point
    "ChangeAir": "energy_recovery",             # energy recovery ventilators, by name
    "Ventacity": "energy_recovery",             # "DOAS / ERV"
    "Heat Pipe Technology": "energy_recovery",  # passive heat-exchange recovery mechanism
    "LFSystems": "dampers_life_safety",         # lab/fume airflow containment is life-safety, not general BAS
    "Carel": "humidification",                  # "humidification & refrigeration controls" -- humidification led
    "MacroAir": "fans_ventilation",             # HVLS fans are fans, not specialty cooling
    "Barcol-Air": "air_distribution_terminal",  # chilled beams are a room terminal device
    "Pottorff": "dampers_life_safety",          # "fire/smoke dampers and louvers"
    "Berner": "fans_ventilation",               # air curtains are fan-driven air barriers
    "Ice-Cel": "cooling_generation",            # thermal ice storage is cooling-side load shifting, not heating/specialty
}


def resolve_building_role(name: str, category: str) -> str:
    return ROLE_OVERRIDE_BY_LINE.get(name) or CATEGORY_TO_ROLE.get(category, "heating_specialty")


# ---- line card: markets served ---------------------------------------------
#
# Which of these 8 a line plausibly sells into. Two tiers, never conflated
# -- see ProductLine.markets_served_source in app/models.py:
#   - "researched": config.yaml's `markets:` key on a line_card entry, each
#     one backed by a markets_basis citation to the manufacturer's own
#     literature (case study, named vertical page, explicit copy). The
#     source of truth; wins over MARKETS_BY_LINE below whenever a line has
#     been through this pass, including when the researched result is a
#     confirmed empty list.
#   - "legacy_guess": MARKETS_BY_LINE below, a first-pass table from before
#     the research pass existed, inferred from a line's stated description/
#     subcategory (e.g. BASX's "mission-critical/data center applications")
#     rather than a citable source. Kept only as a fallback for lines
#     nobody has researched yet -- never upgrade a line's result by editing
#     this table; research it into config.yaml's `markets:` key instead.
# A line with neither has an empty list and a null source: "not yet
# mapped", not "sells nowhere".
#
# Distinct on purpose from project matching (matching_projects_for_line
# below): Scout's own board only tracks data_center/industrial/esco
# new-construction, so a line marked "healthcare" here will never surface a
# live Scout project -- that is a fact about what Scout tracks, not about
# the line, and the /line page must say so rather than imply the gap is the
# line's fault.
MARKETS = (
    "data_center", "healthcare", "industrial_warehouse", "education",
    "hospitality", "labs", "office", "multifamily",
)

MARKET_LABELS = {
    "data_center": "Data center",
    "healthcare": "Healthcare",
    "industrial_warehouse": "Industrial & warehouse",
    "education": "Education",
    "hospitality": "Hospitality",
    "labs": "Labs",
    "office": "Office",
    "multifamily": "Multifamily",
}

MARKETS_BY_LINE = {
    "BASX": ["data_center"],
    "VU Flow Environmental": ["data_center", "labs"],
    "Marley": ["data_center"],
    "Recold": ["data_center"],
    "Strobic Air": ["labs"],
    "LFSystems": ["labs"],
    "Seresco": ["hospitality", "multifamily", "education"],
    "Islandaire": ["hospitality", "multifamily"],
    "Engineered Comfort": ["hospitality", "multifamily"],
    "Panasonic": ["multifamily", "hospitality"],
    "Delta Breez": ["multifamily", "hospitality"],
    "Broan NuTone": ["multifamily", "hospitality"],
    "Monoxivent": ["industrial_warehouse"],
    "Howden": ["industrial_warehouse"],
    "MacroAir": ["industrial_warehouse"],
    "Ice-Cel": ["industrial_warehouse"],
    "Cambridge": ["industrial_warehouse"],
    "Suburban": ["industrial_warehouse"],
    "Markel": ["industrial_warehouse"],
    "UVDI": ["healthcare"],
    "AtmosAir/Bioclimatic": ["healthcare", "education", "office"],
    "Cosatron": ["office", "education"],
    "Barcol-Air": ["office"],
}


# Lines whose CATEGORY (not role) is still a first-pass guess from the
# original seed (6 remain as of 2026-08-11; see NEEDS_VERIFICATION below),
# flagged "(best-guess categorization — confirm)" in their
# config.yaml description rather than a second boolean column -- the flag and
# the fact it's attached to live in the same place, so they can never drift
# apart. Read back out here once, at import-adjacent scope, rather than
# string-matching it three times across accounts.py/main.py/templates.
CATEGORY_BEST_GUESS_MARKER = "best-guess categorization"


def category_is_best_guess(line: ProductLine) -> bool:
    return CATEGORY_BEST_GUESS_MARKER in (line.description or "")


# 2026-08-11 review resolved 6 of the original 12 best-guess lines (VTS, PEP
# Filters, Recold, HCi, CRC corrected; Thermal Corp, DB, Hecoclima confirmed
# correct) plus Ice-Cel via ROLE_OVERRIDE_BY_LINE above. These six are still
# genuinely unresolved -- not re-researched, not re-guessed, left exactly as
# best-guess in config.yaml pending an actual answer from the manufacturer or
# a rep who knows the line. Do not silently correct these from inference;
# call the factory.
NEEDS_VERIFICATION = (
    "Engineered Comfort", "LFSystems", "Effectiv", "ChangeAir", "Suburban", "Cambridge",
)


# ---- UFC 4-010-06 applicability ---------------------------------------
#
# NOT a lookup -- a logic rule applied by hand to every line, per the
# charter: UFC 4-010-06's network/utility-infrastructure provisions reach
# anything with a factory controller or a network/BAS connection, and skip
# anything that's a passive mechanical device with no controller of its
# own. This is judgment applied to what each line's own stated
# subcategory/description says it is -- not a researched fact with a
# citable source the way OSHPD OSP or AHRI certification are, and the
# basis text says so plainly rather than dressing it up as sourced.
#
# A handful of lines sell BOTH a passive product and a controlled one
# under the same name (Titus/Nailor sell plain diffusers alongside DDC-
# actuated VAV terminals; Pottorff's fire/smoke dampers are typically
# actuator/BAS-monitored even though its line also includes plain
# louvers) -- these are marked True with a note that the passive portion
# of the line doesn't independently carry a controller, since "confirm
# per model" is the honest caveat a blanket per-line flag can't avoid.
#
# Re-derive by editing this table, not by researching a spec sheet --
# and always re-check which UFC clause a given contract actually invokes,
# since this table only answers "does it have a controller/network
# connection", not "does UFC 4-010-06 apply to this specific contract".
UFC_4_010_06_BY_LINE: dict[str, bool] = {
    # ---- True: factory controller or network/BAS connection -----------
    "AAON": True, "DB": True, "Energy Labs": True, "LG": True, "Marley": True,
    "BASX": True, "ClimateCraft": True, "Scott Springfield": True, "VTS": True,
    "ClimaCool": True, "VU Flow Environmental": True, "Geoclima": True,
    "ClimateMaster": True, "Hecoclima": True, "IEC": True, "Strobic Air": True,
    "Ebtron": True, "AtmosAir/Bioclimatic": True, "UVDI": True, "Cosatron": True,
    "Neptronic": True, "LFSystems": True, "Carel": True, "Cambridge": True,
    "Seresco": True, "Aldes": True, "MacroAir": True, "Islandaire": True,
    "ChangeAir": True, "Flow-Tech": True, "Howden": True, "Suburban": True,
    "Yaskawa": True, "Airzone": True, "Systemair": True, "Ventacity": True,
    "Titus": True, "Pottorff": True, "TCF/Twin City Fan": True, "Nailor": True,
    "Berner": True, "Specified Controls": True, "Effectiv": True, "Markel": True,
    "Monoxivent": True, "Hitachi": True, "Engineered Comfort": True,
    # ---- False: passive mechanical device, no controller of its own ---
    "Thermal Corp": False, "Recold": False, "Vibro-Acoustics": False,
    "PEP Filters": False, "Heat Pipe Technology": False, "Ice-Cel": False,
    "HCi": False, "Barcol-Air": False, "Ductsox": False,
    "Commercial Acoustics": False, "AJ Manufacturing": False, "CRC": False,
    "Anemostat": False, "Panasonic": False, "Young Regulator": False,
    "Seiho": False, "Canarm": False, "Penn Barry": False, "Delta Breez": False,
    "Broan NuTone": False, "Tuttle & Bailey": False, "Soler & Palau": False,
    "FanAm": False,
}

# Per-line notes for the handful where the True/False call needs the extra
# sentence -- mixed passive/controlled lines, or a judgment that could
# plausibly go the other way. Appended to the generic basis text below.
UFC_4_010_06_NOTES: dict[str, str] = {
    "Titus": "Line spans both plain diffusers/grilles (no controller) and DDC-actuated VAV terminals "
             "(networked) -- marked True for the VAV terminal portion; confirm per model.",
    "Nailor": "Line spans both plain diffusers and DDC-actuated VAV terminals -- marked True for the VAV "
              "terminal portion; confirm per model.",
    "Pottorff": "Fire/smoke dampers are typically actuator/BAS-monitored for code compliance; the line also "
                "includes plain louvers with no controller -- marked True for the damper portion.",
    "Howden": "Marked True on the assumption that large industrial fans/blowers/compressors at this scale "
              "commonly ship with VFD control; a specific small/direct-drive model may not.",
    "TCF/Twin City Fan": "Marked True on the assumption that commercial/industrial fans at this scale "
                        "commonly ship with VFD control; confirm per model.",
    "Systemair": "Marked True primarily for the ERV portion of the line, which is controller-equipped by "
                "design; some fan-only SKUs may be simpler.",
}


def ufc_4_010_06_basis(name: str) -> str | None:
    """Generates the basis text for a UFC_4_010_06_BY_LINE entry -- a
    consistent disclosure that this is a derived judgment call, not a
    researched fact, on every line rather than only the ones with a
    special-case note."""
    applies = UFC_4_010_06_BY_LINE.get(name)
    if applies is None:
        return None
    verdict = ("has a factory controller or network/BAS connection" if applies
              else "is a passive mechanical device with no controller of its own")
    text = (f"Derived, not researched: UFC 4-010-06's controller/network-infrastructure provisions were "
           f"applied as a logic rule, not looked up per product. This line {verdict}, based on its stated "
           f"subcategory/description. Always confirm which UFC 4-010-06 clause a specific contract actually "
           f"invokes -- this only answers whether the line has a controller or network connection.")
    note = UFC_4_010_06_NOTES.get(name)
    return f"{text} {note}" if note else text


# ---- seeding ---------------------------------------------------------------

def seed_product_lines(session: Session, cfg: Config) -> int:
    """Load the line card from config.yaml into product_lines. Idempotent,
    same shape as app.firms.seed_firms: keyed on normalized name, config wins
    over anything already in the row on every field, insert only if new."""
    added = 0
    for entry in cfg.get("accounts.line_card", []) or []:
        name = entry["name"]
        norm = normalize_name(name)
        existing = session.exec(select(ProductLine).where(ProductLine.name_norm == norm)).first()
        category = entry["category"]
        # markets: keyed on whether config.yaml's `markets:` key is PRESENT,
        # not on whether its value is truthy -- a researched, confirmed-empty
        # result (VU Flow Environmental, Young Regulator) must stay empty,
        # never silently backfilled from the legacy_guess table just because
        # `[] or fallback` is truthy. See the MARKETS_BY_LINE module comment
        # and ProductLine.markets_served_source's docstring.
        if "markets" in entry:
            markets_served = entry.get("markets") or []
            markets_served_source = "researched"
        else:
            markets_served = MARKETS_BY_LINE.get(name, [])
            markets_served_source = "legacy_guess" if markets_served else None
        fields = {
            "firm": entry.get("firm", "DMG"), "category": category,
            "subcategory": entry.get("subcategory", ""), "description": entry.get("description", ""),
            "value_tier": int(entry.get("value_tier", 3)), "equipment_type": entry.get("equipment_type"),
            "heat_rejection_mode": entry.get("heat_rejection_mode"),
            "heat_rejection_mode_verified": bool(entry.get("heat_rejection_mode_verified", False)),
            "heat_rejection_mode_basis": entry.get("heat_rejection_mode_basis"),
            # role: resolved from the table above, never entered per line in
            # config.yaml (see its module comment) -- an explicit `role:` key
            # in an entry still wins, for the rare case someone corrects one
            # directly in config.yaml instead.
            "building_role": entry.get("role") or resolve_building_role(name, category),
            "markets_served": markets_served,
            "markets_served_source": markets_served_source,
            "markets_served_basis": entry.get("markets_basis"),
            # Everything below is config-only, unfilled unless config.yaml
            # states it -- see ProductLine's docstring for why there is no
            # inferred default for any of these.
            "competes_with": entry.get("competes_with"),
            "competes_with_basis": entry.get("competes_with_basis"),
            "oshpd_osp": entry.get("oshpd_osp"),
            "oshpd_osp_basis": entry.get("oshpd_osp_basis"),
            # ufc_4_010_06 is the one field derived rather than sourced from
            # config.yaml -- see UFC_4_010_06_BY_LINE's module comment. An
            # explicit config.yaml value still wins if one is ever set.
            "ufc_4_010_06": (entry.get("ufc_4_010_06") if entry.get("ufc_4_010_06") is not None
                             else UFC_4_010_06_BY_LINE.get(name)),
            "ufc_4_010_06_basis": entry.get("ufc_4_010_06_basis") or ufc_4_010_06_basis(name),
            "ahri_certified": entry.get("ahri_certified"),
            "ahri_certified_basis": entry.get("ahri_certified_basis"),
            "country_of_manufacture": entry.get("country_of_manufacture"),
            "country_of_manufacture_basis": entry.get("country_of_manufacture_basis"),
            "lead_time_weeks_low": entry.get("lead_time_weeks_low"),
            "lead_time_weeks_high": entry.get("lead_time_weeks_high"),
            "lead_time_basis": entry.get("lead_time_basis"),
            "limitations": entry.get("limitations"),
        }
        if existing:
            changed = any(getattr(existing, k) != v for k, v in fields.items())
            if changed:
                for k, v in fields.items():
                    setattr(existing, k, v)
                session.add(existing)
            continue
        session.add(ProductLine(name=name, name_norm=norm, **fields))
        added += 1
    session.commit()
    return added


# ---- selection tools ---------------------------------------------------
#
# access_level's controlled vocabulary. NONE_EXISTS means someone checked
# and the manufacturer genuinely has no selection tool -- a finding, not an
# absence of one. null means nobody has looked yet. These two must never be
# rendered the same way; see SELECTION_TOOL_ACCESS_LABELS and
# app/web/templates/line_detail.html.
SELECTION_TOOL_ACCESS_LEVELS = (
    "public_free", "free_registration", "rep_login", "request_from_factory", "none_exists",
)
SELECTION_TOOL_ACCESS_LABELS = {
    "public_free": "Public, free",
    "free_registration": "Free, registration required",
    "rep_login": "Rep/dealer login required",
    "request_from_factory": "Request from factory",
    "none_exists": "Confirmed: no selection tool exists",
}

# unchecked: nobody has looked -- the default for every line not explicitly
# listed in config.yaml's accounts.selection_tools. search_verified: found
# via web research (a vendor's own page), not manufacturer-direct, not a
# live directory hit -- same tier as ahri_certified's SEARCH_VERIFIED round.
# confirmed: unambiguous on the vendor's own page (SPX's CoolSpec page
# naming both Marley and Recold).
SELECTION_TOOL_VERIFICATION_STATUSES = ("unchecked", "search_verified", "confirmed")
SELECTION_TOOL_VERIFICATION_LABELS = {
    "unchecked": "Unchecked",
    "search_verified": "Search-verified",
    "confirmed": "Confirmed",
}


def seed_selection_tools(session: Session, cfg: Config) -> int:
    """One SelectionTool row per ProductLine, 70 total. A line named in
    config.yaml's accounts.selection_tools gets those fields; every other
    line gets an all-null, verification_status="unchecked" row -- created,
    not skipped, so /line/{id} always has a row to render rather than a
    template branch for "row doesn't exist yet" on top of the
    verification_status=unchecked branch it already needs. Idempotent and
    config-wins, same shape as seed_product_lines/ensure_coverage_rows.

    firm is deliberately not read or written here -- ProductLine.firm via
    product_line_id is the only source of truth; see SelectionTool's
    docstring in app/models.py.
    """
    overrides = {e["name"]: e for e in (cfg.get("accounts.selection_tools", []) or [])}
    lines = session.exec(select(ProductLine)).all()
    added = 0
    for line in lines:
        entry = overrides.get(line.name)
        if entry:
            verified_date = entry.get("verified_date")
            fields = {
                "tool_name": entry.get("tool_name"),
                "vendor_url": entry.get("vendor_url"),
                "access_level": entry.get("access_level"),
                "what_it_outputs": entry.get("what_it_outputs"),
                "produces_submittal_docs": entry.get("produces_submittal_docs"),
                "verified_by": entry.get("verified_by"),
                "verified_date": datetime.strptime(verified_date, "%Y-%m-%d") if verified_date else None,
                "verification_status": entry.get("verification_status", "unchecked"),
            }
        else:
            fields = {
                "tool_name": None, "vendor_url": None, "access_level": None, "what_it_outputs": None,
                "produces_submittal_docs": None, "verified_by": None, "verified_date": None,
                "verification_status": "unchecked",
            }

        existing = session.exec(
            select(SelectionTool).where(SelectionTool.product_line_id == line.id)).first()
        if existing:
            changed = any(getattr(existing, k) != v for k, v in fields.items())
            if changed:
                for k, v in fields.items():
                    setattr(existing, k, v)
                existing.updated_at = utcnow()
                session.add(existing)
            continue
        session.add(SelectionTool(product_line_id=line.id, **fields))
        added += 1
    session.commit()
    return added


def lines_needing_selection_tool_research(session: Session) -> list[ProductLine]:
    """Every line whose selection tool is still unchecked -- the needs-
    research list. Computed live from the DB, not a hardcoded name list, so
    it can never drift from what seed_selection_tools actually wrote."""
    rows = session.exec(
        select(ProductLine)
        .join(SelectionTool, SelectionTool.product_line_id == ProductLine.id)
        .where(SelectionTool.verification_status == "unchecked")
        .order_by(ProductLine.name)
    ).all()
    return list(rows)


def ensure_coverage_rows(session: Session, account: Account) -> int:
    """Create a row for every product line this account has no coverage row for
    yet, status='unknown'. Called on account creation, and safe to call again
    (e.g. after `scout seed-lines` adds a new line) — only fills gaps, never
    touches an existing row.

    This is what makes the account page usable by hand from the first click:
    every line is already a row waiting for a status, not something a rep has
    to add before they can mark it.
    """
    existing_line_ids = {
        c.product_line_id for c in session.exec(
            select(AccountCoverage).where(AccountCoverage.account_id == account.id)).all()
    }
    added = 0
    for line in session.exec(select(ProductLine)).all():
        if line.id in existing_line_ids:
            continue
        session.add(AccountCoverage(account_id=account.id, product_line_id=line.id))
        added += 1
    session.commit()
    return added


def create_account(session: Session, **fields) -> Account:
    """Create an account, auto-linking Firm by normalized name if an existing
    roster entry matches (never creates a new Firm — that roster is the
    project pipeline's, and an account with no pipeline match yet is a normal,
    common case, not an error), then populate its coverage rows."""
    name = fields["name"]
    account = Account(name=name, name_norm=normalize_name(name), **{
        k: v for k, v in fields.items() if k != "name"
    })
    if account.firm_id is None:
        firm = match_firm(session, name)
        if firm is not None:
            account.firm_id = firm.id
    session.add(account)
    session.commit()
    session.refresh(account)
    ensure_coverage_rows(session, account)
    return account


# ---- adjacency scoring -------------------------------------------------

_AFFINITY_CACHE: dict[int, dict[tuple[str, str], float]] = {}


def _affinity_table(cfg: Config) -> dict[tuple[str, str], float]:
    """Sparse edges from config, expanded into a symmetric lookup dict, cached
    per Config instance — same pattern as _esco_match's pattern cache in
    app/sources/base.py, for the same reason: this gets called once per
    category pair per gap, on every account page load."""
    key = id(cfg)
    table = _AFFINITY_CACHE.get(key)
    if table is not None:
        return table
    table = {}
    for edge in cfg.get("accounts.adjacency.edges", []) or []:
        a, b, weight = edge[0], edge[1], float(edge[2])
        table[(a, b)] = weight
        table[(b, a)] = weight
    _AFFINITY_CACHE[key] = table
    return table


def category_affinity(cfg: Config, cat_a: str, cat_b: str) -> float:
    """0-1: how relevant category b is to an account that already owns
    category a. 0 for any pair not in accounts.adjacency.edges — an unlisted
    pair is "no fit", not "not yet rated"."""
    if cat_a == cat_b:
        return 0.0  # a category is not a gap against itself
    return _affinity_table(cfg).get((cat_a, cat_b), 0.0)


def account_type_modifier(cfg: Config, account_type: str, category: str) -> float:
    """How plausible it is that THIS account type buys in `category` at all,
    independent of what they already own — see accounts.adjacency.
    account_type_modifier in config.yaml."""
    table = cfg.get("accounts.adjacency.account_type_modifier", {}) or {}
    entry = table.get(account_type)
    if entry is None:
        return float(table.get("default", 1.0))
    if category in entry:
        return float(entry[category])
    return float(entry.get("default", table.get("default", 1.0)))


def value_tier_band(cfg: Config, tier: int) -> tuple[float, float]:
    table = cfg.get("accounts.value_tier_dollars", {}) or {}
    entry = table.get(tier) or table.get(str(tier))
    if not entry:
        return (0.0, 0.0)
    return (float(entry["low"]), float(entry["high"]))


def owned_categories(session: Session, account_id: int) -> set[str]:
    """Categories this account has at least one product line marked 'bought'
    in. Quoted-and-lost or never-quoted do not establish ownership for
    adjacency purposes — only a completed sale tells you what this account's
    buildings/scope actually run."""
    rows = session.exec(
        select(AccountCoverage, ProductLine)
        .where(AccountCoverage.account_id == account_id,
              AccountCoverage.status == BOUGHT,
              ProductLine.id == AccountCoverage.product_line_id)
    ).all()
    return {line.category for _cov, line in rows}


@dataclass
class Gap:
    line: ProductLine
    coverage_status: str  # never_quoted | quoted_not_won | unknown — always != bought
    relevance: float
    value_low: float
    value_high: float

    @property
    def value_mid(self) -> float:
        return (self.value_low + self.value_high) / 2

    @property
    def gap_score(self) -> float:
        return self.relevance * self.value_mid


def compute_gaps(session: Session, cfg: Config, account_id: int) -> list[Gap]:
    """What this account should have but does not, ranked by estimated dollar
    value — a missing tier-1 line at moderate relevance can and should outrank
    a missing tier-4 line at high relevance, because the question is "what is
    the biggest number on the table I'm not touching", not "what fits best".

    Relevance below accounts.adjacency.min_relevance_to_show drops the line
    from the list entirely rather than showing it at the bottom — this is the
    literal mechanism behind "a rooftop-only account has no use for Seresco
    natatorium units": it does not appear, full stop.
    """
    owned = owned_categories(session, account_id)
    account = session.get(Account, account_id)
    no_coverage_base = float(cfg.get("accounts.adjacency.no_coverage_base", 0.3))
    floor = float(cfg.get("accounts.adjacency.min_relevance_to_show", 0.15))

    rows = session.exec(
        select(AccountCoverage, ProductLine)
        .where(AccountCoverage.account_id == account_id,
              AccountCoverage.status != BOUGHT,
              ProductLine.id == AccountCoverage.product_line_id)
    ).all()

    gaps = []
    for cov, line in rows:
        if owned:
            base = max((category_affinity(cfg, o, line.category) for o in owned), default=0.0)
        else:
            base = no_coverage_base
        relevance = base * account_type_modifier(cfg, account.account_type, line.category)
        if relevance < floor:
            continue
        low, high = value_tier_band(cfg, line.value_tier)
        gaps.append(Gap(line=line, coverage_status=cov.status, relevance=relevance,
                        value_low=low, value_high=high))
    gaps.sort(key=lambda g: g.gap_score, reverse=True)
    return gaps


# ---- replacement windows ----------------------------------------------

@dataclass
class ReplacementWindow:
    line: ProductLine
    install_year: int
    age_years: float
    service_life: ServiceLife
    status: str  # overdue | due | approaching | not_due
    basis: str
    # Calendar year the equipment enters (low) and exits (high) its window,
    # for a rep who wants a date rather than an age to carry into a meeting.
    window_start_year: int
    window_end_year: int


def account_replacement_windows(session: Session, cfg: Config, account_id: int,
                                include_not_due: bool = False) -> list[ReplacementWindow]:
    """What is forcing this account to buy: every covered line with a stated
    install year AND an equipment_type this system has a service-life band
    for, scored against replacement.service_life — the SAME table and the
    SAME ownership branch app/replacement.py already established, not a new
    number. A line with no install year, or whose equipment_type is null
    (most of the line card — diffusers, controls, filters have no ASHRAE-style
    service life on file), is simply not in this list; that is the "abstain
    rather than invent" rule replacement.py already applies to unknown
    equipment/ownership, applied here to unknown install year too.
    """
    account = session.get(Account, account_id)
    now_year = utcnow().year
    rows = session.exec(
        select(AccountCoverage, ProductLine)
        .where(AccountCoverage.account_id == account_id,
              AccountCoverage.status == BOUGHT,
              AccountCoverage.install_year.is_not(None),
              ProductLine.id == AccountCoverage.product_line_id)
    ).all()

    windows = []
    for cov, line in rows:
        if not line.equipment_type:
            continue
        try:
            sl = service_life(cfg, line.equipment_type, account.ownership_type)
        except UnknownEquipment:
            continue
        age = float(now_year - cov.install_year)
        status = sl.status(age)
        if status == "not_due" and not include_not_due:
            continue
        windows.append(ReplacementWindow(
            line=line, install_year=cov.install_year, age_years=age, service_life=sl,
            status=status, basis=replacement_basis(sl, age),
            window_start_year=cov.install_year + sl.low,
            window_end_year=cov.install_year + sl.high,
        ))
    # Most urgent first: overdue, then due, then approaching.
    order = {"overdue": 0, "due": 1, "approaching": 2, "not_due": 3}
    windows.sort(key=lambda w: (order.get(w.status, 9), -w.age_years))
    return windows


# ---- live Scout projects -----------------------------------------------

def live_scout_projects(session: Session, account: Account) -> list[dict]:
    """Active Scout board projects naming this account's company, via the Firm
    bridge — firm_id if set, else a normalized-name fallback so an account
    that was never explicitly linked still shows up if the roster already
    knows the name. No LLM, no fuzzy match beyond what Firm's own aliases
    already carry.
    """
    firm = session.get(Firm, account.firm_id) if account.firm_id else None
    if firm is None:
        firm = match_firm(session, account.name)
    if firm is None:
        return []
    links = session.exec(select(ProjectFirm).where(ProjectFirm.firm_id == firm.id)).all()
    out = []
    for link in links:
        project = session.get(Project, link.project_id)
        if project is None or project.status not in ACTIVE_STATUSES:
            continue
        out.append({"project": project, "role": link.role})
    out.sort(key=lambda r: -r["project"].score)
    return out


# ---- coverage summary + brief -------------------------------------------

def coverage_summary(session: Session, account_id: int) -> dict:
    rows = session.exec(
        select(AccountCoverage, ProductLine)
        .where(AccountCoverage.account_id == account_id,
              ProductLine.id == AccountCoverage.product_line_id)
        .order_by(ProductLine.category, ProductLine.name)
    ).all()
    by_status: dict[str, list] = {s: [] for s in COVERAGE_STATUSES}
    for cov, line in rows:
        by_status[cov.status].append({"coverage": cov, "line": line})
    return {
        "rows": rows,
        "by_status": by_status,
        "counts": {s: len(by_status[s]) for s in COVERAGE_STATUSES},
    }


@dataclass
class AccountBrief:
    account: Account
    coverage: dict
    gaps: list[Gap]
    replacement_windows: list[ReplacementWindow]
    live_projects: list[dict]
    generated_at: object = field(default_factory=utcnow)


def build_account_brief(session: Session, cfg: Config, account_id: int) -> AccountBrief:
    """Everything that goes on the printable one-pager: what they buy, what
    they don't, the ranked gaps with dollar estimates, replacement windows
    with dates, and any live Scout project naming them. See app/brief.py for
    the project-pipeline equivalent this deliberately mirrors."""
    account = session.get(Account, account_id)
    if account is None:
        raise ValueError(f"no account {account_id}")
    return AccountBrief(
        account=account,
        coverage=coverage_summary(session, account_id),
        gaps=compute_gaps(session, cfg, account_id),
        replacement_windows=account_replacement_windows(session, cfg, account_id),
        live_projects=live_scout_projects(session, account),
    )


# ---- line card view: pull-through, accounts, and project matching --------
#
# What makes /lines and /line/{id} a working tool instead of a catalog page:
# for one line, what else attaches to the same job (pull_through), which of
# my accounts already buy it (line_account_matrix), and which live board
# projects would plausibly call for it (matching_projects_for_line) — plus
# the reverse, on a project's own page: given ITS building type, what does
# the card offer by role, and where is there nothing at all
# (line_offering_by_role).

def pull_through(session: Session, cfg: Config, line: ProductLine, limit: int = 18) -> list[dict]:
    """Other lines that plausibly attach to the same job as this one — the
    DMG/ToroAire cross-sell made concrete. Reuses accounts.adjacency's
    existing category_affinity table exactly as compute_gaps does: an
    AAON rooftop (category rooftop_units) pulls Titus/Nailor/CRC/Anemostat
    (air_distribution, 0.8), Pottorff (air_distribution, 0.8), Yaskawa
    (controls, 0.8) and Vibro-Acoustics (acoustics_vibration, 0.5) with it,
    because those ARE the weights already scored on the accounts side — this
    does not introduce a second, unreconciled notion of "what goes together".

    Only OTHER categories are considered (a line never pulls through its own
    category — that's a substitute, not an attachment), and only lines with
    affinity above accounts.adjacency.min_relevance_to_show, the same floor
    compute_gaps uses to drop a pair from view rather than show it at the
    bottom.
    """
    floor = float(cfg.get("accounts.adjacency.min_relevance_to_show", 0.15))
    others = session.exec(select(ProductLine).where(ProductLine.id != line.id)).all()
    scored = []
    for other in others:
        weight = category_affinity(cfg, line.category, other.category)
        if weight < floor:
            continue
        scored.append({"line": other, "weight": weight})
    scored.sort(key=lambda r: (-r["weight"], r["line"].value_tier, r["line"].name))
    return scored[:limit]


def line_account_matrix(session: Session, line_id: int) -> dict:
    """The whitespace matrix viewed by product instead of by account — every
    account that has a coverage row for this line, grouped by status, so the
    page reads as a call list (who already buys it, who doesn't) rather than
    a spec sheet."""
    rows = session.exec(
        select(AccountCoverage, Account)
        .where(AccountCoverage.product_line_id == line_id,
              Account.id == AccountCoverage.account_id)
        .order_by(Account.name)
    ).all()
    by_status: dict[str, list] = {s: [] for s in COVERAGE_STATUSES}
    for cov, account in rows:
        by_status[cov.status].append({"coverage": cov, "account": account})
    return {
        "by_status": by_status,
        "counts": {s: len(by_status[s]) for s in COVERAGE_STATUSES},
    }


# Which board Category/FacilityType combinations a role plausibly gets
# called for. A first-pass judgment table, same footing as MARKETS_BY_LINE
# above — correctable here. `facility_types: None` means "any facility type
# within these categories"; a list narrows it for roles where the building's
# specific use genuinely changes whether the role applies (e.g. water
# treatment matters a lot more to a cleanroom or a heavy-manufacturing plant
# than to a distribution warehouse).
#
# Deliberately scoped to what Scout's board actually tracks
# (data_center/industrial/esco new construction) — not the fuller MARKETS
# vocabulary above, which includes healthcare/education/hospitality/labs/
# office/multifamily that this pipeline has no project data for at all. See
# matching_projects_for_line's docstring.
ROLE_PROJECT_RELEVANCE = {
    "air_handling": {"categories": ["data_center", "industrial", "esco"], "facility_types": None},
    "cooling_generation": {"categories": ["data_center", "industrial", "esco"], "facility_types": None},
    "heat_rejection": {"categories": ["data_center", "industrial"], "facility_types": None},
    "air_distribution_terminal": {"categories": ["data_center", "industrial", "esco"], "facility_types": None},
    "fans_ventilation": {"categories": ["data_center", "industrial", "esco"], "facility_types": None},
    "dampers_life_safety": {"categories": ["data_center", "industrial", "esco"], "facility_types": None},
    "controls_valves": {"categories": ["data_center", "industrial", "esco"], "facility_types": None},
    "indoor_air_quality": {
        "categories": ["data_center", "industrial"],
        "facility_types": ["cleanroom", "office_rnd", "data_center", "unknown"],
    },
    "humidification": {
        "categories": ["data_center", "industrial"],
        "facility_types": ["cleanroom", "data_center", "unknown"],
    },
    "acoustics_seismic": {"categories": ["data_center", "industrial"], "facility_types": None},
    "energy_recovery": {
        "categories": ["data_center", "industrial"],
        "facility_types": ["office_rnd", "data_center", "cleanroom", "unknown"],
    },
    "water_treatment": {
        "categories": ["data_center", "industrial"],
        "facility_types": ["heavy_manufacturing", "cleanroom", "data_center", "unknown"],
    },
    "heating_specialty": {"categories": ["industrial", "esco"], "facility_types": None},
}


def project_facility_type(project: Project, signals: list[Signal]) -> FacilityType:
    """The same resolution app.pipeline.size_score._facility_type +
    run_size_score's value_type fallback already apply when sizing/valuing a
    project — reproduced here rather than imported, since the pipeline
    module's own docstring scopes it to scoring, and this needs only the
    plain type, not a recompute of tonnage. Kept in exact lockstep with that
    logic: the first linked signal that states a facility type wins, and a
    data-center project with no signal stating one is treated as
    facility_type=data_center rather than unknown (most data-center filings
    never restate what the category column already says)."""
    facility_type = FacilityType.unknown
    for s in signals:
        if s.facility_type is not FacilityType.unknown:
            facility_type = s.facility_type
            break
    if facility_type is FacilityType.unknown and project.category is Category.data_center:
        return FacilityType.data_center
    return facility_type


def facility_types_by_project(session: Session, project_ids: list[int]) -> dict[int, FacilityType]:
    """Batched version of project_facility_type, same shape and reason as
    app.pipeline.size_score.signal_types_by_project: matching_projects_for_line
    needs this across every active project on the board, not one at a time."""
    if not project_ids:
        return {}
    projects_by_id = {p.id: p for p in session.exec(
        select(Project).where(Project.id.in_(project_ids))).all()}
    links = session.exec(
        select(ProjectSignal).where(ProjectSignal.project_id.in_(project_ids))).all()
    signal_ids = list({l.signal_id for l in links})
    signals_by_id = ({s.id: s for s in session.exec(
        select(Signal).where(Signal.id.in_(signal_ids))).all()} if signal_ids else {})
    signals_by_project: dict[int, list[Signal]] = {pid: [] for pid in project_ids}
    for link in links:
        s = signals_by_id.get(link.signal_id)
        if s is not None:
            signals_by_project[link.project_id].append(s)
    return {
        pid: project_facility_type(projects_by_id[pid], signals_by_project.get(pid, []))
        for pid in project_ids if pid in projects_by_id
    }


def matching_projects_for_line(session: Session, line: ProductLine, limit: int = 25) -> list[Project]:
    """Active board projects that would plausibly call for this line, from
    its building_role via ROLE_PROJECT_RELEVANCE — "show me every project
    where Strobic lab exhaust applies" as one click, not a search someone
    has to construct by hand.

    Scoped to what Scout's board actually is: data_center/industrial/esco
    new-construction. A line whose markets_served names healthcare or
    education will never return anything here, because Scout does not track
    projects in those verticals at all — that is a statement about the
    pipeline's own scope, not about whether the line fits.
    """
    rule = ROLE_PROJECT_RELEVANCE.get(line.building_role)
    if rule is None:
        return []
    q = select(Project).where(
        Project.status.in_(ACTIVE_STATUSES),
        Project.category.in_([Category(c) for c in rule["categories"]]),
    )
    projects = session.exec(q.order_by(Project.score.desc())).all()
    if rule["facility_types"] is None:
        return projects[:limit]
    allowed = {FacilityType(f) for f in rule["facility_types"]}
    ft_by_project = facility_types_by_project(session, [p.id for p in projects])
    matched = [p for p in projects if ft_by_project.get(p.id) in allowed]
    return matched[:limit]


@dataclass
class RoleOffering:
    role: str
    label: str
    relevant: bool          # is this role even plausibly called for on this building type
    lines: list[ProductLine]
    gap: bool = False       # relevant AND zero lines on the card for it


def line_offering_by_role(session: Session, category: Category, facility_type: FacilityType) -> list[RoleOffering]:
    """The reverse of matching_projects_for_line, for a project's own page:
    given THIS building's category/facility type, what does the line card
    offer, grouped by role, and where does it offer nothing at all. Every
    role currently has at least one line (see CATEGORY_TO_ROLE — 70 lines
    span all 13), so `gap` should never fire today; it stays a real,
    computed flag rather than an assumption so a future line-card edit that
    empties a role is caught here instead of silently disappearing.
    """
    lines_by_role: dict[str, list[ProductLine]] = {}
    for line in session.exec(select(ProductLine).order_by(ProductLine.value_tier, ProductLine.name)).all():
        lines_by_role.setdefault(line.building_role, []).append(line)

    out = []
    for role in ROLE_ORDER:
        rule = ROLE_PROJECT_RELEVANCE.get(role, {})
        relevant = (
            category.value in rule.get("categories", [])
            and (rule.get("facility_types") is None or facility_type.value in rule["facility_types"])
        )
        lines = lines_by_role.get(role, [])
        out.append(RoleOffering(
            role=role, label=ROLE_LABELS[role], relevant=relevant, lines=lines,
            gap=relevant and not lines,
        ))
    return out
