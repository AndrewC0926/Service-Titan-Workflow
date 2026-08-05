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
                "enum": ["data_center", "industrial", "other"],
                "description": "data_center = operates computing capacity; "
                               "industrial = any other substantial new/expanding "
                               "building; other = not relevant",
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
- named_people: only people named in the document with a role related to the project.
- confidence reflects how clearly the document supports the extracted fields overall.
- summary_one_line: one factual sentence, no speculation."""

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
