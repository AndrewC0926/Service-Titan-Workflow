"""Plain-language search over the board.

"natural language to SQL" is the usual framing and it is the wrong one here. This
translates a question into the SAME validated criteria dict that saved searches
use, and app/searches.py executes it. The model never emits SQL and never touches
the database.

That choice is the whole design, and the reasons are the ones this codebase keeps
relearning:

  - **Every criterion is grounded.** The criteria vocabulary is a closed set
    (searches.CRITERIA_KEYS), the enums are closed sets, and an unknown key
    raises. A model that hallucinates a filter gets an error, not a silent
    match-everything — which is the exact failure mode invariant 4 exists for,
    one layer up.
  - **No fabrication surface.** Generated SQL can invent a column, join the wrong
    table, or quietly return a plausible number. A criteria dict can be printed
    back to the user in full and checked by eye before they trust the answer.
  - **It is auditable.** The dict is shown on the results page and can be saved as
    a saved search in one click, which is the natural next thing a rep wants and
    is impossible if the intermediate artefact is a SQL string nobody can read.

The cost is real: questions outside the vocabulary cannot be answered, and the
honest response is to say so rather than approximate. `interpret` returns the
unsupported parts so the page can show them.
"""
from __future__ import annotations

import json
import logging

from app.llm import LLMUnavailable, _tool_call
from app.models import Category, Stage, Window
from app.searches import CRITERIA_KEYS, UnknownCriterion, validate

log = logging.getLogger(__name__)

NL_SYSTEM = """You translate a salesperson's question about a construction-project
board into a structured filter. You do NOT answer the question and you do NOT write
SQL — you only say which filter expresses it.

The board holds commercial construction and retrofit opportunities for an HVAC
manufacturers' rep in California, Nevada and Arizona. Each project has a county and
state, a category, a stage, a buying window, a score, an estimated tonnage, and
sometimes a megawatt figure.

Fill ONLY the fields that the question actually constrains. Leaving a field out is
always better than guessing at it: an unasked-for filter silently removes rows the
person wanted to see, and they cannot tell that it happened.

Put anything the vocabulary cannot express into `unsupported`, in the user's own
words. Never approximate it into a filter that is merely nearby — "projects where
the mechanical engineer is named" is not "min_score 0.5", and answering it as
though it were is worse than saying it cannot be answered.

county takes a bare county name with no "County" suffix ("Storey", "Los Angeles").
names takes company names to look for anywhere on the project — developer, linked
firm, or a firm named in a filing."""

NL_TOOL = {
    "name": "board_filter",
    "description": "Express the question as a board filter.",
    "input_schema": {
        "type": "object",
        "properties": {
            "min_mw": {"type": "number", "description": "Minimum megawatts (IT or total)"},
            "max_mw": {"type": "number"},
            "county": {"type": "string", "description": "Bare county name, no suffix"},
            "state": {"type": "string", "enum": ["CA", "NV", "AZ"]},
            "category": {"type": "string",
                         "enum": [c.value for c in Category]},
            "stage": {"type": "string", "enum": [s.value for s in Stage]},
            "window": {"type": "string", "enum": [w.value for w in Window]},
            "min_score": {"type": "number"},
            "min_tons": {"type": "number"},
            "names": {"type": "array", "items": {"type": "string"},
                      "description": "Company names to match anywhere on the project"},
            "unsupported": {
                "type": "array", "items": {"type": "string"},
                "description": "Parts of the question this vocabulary cannot express, "
                               "in the user's own words. Do not approximate them.",
            },
            "reading": {"type": "string",
                        "description": "One sentence: how you read the question"},
        },
        "required": ["reading"],
    },
}


class Uninterpretable(ValueError):
    """The question produced no usable filter."""


def interpret(question: str) -> dict:
    """Question -> {"criteria": {...}, "unsupported": [...], "reading": str}.

    Raises LLMUnavailable when there is no API key or the account cannot run
    inference — the caller must surface that rather than silently returning an
    empty filter, which would render as "no matches" and read like an answer.
    """
    from app.config import load_config
    # Haiku by default: this is a short, closed-vocabulary translation, not a
    # reasoning task, and it runs interactively while someone waits.
    model = load_config().get("llm.triage_model")
    out = _tool_call(model, NL_SYSTEM, NL_TOOL, f"Question: {question}",
                     max_tokens=512, stage="nlsearch")
    unsupported = [u for u in (out.pop("unsupported", None) or []) if u]
    reading = str(out.pop("reading", "") or "")

    criteria = {k: v for k, v in out.items()
                if k in CRITERIA_KEYS and v not in (None, "", [], {})}
    # The model is constrained by the tool schema, but a schema is a request, not
    # a guarantee. Validate anyway — this is the boundary between a generated
    # value and a database query.
    validate(criteria)
    for key, enum in (("category", Category), ("stage", Stage), ("window", Window)):
        if key in criteria:
            try:
                enum(criteria[key])
            except ValueError as exc:
                raise UnknownCriterion(
                    f"model returned {key}={criteria[key]!r}, which is not a "
                    f"valid {enum.__name__}") from exc

    if not criteria and not unsupported:
        raise Uninterpretable(
            "That question did not map onto anything the board can filter on. "
            "Try naming a county, a company, a size or a stage.")
    log.info("nlsearch %r -> %s (unsupported: %s)", question,
             json.dumps(criteria), unsupported)
    return {"criteria": criteria, "unsupported": unsupported, "reading": reading}
