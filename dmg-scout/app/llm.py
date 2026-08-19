"""Anthropic API layer: cheap triage (Haiku), structured extraction and entity
adjudication (Sonnet). Tool-use forces valid JSON; the models are instructed to
return null rather than guess."""
from __future__ import annotations

import json
import logging
from contextvars import ContextVar

import anthropic

from app.config import anthropic_api_key, load_config
from app.schemas import EXTRACTION_JSON_SCHEMA, OutreachCallExtraction, coerce_extraction

log = logging.getLogger(__name__)

# Same ContextVar pattern app.spend uses for "the currently active run" and
# app.pipeline_health for "the currently active pipeline_run" -- lets a
# caller read what the call it JUST made cost without changing _tool_call's
# return shape, which every other caller in this file already treats as a
# plain dict. Set at the end of _tool_call; read via last_call_cost_usd().
_last_call_cost_usd: ContextVar[float | None] = ContextVar("_last_call_cost_usd", default=None)


def last_call_cost_usd() -> float | None:
    """Cost of the most recent _tool_call in this task/thread, or None if
    none has run yet. See app.pipeline.voice_capture for the one caller
    that needs a per-call cost figure rather than just the daily total."""
    return _last_call_cost_usd.get()

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
beats any inference from the company's description of itself.

water_source_stated / water_reclaimed_identified / water_use_efficiency_stated /
water_opposition_stated — capture these as four SEPARATE facts, each null unless the
document states it. Do not collapse them into one judgment about whether water is
"an issue":
- water_source_stated: the water source as the document describes it, e.g. "recycled
  water from West Basin Municipal Water District", "municipal potable supply",
  "on-site groundwater wells". Quote or closely paraphrase; do not summarize away the
  specifics.
- water_reclaimed_identified: true ONLY if the document identifies a recycled,
  reclaimed, non-potable, or "purple pipe" supply (also: NPW, tertiary-treated
  effluent, recycled water district service). false ONLY if the document explicitly
  states a potable or municipal-domestic source with no recycled component mentioned.
  Null if water source is not discussed, or the document is ambiguous about which it
  is — do not guess from the jurisdiction or infer recycled availability from the
  water district's name alone.
- water_use_efficiency_stated: any stated water-use figure, with its units exactly as
  given — a WUE (e.g. "0.20 L/kWh"), a daily/annual volume, an acre-feet figure
  outside the water_acre_feet_per_year field's own format. Do not convert units or
  compute a WUE from other numbers.
- water_opposition_stated: true only if the document itself records public
  opposition, an objection, a comment letter, or a hearing continuance citing water
  use or availability for THIS project. This is about what the document says
  happened, not a general impression that water is a controversial topic in the
  jurisdiction. Null when not discussed.

delivery_method — how the project is being procured, which decides WHO selects the
mechanical equipment. Report it ONLY when the document itself names the delivery
method; never infer it from project type, owner, agency, or stage. A school district
or hospital project is NOT automatically design-bid-build, and a tech-company
project is NOT automatically design-build — guessing from the owner type is exactly
the mistake this field exists to prevent. Recognize these terms and their common
phrasings:
- design_bid_build: "design-bid-build", "DBB", "traditional design-bid-build
  delivery", a filing that separately names an architect/engineer of record AND a
  general contractor selected through competitive bid on completed plans.
- design_build: "design-build", "design/build", "DB delivery", "design-build
  entity", a single firm or joint venture holding both design and construction.
- design_assist: "design-assist", "design assist", "early contractor involvement
  with design input", a subcontractor or trade contractor joining the design team
  before construction documents are complete.
- cm_at_risk: "CM at risk", "CMAR", "CM@Risk", "construction manager at risk",
  "construction manager/general contractor", "CMGC" — a construction manager holds
  the GC contract and commits to a guaranteed maximum price before design is fully
  complete.
- progressive_design_build: "progressive design-build", "PDB", "progressive
  design/build" — design-build where scope and price are negotiated in phases
  rather than fixed at award.
Null when the document doesn't use language like this — most public filings never
state a delivery method at all, and that absence is the honest, expected answer,
not a gap to fill with a guess."""

EXTRACT_TOOL = {
    "name": "record_extraction",
    "description": "Record the structured extraction.",
    "input_schema": EXTRACTION_JSON_SCHEMA,
}

SAM_GOV_SPEC_SYSTEM = """You extract manufacturer mentions from UFGS Division 23 (Heating,
Ventilating, and Air Conditioning) sections of a federal construction specification. You
are given only the 23-series UFGS section text (e.g. "SECTION 23 64 26"), which may be one
section from a standalone per-section file, or several consecutive 23-series sections from
a combined spec book -- either way, treat every distinct "SECTION 23 XX XX" heading in the
text as its own section.

FAR 11.104 and 11.105 discourage brand-name specifications in federal procurement, so a
LOT of what you see will specify by PERFORMANCE or salient characteristics (capacity,
efficiency, dimensions, materials, testing standards) rather than naming a manufacturer at
all. That is the NORMAL, EXPECTED case for federal work, not a failure to find something --
record it as such via performance_spec_only, do not leave it looking like an empty miss.

For each 23-series spec SECTION, record:
  - spec_section: the section number as written (e.g. "23 64 26")
  - spec_section_title: the section title as written
  - basis_of_design_manufacturer: the SINGLE manufacturer named as "Basis of Design" (BOD),
    "Basis-of-Design Product", or equivalent language for that section -- null if the
    section lists only acceptable manufacturers with no single BOD singled out, or if it
    specifies by performance only.
  - or_equal_manufacturers: every OTHER manufacturer named as an acceptable substitute
    ("or equal", "or approved equal", "acceptable manufacturers", "approved equals") for
    that section -- do NOT include the basis_of_design_manufacturer again in this list.

Also record specifying_firm: the architect-of-record or engineer-of-record firm name, if
stated (usually on a title/cover sheet, professional stamp block, or "prepared by" line).
Null if not stated in the text you were given.

Also record performance_spec_only: true if you found real 23-series section content but
NO section anywhere in it names any manufacturer at all -- every requirement is written as
performance/salient-characteristic language (capacity, SEER/EER, sound rating, materials,
referenced standards like ASHRAE/AMCA/UL) with no brand named. false if at least one
section names a manufacturer (basis-of-design or or-equal). This is a real, common,
EXPECTED outcome per FAR 11.104/11.105 -- do not treat it as "nothing found."

Only extract manufacturers EXPLICITLY named in the spec text. Never infer a manufacturer
from a product description, model number pattern, or general industry knowledge. If a
section names no manufacturers at all, omit that section from the sections list rather
than recording it empty (performance_spec_only is what carries that signal, not an empty
sections entry). If nothing in the given text is actually 23-series UFGS content at all,
return an empty sections list and performance_spec_only: false -- do not force a result."""

SAM_GOV_SPEC_TOOL = {
    "name": "record_division_23_mentions",
    "description": "Record the specifying firm and manufacturer mentions found in UFGS Division 23 sections.",
    "input_schema": {
        "type": "object",
        "properties": {
            "specifying_firm": {"type": ["string", "null"]},
            "performance_spec_only": {
                "type": "boolean",
                "description": "True if real 23-series content was found but it specifies by "
                              "performance/salient characteristics only, per FAR 11.104/11.105, "
                              "with no manufacturer named anywhere.",
            },
            "sections": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "spec_section": {"type": "string"},
                        "spec_section_title": {"type": "string"},
                        "basis_of_design_manufacturer": {"type": ["string", "null"]},
                        "or_equal_manufacturers": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["spec_section", "spec_section_title", "or_equal_manufacturers"],
                },
            },
        },
        "required": ["sections", "performance_spec_only"],
    },
}


def extract_division_23_mentions(division_23_text: str, *, title: str = "", url: str = "") -> dict:
    """Returns {specifying_firm, performance_spec_only, sections: [...]}. See
    SAM_GOV_SPEC_SYSTEM/TOOL. Caller (app.pipeline.sam_gov) is responsible for
    having already located and trimmed the 23-series UFGS portion of the
    document -- this function does not search a full spec book."""
    cfg = load_config()
    model = cfg.get("llm.extract_model")
    content = f"URL: {url}\nTitle: {title}\n\n23-series UFGS text:\n{division_23_text}"
    return _tool_call(model, SAM_GOV_SPEC_SYSTEM, SAM_GOV_SPEC_TOOL, content,
                      max_tokens=4096, stage="sam_gov_extract")


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
    check_budget(stage)  # raises BudgetExceeded past the daily, run, or per-stage cap
    resp = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=system,
        tools=[tool],
        tool_choice={"type": "tool", "name": tool["name"]},
        messages=[{"role": "user", "content": user_content}],
    )
    cost = record(stage, model, resp.usage.input_tokens, resp.usage.output_tokens)
    _last_call_cost_usd.set(cost)
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


VOICE_CAPTURE_SYSTEM = """You extract structured facts about a sales call from a voice-note
transcript for an HVAC/mechanical equipment manufacturers' rep firm. The transcript comes
from a phone recording, not a written report — expect filler words, false starts, and
misheard proper nouns from the transcription step itself; extract what the transcript
actually says, not what you assume the caller meant.

Rules — these are absolute:
- NEVER guess or infer a value. If the transcript does not clearly state a field, return
  null for it. A transcript that never gives a next-step date must leave next_action_date
  null — do not default to "today", "tomorrow", or any other assumed date.
- Do not correct, normalize, or "clean up" a name (person, firm, project, or building).
  Transcribe it as heard, even if it sounds like it could be a mishearing — a downstream
  fuzzy-match step handles reconciling it against known names, and normalizing it here
  would hide the very ambiguity that step needs to see.
- outcome is a factual account of what happened on THIS call, specific to the deal or
  project discussed — not a generic "had a call" summary. 1-3 sentences.
- stage is the caller's own words for where the deal/relationship stands (e.g.
  "prequalified", "bid submitted", "awarded the job", "lost to a competitor"), verbatim or
  closely paraphrased. Null if the caller didn't characterize a stage.
- confidence reflects how clearly the transcript supports the extraction overall — a
  garbled or very short transcript should score low even if a few fields came through
  clearly."""

VOICE_CAPTURE_TOOL = {
    "name": "record_outreach_call",
    "description": "Record the structured facts this call transcript supports.",
    # The Anthropic tool input_schema IS OutreachCallExtraction's own JSON
    # schema (see that class's docstring) -- title/top-level description are
    # stripped here since they'd otherwise duplicate this dict's own
    # "description" and burn tokens on the class's Python docstring.
    "input_schema": {
        k: v for k, v in OutreachCallExtraction.model_json_schema().items()
        if k not in ("title", "description")
    },
}


NARRATE_DIGEST_SYSTEM = """You turn a structured sales-intelligence digest into a short morning
briefing for an HVAC manufacturers' rep, meant to be skimmed on a phone screen before 6am.

Rules — these are absolute:
- NEVER introduce a fact, a number, or an inference that is not already present in the
  structured data you are given. You are REPHRASING data someone else already looked up, not
  researching, estimating, or summarizing your own general knowledge of the situation.
- Every number you use must come from the input, unchanged. A tonnage band stays a band — do
  not collapse "500-800 tons" into one figure or an average. A day count that carries a
  confidence interval keeps that qualifier attached, not dropped. A score keeps whatever
  precision it was given. Do not round, do not estimate a midpoint, do not add a unit the
  input doesn't already carry.
- Do not soften or resolve stated uncertainty. If a figure is qualified as modeled, estimated,
  unverified, or a range, the qualifier must survive into the prose — smoothing it into a
  clean, confident-sounding number would misrepresent what the system actually knows.
- If a section of the input is empty or says nothing happened, say so plainly in one short
  line — do not pad it with generic filler, encouragement, or invented color to make the
  section feel fuller than it is.
- Keep names, counties, and stage/window labels exactly as given — do not paraphrase a proper
  noun or invent a nickname for it.
- Plain prose written the way a sharp colleague would text a short morning update — short
  paragraphs or tight lines, no markdown headers, no bullet-dash lists pretending to be prose.
- Under 200 words total, covering every section given (calls to make, what changed, what's
  due, and the one thing worth knowing) — skip a section only if its input was empty."""

NARRATE_DIGEST_TOOL = {
    "name": "narrated_digest",
    "description": "Record the narrated digest prose.",
    "input_schema": {
        "type": "object",
        "properties": {
            "body": {"type": "string",
                     "description": "The full narrated digest as plain prose, under 200 words, "
                                    "using only facts and numbers present in the input."},
        },
        "required": ["body"],
    },
}


def narrate_digest(payload: dict) -> dict:
    """Returns {body: str}. Rephrases an already-assembled digest payload
    (see app.pipeline.notify._structured_payload) into prose -- never
    researches or computes anything itself. Cheap model (llm.narrate_model)
    on purpose: this is rewording, not extraction or judgment. Caller
    (app.pipeline.notify.narrate_digest) is responsible for validating the
    result (schema is enforced here via forced tool-use, but the grounding
    check -- every number in the output must appear in the input -- is not,
    since it needs the same payload this function was given) and falling
    back to the plain-text digest on any failure -- this function raises
    rather than swallows, same as every other call in this file."""
    cfg = load_config()
    model = cfg.get("llm.narrate_model", cfg.get("llm.triage_model"))
    content = ("Structured digest data (JSON) -- rephrase this into prose, using nothing else:\n"
              + json.dumps(payload, indent=2, default=str))
    return _tool_call(model, NARRATE_DIGEST_SYSTEM, NARRATE_DIGEST_TOOL, content,
                      max_tokens=700, stage="narrate_digest")


def extract_voice_capture(transcript: str) -> OutreachCallExtraction:
    """Constrained decoding (forced tool_choice against OutreachCallExtraction's
    own schema) PLUS explicit Pydantic validation -- the tool_choice force is
    not trusted alone. Raises pydantic.ValidationError if the model's output
    doesn't actually validate (e.g. an out-of-range confidence, a malformed
    date) -- see app/pipeline/voice_capture.py for how the caller handles
    that without losing the underlying transcript."""
    cfg = load_config()
    model = cfg.get("llm.voice_capture_model", cfg.get("llm.outreach_model"))
    raw = _tool_call(model, VOICE_CAPTURE_SYSTEM, VOICE_CAPTURE_TOOL, transcript,
                     max_tokens=1024, stage="voice_capture")
    return OutreachCallExtraction.model_validate(raw)
