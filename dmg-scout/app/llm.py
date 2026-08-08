"""Anthropic API layer: cheap triage (Haiku), structured extraction and entity
adjudication (Sonnet). Tool-use forces valid JSON; the models are instructed to
return null rather than guess."""
from __future__ import annotations

import json
import logging

import anthropic

from app.config import anthropic_api_key, load_config
from app.schemas import EXTRACTION_JSON_SCHEMA, coerce_extraction

log = logging.getLogger(__name__)

TRIAGE_SYSTEM = """You triage documents for an HVAC manufacturers' rep firm that sells
mechanical equipment into new buildings in California, Nevada and Arizona. You CLASSIFY;
you do not merely filter.

A document is relevant if it concerns a specific NEW OR EXPANDING BUILDING of meaningful
size — either the project itself (planned, proposed, permitted, under construction,
expanding, relocating, or being leased) or an action about one (tax abatement
application, generator or air permit, utility load request, planning or commission
agenda item, entitlement, grading permit, job posting tied to a location).

Then assign a category:

data_center — the facility's primary purpose is housing and OPERATING computing
  equipment at scale: data center, colocation, hyperscale or AI compute campus,
  enterprise server room or computing consolidation, cryptocurrency mining operation.

industrial — any other substantial non-residential building: manufacturing, assembly,
  fabrication, processing, warehouse, distribution, fulfillment, logistics, cold
  storage, cleanroom, R&D, laboratory, or a corporate headquarters or office campus.
  A plant that BUILDS servers, computing hardware or mining rigs belongs here: it is
  a factory, not a computing facility. So does an equipment maker of any kind.

esco — a public agency is procuring or awarding an ENERGY SERVICES PERFORMANCE
  CONTRACT on buildings it already owns. Energy services agreement, energy savings
  performance contract, ESPC, guaranteed energy savings or guaranteed energy cost
  savings agreement, investment grade audit, or an RFQ/RFP to select an energy
  services company (ESCO).

  This is the one category that is NOT a new or expanding building, and it is kept
  anyway for one reason: the ESCO selects the equipment. An architect or CM award
  tells you a project exists; an ESCO award tells you who will choose the
  chillers, and that equipment gets bought in 12 to 24 months.

  Use it when the document is about SELECTING OR AWARDING the contract — the
  procurement itself. Routine energy-efficiency chatter, a utility rebate item, a
  solar PPA, or a completed retrofit being accepted is `other`: there is no
  upcoming selection to influence. If the document names a specific agency and a
  specific facility or portfolio, that is enough to name a location.

other — not relevant. Use this for:
  - rules about buildings in general with no specific project: moratoria,
    prohibitions, zoning or general-plan amendments, ordinances
  - infrastructure that is not a building: cell towers, two-way radio systems, fiber
    or cable routes, transmission lines, roads, pipelines, solar or generation plants
    (unless the document ties them to a specific data center or industrial building)
  - general industry news, market commentary or financial reporting naming no specific
    project or location
  - routine agendas, minutes and administrative items with no building project
  - tenant improvements, re-roofing, maintenance, and other work that adds no new
    conditioned space
  - anything small: under roughly 10,000 square feet, unless it is a data center

The kind of document never decides the category on its own. A tax abatement application
is relevant only if it concerns a real building, and its category depends on what that
building DOES — not on how technical the applicant sounds.

Classify the FACILITY THIS DOCUMENT DESCRIBES, never the company's line of business.
An AI cloud provider leasing 20,000 sqft of headquarters space with $1M of equipment is
an office, and belongs in `industrial` — the fact that it operates GPUs somewhere else
is not evidence about this building. Read the numbers: staff counts, lease rate and
capital equipment give the use away. A data hall has almost no staff and costs tens of
millions to fit out. If the document contains an explicit facility-type field (GOED's
application form has one: Headquarters / Manufacturing / Distribution / R&D / Back
Office), that field decides it.

Equipment work at an existing building is `other`, not a project: transformer or
switchgear replacement, electrical service upgrades, generator swaps, re-roofing,
tenant improvements. Adding no new conditioned space means there is nothing to sell.

Never guess. If the document does not make clear that a specific building is involved,
the category is other."""

TRIAGE_TOOL = {
    "name": "triage_result",
    "description": "Record the triage classification.",
    "input_schema": {
        "type": "object",
        "properties": {
            "category": {
                "type": "string",
                "enum": ["data_center", "industrial", "esco", "other"],
                "description": "data_center = operates computing capacity; "
                               "industrial = any other substantial new/expanding "
                               "building; esco = a public agency awarding or "
                               "procuring an energy services performance contract "
                               "on buildings it already owns; other = not relevant",
            },
            "names_location": {"type": "boolean",
                               "description": "True if a specific site, city, county, or parcel is named"},
            "reason": {"type": "string", "description": "One short sentence"},
        },
        "required": ["category", "names_location", "reason"],
    },
}

EXTRACT_SYSTEM = """You extract structured facts about data center projects from public
documents (environmental filings, board packets, permits, SEC filings, agendas, news).

Rules — these are absolute:
- NEVER guess or infer a value. If the document does not state it, return null.
- Do not convert units or interpolate. Report numbers exactly as stated.
- mw_it is IT/critical load only when the document explicitly says so; a generic
  "megawatts" figure goes in mw_total.
- generator_critical_count/generator_critical_mw_each and generator_house_count/
  generator_house_mw_each: use these ONLY when the document itself splits the
  generator fleet into units dedicated to data center critical/IT load versus
  units backing house load (office, cooling plant, everything else) — CEC power
  filings state this routinely, e.g. "thirty eight (38) 3 MW units dedicated to
  data center critical loads, and two 1 MW house generators". Report the MW
  figure exactly as stated per unit; do not convert from hp or kW to fill these.
  An undifferentiated generator count with no critical/house split goes in the
  plain generator_count/generator_hp_each/generator_kw_each fields instead, and
  the critical/house fields stay null — do not guess which bucket it belongs to.
- named_people: only people named in the document with a role related to the project.
- confidence reflects how clearly the document supports the extracted fields overall.
- summary_one_line: one factual sentence, no speculation.

facility_type — what the building DOES, which sets its cooling load per square
foot. Report it only when the document makes the use clear; null or "unknown" is
correct otherwise, and far better than a guess, because the sqft-per-ton figure
varies 50x across these:
- distribution_fulfillment: distribution centre, fulfillment, logistics, cross-dock
- warehouse_conditioned: conditioned storage or cold storage
- light_manufacturing: assembly, packaging, fabrication, food production
- heavy_manufacturing: process load — foundry, chemical, steel, plastics extrusion,
  anything with furnaces, ovens, kilns, reactors or heavy motor load
- cleanroom: semiconductor, pharmaceutical, medical device, controlled environment
- office_rnd: office, headquarters, R&D, laboratory, engineering space
- data_center: the building operates computing equipment at scale
A building with several uses takes the one covering the most floor area.
If the document has an explicit facility-type field, use it: GOED's application form
asks "Type of Facility: Headquarters / Service Provider / Technology / Distribution /
Fulfillment / Back Office / Manufacturing / Research & Development", and that answer
beats any inference from the company's description of itself."""

EXTRACT_TOOL = {
    "name": "record_extraction",
    "description": "Record the structured extraction.",
    "input_schema": EXTRACTION_JSON_SCHEMA,
}

ADJUDICATE_SYSTEM = """You decide whether two records describe the SAME physical data center
project. Different SPE/LLC names for one campus are the same project. Different phases on
one campus are the same project unless clearly separate buildings years apart. Same
developer in the same county is NOT automatically the same project. If the evidence is
thin, answer uncertain — never force a match."""

ADJUDICATE_TOOL = {
    "name": "adjudicate_match",
    "description": "Record the match decision.",
    "input_schema": {
        "type": "object",
        "properties": {
            "verdict": {"type": "string", "enum": ["match", "no_match", "uncertain"]},
            "reasoning": {"type": "string"},
        },
        "required": ["verdict", "reasoning"],
    },
}


class LLMUnavailable(Exception):
    pass


def _client() -> anthropic.Anthropic:
    key = anthropic_api_key()
    if not key:
        raise LLMUnavailable("ANTHROPIC_API_KEY is not set")
    return anthropic.Anthropic(api_key=key)


def _tool_call(model: str, system: str, tool: dict, user_content: str,
               max_tokens: int = 2048, stage: str = "unknown") -> dict:
    from app.spend import check_budget, record
    client = _client()
    check_budget()  # raises BudgetExceeded past the daily cap
    resp = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=system,
        tools=[tool],
        tool_choice={"type": "tool", "name": tool["name"]},
        messages=[{"role": "user", "content": user_content}],
    )
    record(stage, model, resp.usage.input_tokens, resp.usage.output_tokens)
    for block in resp.content:
        if block.type == "tool_use" and block.name == tool["name"]:
            return dict(block.input)
    raise RuntimeError(f"model returned no {tool['name']} tool call")


def triage(text: str, title: str = "", source: str = "") -> dict:
    """Returns {category, names_location, reason}. Cheap Haiku call.

    Reads the whole document where it fits and a spanning sample where it does
    not — never just the head. See app.sections.select_triage_text: a relevance
    question is about the whole document, and truncating to the first 6,000 chars
    silently answered it for 144 of 204 documents on a fraction of the text.
    """
    from app.sections import select_triage_text
    cfg = load_config()
    model = cfg.get("llm.triage_model")
    selection = select_triage_text(text, cfg)
    if selection.chunked:
        log.info("triage sampling: %d -> %d chars spanning the document",
                 selection.original_chars, selection.selected_chars)
    preface = ("Document text:" if not selection.chunked else
               "Evenly spaced excerpts spanning a long document (the head, then samples "
               "through to the end). Judge on what these show; do not assume the gaps "
               "are empty:")
    content = f"Source: {source}\nTitle: {title}\n\n{preface}\n{selection.text}"
    out = _tool_call(model, TRIAGE_SYSTEM, TRIAGE_TOOL, content, max_tokens=512, stage="triage")
    # Handed back so the caller can record how much of the document this verdict
    # was actually based on. A negative call on 15% of a filing is not the same
    # claim as one on all of it, and the difference has to survive to the row.
    out["_coverage"] = round(selection.coverage, 3)
    out["_original_chars"] = selection.original_chars
    return out


def extract(text: str, title: str = "", source: str = "", url: str = "") -> dict:
    """Returns the coerced extraction dict plus the raw model JSON under '_raw'
    and the section-chunking record under '_sections'.

    Long documents are NOT truncated at the head: section-aware chunking pulls
    the project description, utilities/energy, air quality, water, and noise
    sections (where MW and generator specs live) — see app/sections.py.
    """
    from app.sections import select_relevant_text
    cfg = load_config()
    model = cfg.get("llm.extract_model")
    selection = select_relevant_text(text, cfg)
    if selection.chunked:
        log.info("section chunking: %d -> %d chars, sections=%s",
                 selection.original_chars, selection.selected_chars,
                 ",".join(selection.sections_found))
    preface = ("Document text:" if not selection.chunked else
               "Selected sections of a long document (head + targeted sections; "
               "other sections omitted):")
    content = (
        f"Source: {source}\nURL: {url}\nTitle: {title}\n\n{preface}\n{selection.text}"
    )
    raw = _tool_call(model, EXTRACT_SYSTEM, EXTRACT_TOOL, content, max_tokens=4096, stage="extract")
    out = coerce_extraction(raw)
    out["_raw"] = raw
    out["_sections"] = {"chunked": selection.chunked,
                        "sections_found": selection.sections_found,
                        "original_chars": selection.original_chars,
                        "selected_chars": selection.selected_chars}
    return out


def adjudicate(record_a: dict, record_b: dict) -> dict:
    """Returns {verdict: match|no_match|uncertain, reasoning}."""
    cfg = load_config()
    model = cfg.get("llm.adjudicate_model")
    content = (
        "Record A:\n" + json.dumps(record_a, indent=2, default=str)
        + "\n\nRecord B:\n" + json.dumps(record_b, indent=2, default=str)
    )
    return _tool_call(model, ADJUDICATE_SYSTEM, ADJUDICATE_TOOL, content, max_tokens=1024, stage="adjudicate")


OUTREACH_SYSTEM = """You are summarizing a sales call transcript into a CRM outreach
log entry for an HVAC/mechanical equipment manufacturers' rep firm. Write what
actually happened on THIS call, specific to the project/deal discussed — not a
generic recap of the meeting format. If no next step was stated or clearly implied,
leave next_action null rather than inventing one. If no date or timeframe for the
next step was stated, leave next_action_date null rather than guessing."""

OUTREACH_TOOL = {
    "name": "outreach_log",
    "description": "Record what happened on this call for the outreach log.",
    "input_schema": {
        "type": "object",
        "properties": {
            "notes": {"type": "string",
                      "description": "2-4 sentence summary of what was discussed on "
                                     "this specific call"},
            "next_action": {"type": ["string", "null"],
                            "description": "The single next step, if one was stated "
                                           "or clearly implied. Null if none."},
            "next_action_date": {"type": ["string", "null"],
                                 "description": "ISO date (YYYY-MM-DD) for the next "
                                                "action, only if a specific date or "
                                                "timeframe was actually mentioned. "
                                                "Null otherwise -- do not guess."},
        },
        "required": ["notes", "next_action", "next_action_date"],
    },
}


def outreach_from_transcript(transcript: str, summary: str | None = None) -> dict:
    """Returns {notes, next_action, next_action_date} extracted from a Fathom
    call transcript. See app/pipeline/fathom_outreach.py."""
    cfg = load_config()
    model = cfg.get("llm.outreach_model", cfg.get("llm.extract_model"))
    content = transcript
    if summary:
        content = f"Meeting summary (from Fathom):\n{summary}\n\nFull transcript:\n{transcript}"
    return _tool_call(model, OUTREACH_SYSTEM, OUTREACH_TOOL, content,
                      max_tokens=1024, stage="fathom_outreach")


DC_NEWS_SYSTEM = """You extract facts from a data center trade-press article's title
and summary/description ONLY — never invent details beyond what this short text
states. This is used to match a news item against data center projects a sales
intelligence tool already tracks from other public filings, as a corroborating
signal, not to create new project records. Null over inference: if the developer,
location, or MW figure is not stated in this text, leave it null rather than
guessing."""

DC_NEWS_TOOL = {
    "name": "dc_news_facts",
    "description": "Record what this article title/summary states.",
    "input_schema": {
        "type": "object",
        "properties": {
            "developer": {"type": ["string", "null"],
                          "description": "The company developing/operating the data "
                                         "center, if named. Null if not stated."},
            "location_text": {"type": ["string", "null"],
                              "description": "City, county, or state named as the "
                                             "project's location, verbatim as stated. "
                                             "Null if not stated."},
            "mw": {"type": ["number", "null"],
                  "description": "The MW figure stated, if any. Null if not stated."},
            "is_data_center_project": {"type": "boolean",
                                       "description": "True only if this is about a "
                                                       "specific data center construction/"
                                                       "development project (not general "
                                                       "industry news, an executive move, "
                                                       "a funding round with no named site, etc)"},
        },
        "required": ["developer", "location_text", "mw", "is_data_center_project"],
    },
}


def dc_news_facts(title: str, summary: str) -> dict:
    """Returns {developer, location_text, mw, is_data_center_project} from an
    RSS entry's title+summary only — see app/pipeline/dc_news_enrichment.py."""
    cfg = load_config()
    model = cfg.get("llm.triage_model")  # cheap model: short text, simple extraction
    content = f"Title: {title}\n\nSummary: {summary}"
    return _tool_call(model, DC_NEWS_SYSTEM, DC_NEWS_TOOL, content,
                      max_tokens=256, stage="dc_news_enrichment")
