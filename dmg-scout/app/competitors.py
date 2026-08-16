"""Competitor line-card map: which rep firms carry which competing
manufacturer lines, for which building role, in DMG/ToroAire's own
territory. See RepFirm/CompetitorLine's docstrings in app/models.py for the
schema and status discipline.

RESEARCH METHOD (2026-08-16). Every row below is sourced ONLY from that
rep firm's own published line card page, or (for Greenheck) the
manufacturer's own live "find a rep" locator -- never a third-party
directory, never a guess from reputation. Compliance checked before any
fetch: all seven rep-firm sites' robots.txt allow the pages read here
(only standard WordPress/Squarespace admin/search/config paths disallowed
anywhere -- nswcmech.com, airtreatment.com, dencorep.com, trcsales.com,
wpreps.com, keylinesales.com all disallow only /wp-admin or Squarespace's
standard /config,/search,/account,/api set; wrightsales.net has no
robots.txt at all, the standard "everything allowed" default). One ACHR
News directory page that might have shown the OLDER, conflicting Greenheck
listing returned HTTP 403 and could not be read -- noted on that row
rather than silently treated as resolved.

TWO KINDS OF "unconfirmed", both recorded rather than resolved by picking
a side (per instruction):
1. Greenheck / Norman S. Wright Climatec -- Greenheck's own live rep
   locator (greenheck.com/find-my-rep/0007_usa_california) and NSWC's own
   site (nswcmech.com/products/manufacturers/) currently agree, but an
   older third-party HVAC industry directory listing is known to conflict.
2. FOUR lines this research found also sitting on DMG's OWN line card for
   the identical role: Twin City Fan (Air Treatment Corp / DMG, both
   fans_ventilation), Panasonic (DENCO / DMG, both fans_ventilation),
   Soler & Palau (TRC Sales / DMG, both fans_ventilation), and Airzone
   (TRC Sales / DMG, both controls_valves). Not something this research
   set out to find -- it fell out of cross-checking manufacturer names
   against app.accounts's own seeded ProductLine table -- but exactly the
   shape of ambiguity the instruction covers: two cards claiming the same
   line, recorded rather than silently treated as a clean competitor.

Trane is recorded with channel="factory_direct", rep_firm_id=None: no
independent rep firm's published card checked here lists Trane anywhere,
and Trane operates its own branded "Commercial Sales Office" pages across
California (Los Angeles, San Francisco, San Diego, Sacramento, Oakland,
Fresno) rather than routing through a third party -- an absence-based
finding, disclosed as such, not a positive statement from a locator tool
the way Greenheck's is.

Not exhaustive. Rep firms carry accessories, tools, parts, and components
alongside real equipment lines (gauges, brazing alloys, lineset covers,
relays, insulation) that map onto none of the 13 building_role values --
those rows are recorded (still a real, sourced fact: this firm carries
this line) with building_role=None, and simply don't appear on the
per-role competitive surfacing. Keyline Sales' own line card is
predominantly plumbing fixtures (American Standard, Gerber, Viega, and
~20 more) entirely outside DMG's HVAC/mechanical scope -- not transcribed
line-by-line here, noted instead.
"""
from __future__ import annotations

from datetime import datetime

from sqlmodel import Session, select

from app.models import CompetitorLine, RepFirm, SourceRun, utcnow

SOURCE = "competitor_lines"

REP_FIRMS: dict[str, dict] = {
    "Norman S. Wright Climatec": {
        "website": "https://nswcmech.com/",
        "territory_note": "CA, NV, HI, Guam",
    },
    "Air Treatment Corporation": {
        "website": "https://www.airtreatment.com/",
        "territory_note": "CA, NV, HI, OR, Guam",
    },
    "DENCO": {
        "website": "https://www.dencorep.com/",
        "territory_note": "CA, AZ, HI, NV (Las Vegas)",
    },
    "TRC Sales": {
        "website": "https://www.trcsales.com/",
        "territory_note": "CA, AZ, NV, HI, Guam",
    },
    "Western Pacific Reps": {
        "website": "https://wpreps.com/",
        "territory_note": "CA, AZ, NV, HI",
    },
    "Keyline Sales": {
        "website": "https://www.keylinesales.com/",
        "territory_note": "CA, NV, AZ, UT, southern ID, SW WY",
    },
    "Wright Sales": {
        "website": "https://wrightsales.net/",
        "territory_note": "CA, NV (except Clark County), HI",
    },
}

_NSWC_SRC = "https://nswcmech.com/products/manufacturers/"
_ATC_SRC = "https://www.airtreatment.com/product-lines/"
_DENCO_SRC = "https://www.dencorep.com/about.html"
_TRC_SRC = "https://www.trcsales.com/manufacturers/"
_WPR_SRC = "https://wpreps.com/manufacturers-1"
_KEYLINE_SRC = "https://www.keylinesales.com/california-products/"
_WRIGHT_SRC = "https://wrightsales.net/"
_TRANE_SRC = "https://www.trane.com/commercial/north-america/us/en/contact-us/locate-sales-offices/losangeles.html"

_DMG_OVERLAP = ("Also appears on DMG's own line card for this same role -- recorded unconfirmed "
                "rather than presenting DMG and this rep firm as if they cleanly compete for it.")

# (manufacturer, rep_firm | None, building_role | None, channel, status, source_url, note)
COMPETITOR_LINES: list[tuple] = [
    # ---- Norman S. Wright Climatec -- confirmed starting point ------------
    ("Daikin Applied", "Norman S. Wright Climatec", "cooling_generation", "rep_firm",
     "confirmed", _NSWC_SRC, None),
    ("Greenheck", "Norman S. Wright Climatec", "fans_ventilation", "rep_firm",
     "unconfirmed", _NSWC_SRC,
     "Greenheck's own live rep locator (greenheck.com/find-my-rep/0007_usa_california) and "
     "NSWC's own site currently agree, but an older third-party HVAC industry directory listing "
     "is known to conflict -- that directory page returned HTTP 403 and could not be read to "
     "confirm which firm it names, so this is recorded unconfirmed rather than resolved."),

    # ---- Air Treatment Corporation -- confirmed starting point -------------
    ("YORK Applied (chillers, heat pumps)", "Air Treatment Corporation", "cooling_generation",
     "rep_firm", "confirmed", _ATC_SRC,
     "ATC's own line spans both cooling generation and air handling -- see sibling row."),
    ("YORK Applied (AHUs, DOAS, ERV, fan coils, WSHP)", "Air Treatment Corporation", "air_handling",
     "rep_firm", "confirmed", _ATC_SRC,
     "ATC's own line spans both cooling generation and air handling -- see sibling row."),
    ("Baltimore Aircoil", "Air Treatment Corporation", "heat_rejection", "rep_firm",
     "confirmed", _ATC_SRC, None),
    ("Twin City Fan", "Air Treatment Corporation", "fans_ventilation", "rep_firm",
     "unconfirmed", _ATC_SRC, _DMG_OVERLAP),
    ("Krueger", "Air Treatment Corporation", "air_distribution_terminal", "rep_firm",
     "confirmed", _ATC_SRC, None),
    ("Armstrong", "Air Treatment Corporation", "heating_specialty", "rep_firm",
     "confirmed", _ATC_SRC,
     "ATC's own line is domestic pressure-boosting and hot-water recirc pumps -- no building_role "
     "cleanly covers pump systems; heating_specialty is the closest fit, a judgment call."),
    ("Munters", "Air Treatment Corporation", "humidification", "rep_firm",
     "confirmed", _ATC_SRC, None),
    ("Stulz", "Air Treatment Corporation", "humidification", "rep_firm",
     "confirmed", _ATC_SRC,
     "ATC's own line description for Stulz is specifically desiccant dehumidifiers, not Stulz's "
     "broader precision-cooling (CRAC) catalog -- recorded as the source states it, not as Stulz "
     "is generally known for."),

    # ---- DENCO ---------------------------------------------------------------
    ("Samsung", "DENCO", "cooling_generation", "rep_firm", "confirmed", _DENCO_SRC,
     "Ductless mini-split and VRF systems, per DENCO's own description."),
    ("Panasonic", "DENCO", "fans_ventilation", "rep_firm", "unconfirmed", _DENCO_SRC, _DMG_OVERLAP),
    ("Resideo / Honeywell Home", "DENCO", "controls_valves", "rep_firm", "confirmed", _DENCO_SRC,
     "Thermostats, IAQ, zoning, combustion and controls, per DENCO's own description."),
    ("Phenomenal Aire", "DENCO", "indoor_air_quality", "rep_firm", "confirmed", _DENCO_SRC,
     "IAQ measuring/monitoring, per DENCO's own description."),
    # Accessories/tools/components with no clean building_role -- still real,
    # sourced facts, just not roles the /lines board tracks.
    ("Yellow Jacket", "DENCO", None, "rep_firm", "confirmed", _DENCO_SRC, "Manifold gauges/tools."),
    ("Lau", "DENCO", None, "rep_firm", "confirmed", _DENCO_SRC, "Fan blades/blower wheels (component)."),
    ("Modular Metals", "DENCO", None, "rep_firm", "confirmed", _DENCO_SRC, "Sheet metal fabrication."),
    ("Teslong", "DENCO", None, "rep_firm", "confirmed", _DENCO_SRC, "Inspection cameras."),
    ("Lucas Milhaupt", "DENCO", None, "rep_firm", "confirmed", _DENCO_SRC, "Brazing/soldering alloys."),
    ("Blue Diamond", "DENCO", None, "rep_firm", "confirmed", _DENCO_SRC, "Pumps (no clean role fit)."),
    ("Acme-Miami", "DENCO", None, "rep_firm", "confirmed", _DENCO_SRC, "Motors/fan blades (component)."),
    ("Marketair", "DENCO", None, "rep_firm", "confirmed", _DENCO_SRC, "Mini-split install kits."),
    ("Hartland Controls", "DENCO", None, "rep_firm", "confirmed", _DENCO_SRC,
     "Relays/contactors/capacitors (component)."),
    ("Inaba Denko", "DENCO", None, "rep_firm", "confirmed", _DENCO_SRC, "Lineset covers/accessories."),
    ("ICOOL USA", "DENCO", None, "rep_firm", "confirmed", _DENCO_SRC,
     "Linesets/disconnects/refrigerant accessories."),

    # ---- TRC Sales -------------------------------------------------------------
    ("Fujitsu / Airstage", "TRC Sales", "cooling_generation", "rep_firm", "confirmed", _TRC_SRC,
     "VRF systems, per TRC's own description."),
    ("Soler & Palau", "TRC Sales", "fans_ventilation", "rep_firm", "unconfirmed", _TRC_SRC, _DMG_OVERLAP),
    ("Airzone", "TRC Sales", "controls_valves", "rep_firm", "unconfirmed", _TRC_SRC, _DMG_OVERLAP),
    ("Glasfloss", "TRC Sales", "indoor_air_quality", "rep_firm", "confirmed", _TRC_SRC,
     "Air filtration, per TRC's own description."),
    ("Ultravation", "TRC Sales", "indoor_air_quality", "rep_firm", "confirmed", _TRC_SRC,
     "Air quality solutions, per TRC's own description."),
    ("Corrosion Grenade", "TRC Sales", None, "rep_firm", "confirmed", _TRC_SRC, "Corrosion protection product."),
    ("Armacell", "TRC Sales", None, "rep_firm", "confirmed", _TRC_SRC, "Insulation."),
    ("Southwire Genesis", "TRC Sales", None, "rep_firm", "confirmed", _TRC_SRC, "Electrical/wiring."),
    ("Smart Electric", "TRC Sales", None, "rep_firm", "confirmed", _TRC_SRC, "Electrical products."),

    # ---- Western Pacific Reps ---------------------------------------------------
    ("Nordyne (a Rheem company)", "Western Pacific Reps", "air_handling", "rep_firm", "confirmed", _WPR_SRC,
     "Packaged multi-family/commercial heating-cooling equipment, per WEST PAC's own description."),
    ("First Company", "Western Pacific Reps", "air_handling", "rep_firm", "confirmed", _WPR_SRC,
     "Fan coils, air handlers, PTAC, water-source heat pumps, per WEST PAC's own description."),
    ("Metal Fab", "Western Pacific Reps", "dampers_life_safety", "rep_firm", "confirmed", _WPR_SRC,
     "Gas venting, fire dampers, grilles/diffusers -- fire dampers is the defining product; a "
     "judgment call between dampers_life_safety and air_distribution_terminal."),
    ("FAMCO", "Western Pacific Reps", "fans_ventilation", "rep_firm", "confirmed", _WPR_SRC,
     "Ventilation components (vents, chimney caps, dampers, flashings), per WEST PAC's own description."),
    ("JP Lamborn", "Western Pacific Reps", "air_distribution_terminal", "rep_firm", "confirmed", _WPR_SRC,
     "Flexible ductwork, per WEST PAC's own description."),
    ("Boldr Energy", "Western Pacific Reps", None, "rep_firm", "confirmed", _WPR_SRC,
     "Smart thermostats/controllers for mini-splits (component-level)."),
    ("Elitech", "Western Pacific Reps", None, "rep_firm", "confirmed", _WPR_SRC, "Gauges/instruments/tools."),
    ("HMAX", "Western Pacific Reps", None, "rep_firm", "confirmed", _WPR_SRC,
     "Line sets/condensate pumps/accessories."),
    ("Supco & Solderweld", "Western Pacific Reps", None, "rep_firm", "confirmed", _WPR_SRC, "Parts/alloys."),

    # ---- Keyline Sales -- predominantly plumbing; only HVAC-adjacent lines recorded --
    ("Lochinvar", "Keyline Sales", "heating_specialty", "rep_firm", "confirmed", _KEYLINE_SRC,
     "Water heaters/boilers, per Keyline's own description."),
    ("Taco", "Keyline Sales", "heating_specialty", "rep_firm", "confirmed", _KEYLINE_SRC,
     "Hydronic circulation pumps ('Comfort Pumps') -- no building_role cleanly covers pump "
     "systems; heating_specialty is the closest fit, same judgment call as Armstrong above."),
    ("Duravent", "Keyline Sales", "fans_ventilation", "rep_firm", "confirmed", _KEYLINE_SRC,
     "Venting products, per Keyline's own description."),
    ("Davey", "Keyline Sales", None, "rep_firm", "confirmed", _KEYLINE_SRC, "Booster pumps."),
    ("Pietro Fiorentini", "Keyline Sales", None, "rep_firm", "confirmed", _KEYLINE_SRC, "Gas regulators."),

    # ---- Wright Sales ------------------------------------------------------------
    ("Fantech", "Wright Sales", "fans_ventilation", "rep_firm", "confirmed", _WRIGHT_SRC,
     "Ventilation fans, per Wright Sales' own product listing."),
    ("Friedrich", "Wright Sales", "air_handling", "rep_firm", "confirmed", _WRIGHT_SRC,
     "PTAC/room air conditioning units."),
    ("Thermaflex", "Wright Sales", "air_distribution_terminal", "rep_firm", "confirmed", _WRIGHT_SRC,
     "Flexible ductwork."),
    ("ZoneFirst", "Wright Sales", "controls_valves", "rep_firm", "confirmed", _WRIGHT_SRC,
     "Zone damper control systems."),
    ("Rectorseal", "Wright Sales", None, "rep_firm", "confirmed", _WRIGHT_SRC,
     "Sealants/chemicals/tools, confirmed independently by RectorSeal's own 2019 press release "
     "naming Wright Sales Co. as its CA/N.NV/HI rep."),
    ("Aire Technologies", "Wright Sales", None, "rep_firm", "confirmed", _WRIGHT_SRC, "Accessories."),
    ("Arrco", "Wright Sales", None, "rep_firm", "confirmed", _WRIGHT_SRC, "Accessories."),
    ("Aspen", "Wright Sales", None, "rep_firm", "confirmed", _WRIGHT_SRC, "Condensate pumps (component)."),
    ("Hardcast", "Wright Sales", None, "rep_firm", "confirmed", _WRIGHT_SRC, "Duct sealant."),
    ("Macurco", "Wright Sales", None, "rep_firm", "confirmed", _WRIGHT_SRC, "Gas detection."),
    ("NAVAC", "Wright Sales", None, "rep_firm", "confirmed", _WRIGHT_SRC, "Tools."),
    ("NDL", "Wright Sales", None, "rep_firm", "confirmed", _WRIGHT_SRC, "Accessories."),
    ("Owens Corning", "Wright Sales", None, "rep_firm", "confirmed", _WRIGHT_SRC, "Insulation."),
    ("Purolator", "Wright Sales", None, "rep_firm", "confirmed", _WRIGHT_SRC, "Filtration."),
    ("SmartLock", "Wright Sales", None, "rep_firm", "confirmed", _WRIGHT_SRC, "Accessories."),
    ("Vybond", "Wright Sales", None, "rep_firm", "confirmed", _WRIGHT_SRC, "Adhesives/sealants."),

    # ---- Trane -- factory-direct, not repped -----------------------------------
    ("Trane", None, "cooling_generation", "factory_direct", "confirmed", _TRANE_SRC,
     "No independent rep firm's published line card checked in this research lists Trane. Trane "
     "operates its own branded Commercial Sales Office pages across California (Los Angeles, San "
     "Francisco, San Diego, Sacramento, Oakland, Fresno) rather than routing through a third party "
     "-- an absence-based finding, disclosed as such."),
    ("Trane", None, "air_handling", "factory_direct", "confirmed", _TRANE_SRC,
     "Same basis as the cooling_generation row above -- Trane's product line spans both roles."),
]


def seed_competitor_lines(session: Session) -> dict:
    """Loads REP_FIRMS and COMPETITOR_LINES into the database -- upserts by
    (manufacturer, rep_firm, building_role) so re-running this after a
    dataset edit doesn't duplicate rows. Not a live fetch (there is no
    scraper here -- see the module docstring for the research method), but
    still writes a SourceRun so a stale/never-reloaded dataset shows up on
    /health rather than failing silently, same discipline every other
    source in this app follows."""
    run = SourceRun(source=SOURCE)
    session.add(run)
    session.commit()

    firms_by_name: dict[str, RepFirm] = {
        f.name: f for f in session.exec(select(RepFirm)).all()
    }
    for name, fields in REP_FIRMS.items():
        firm = firms_by_name.get(name)
        if firm is None:
            firm = RepFirm(name=name, **fields)
            session.add(firm)
            firms_by_name[name] = firm
        else:
            firm.website = fields.get("website")
            firm.territory_note = fields.get("territory_note")
            session.add(firm)
    session.commit()
    for firm in firms_by_name.values():
        session.refresh(firm)

    existing = {
        (c.manufacturer, c.rep_firm_id, c.building_role): c
        for c in session.exec(select(CompetitorLine)).all()
    }
    retrieved_at = utcnow()
    stored = 0
    for manufacturer, rep_firm_name, role, channel, status, source_url, note in COMPETITOR_LINES:
        rep_firm_id = firms_by_name[rep_firm_name].id if rep_firm_name else None
        key = (manufacturer, rep_firm_id, role)
        row = existing.get(key)
        if row is None:
            row = CompetitorLine(manufacturer=manufacturer, rep_firm_id=rep_firm_id, building_role=role)
            session.add(row)
            existing[key] = row
        row.channel = channel
        row.status = status
        row.source_url = source_url
        row.conflict_note = note
        row.retrieved_at = retrieved_at
        stored += 1
    session.commit()

    run.finished_at = utcnow()
    run.records_fetched = len(COMPETITOR_LINES)
    run.records_new = stored
    run.ok = True
    session.add(run)
    session.commit()

    confirmed = sum(1 for row in COMPETITOR_LINES if row[4] == "confirmed")
    unconfirmed = sum(1 for row in COMPETITOR_LINES if row[4] == "unconfirmed")
    return {"rep_firms": len(REP_FIRMS), "lines_total": len(COMPETITOR_LINES),
            "confirmed": confirmed, "unconfirmed": unconfirmed}


def competing_lines_by_role(session: Session) -> dict[str, list[CompetitorLine]]:
    """Every CompetitorLine grouped by building_role, for the per-role
    surfacing on /project/{id} and /line/{id} -- rows with no role
    (accessories/tools/components) are excluded, since there's no role to
    compare them against DMG's own card on."""
    rows = session.exec(select(CompetitorLine).where(CompetitorLine.building_role.is_not(None))).all()
    out: dict[str, list[CompetitorLine]] = {}
    for row in rows:
        out.setdefault(row.building_role, []).append(row)
    return out
