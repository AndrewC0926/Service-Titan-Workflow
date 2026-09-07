"""Pre-call brief: everything worth knowing about a project or contractor
before dialing, assembled from Scout's own data plus fresh web research, in
one page a rep can read in ninety seconds standing outside a building.

Hard constraint: this module never writes to Postgres. Every DB touch here
is a SELECT (session.get / session.exec against existing read helpers --
app.brief, app.ladder, app.accounts, app.contractors). The cache that makes
"open it twice" free is a plain JSON file on local disk (see CACHE_DIR), not
a database table -- and precisely because it's local disk on a Render web
service with no persistent volume attached (see render.yaml), it does NOT
survive a deploy or restart. That's an accepted tradeoff, not an oversight:
a rep opening the same brief twice before dialing is the case this needs to
be free for, and a brief is cheap enough to regenerate (see cost accounting
below) that losing the cache on a redeploy just means the next open pays for
one more brief, not that anything breaks.

Web research runs Claude's server-side web_search tool with free-form text
output, NOT app.llm._tool_call's forced-tool_choice pattern -- a forced tool
call can't also emit prose, and the point here is a written, readable brief
with citations woven into the sentences, not a JSON payload to render.

Cost is priced with the same $/MTok table app.spend.price() reads
(config.yaml's llm.prices) for consistency with every other LLM call in this
codebase, plus web_search's own per-search cost. The brief's own TEXT still
never touches Postgres -- only the cache file next to it does, and
precall_cost_report() still reads every cached brief's cost straight off
disk. The COST, however, now ALSO writes one app.spend.record() row per
call (2026-08-19): a precall brief runs ~$0.28 (measured), largely
uncapped and, until this change, entirely invisible to token_spend --
meaning a per-stage daily budget cap on precall_project/precall_contractor
(see llm.stage_daily_budget_usd in config.yaml) would have been enforced
against a number that was always zero. This is a narrow exception to "no
DB writes here": a TokenSpend insert is pure spend telemetry, not a Scout
entity, and it's the same write every other LLM call site in this app
already makes via app.llm._tool_call -- precall was the one place doing
its own thing.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path

from sqlmodel import Session, select

from app.config import Config, anthropic_api_key, load_config
from app.models import Contractor, Project, ProjectSignal, Signal

log = logging.getLogger(__name__)

# Repo-root-relative regardless of CWD -- app/precall.py's parent.parent is
# the repo root (/srv/dmg-scout in the deployed image). See module
# docstring for why this is local disk, never Postgres, and why that's fine.
CACHE_DIR = Path(__file__).resolve().parent.parent / "data" / "precall_cache"

WEB_SEARCH_COST_PER_CALL_USD = 0.01  # $10 per 1,000 searches, Anthropic's published rate


class PrecallUnavailable(Exception):
    pass


# ---- cache -----------------------------------------------------------

def _cache_path(entity_type: str, entity_id: int) -> Path:
    return CACHE_DIR / f"{entity_type}_{entity_id}.json"


def _read_cache(entity_type: str, entity_id: int) -> dict | None:
    path = _cache_path(entity_type, entity_id)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("precall cache unreadable at %s (%s) -- regenerating", path, exc)
        return None


def _write_cache(entity_type: str, entity_id: int, entry: dict) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    _cache_path(entity_type, entity_id).write_text(json.dumps(entry, indent=2, default=str))


def precall_cost_report() -> dict:
    """Every cached brief's generation cost, read straight off disk. Never
    touches Postgres or token_spend -- see module docstring."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    entries = []
    for path in sorted(CACHE_DIR.glob("*.json")):
        try:
            entries.append(json.loads(path.read_text()))
        except (OSError, json.JSONDecodeError):
            continue
    total = sum(e.get("cost_usd", 0.0) for e in entries)
    return {"n_briefs": len(entries), "total_cost_usd": round(total, 4), "entries": entries}


# ---- ingredient gathering (read-only) ---------------------------------

def _project_ingredients(session: Session, project_id: int) -> dict:
    from app.accounts import line_offering_by_role, project_facility_type
    from app.brief import build_brief
    from app.ladder import build_ladder
    from app.staleness import stage_ages

    b = build_brief(session, project_id)  # raises ValueError if missing
    p = b["project"]
    ladder = build_ladder(session, p)
    age = stage_ages(session, [p]).get(p.id)

    # facility_type/role_offerings need the raw Signal rows -- build_brief's
    # timeline is already flattened to plain dicts, so refetch exactly like
    # project_detail's own route does (app.web.main:project_detail).
    links = session.exec(select(ProjectSignal).where(ProjectSignal.project_id == project_id)).all()
    signals = [s for s in (session.get(Signal, link.signal_id) for link in links) if s]
    facility_type = project_facility_type(p, signals)
    role_offerings = line_offering_by_role(session, p.category, facility_type)

    from app.call_target import nearby_contractor_by_project, project_call_target
    cfg = load_config()
    nearby = nearby_contractor_by_project(session, cfg, [p])
    call_target = project_call_target(cfg, p, engineer_of_record=b["engineer_of_record"],
                                      gc=b["gc"], nearby_contractor=nearby.get(p.id))

    return {
        "entity_type": "project", "entity_id": p.id, "name": p.name,
        "project": p, "brief": b, "ladder": ladder, "stage_age": age,
        "role_offerings": role_offerings, "call_target": call_target,
    }


def _contractor_ingredients(session: Session, contractor_id: int) -> dict:
    from app.contractors import nearby_replacement_candidates, ranking_radius_miles

    c = session.get(Contractor, contractor_id)
    if c is None:
        raise ValueError(f"no contractor {contractor_id}")

    cfg = load_config()
    radius = ranking_radius_miles(cfg)
    triggers = None
    if c.latitude is not None:
        nearby = nearby_replacement_candidates(session, c, radius)
        triggers = {
            "radius_miles": radius,
            "n_nearby": len(nearby),
            "n_overdue": sum(1 for b in nearby if b.service_life_status == "overdue"),
            "n_due": sum(1 for b in nearby if b.service_life_status == "due"),
            "n_ebewe_arcx_due": sum(1 for b in nearby if b.ebewe_arcx_due_this_year),
            "n_sb1206_in_effect": sum(1 for b in nearby if b.sb1206_trigger_status == "in_effect"),
            "n_carb_candidate": sum(1 for b in nearby if b.carb_candidate),
            "most_urgent": nearby[0] if nearby else None,
        }

    return {
        "entity_type": "contractor", "entity_id": c.id, "name": c.business_name,
        "contractor": c, "regulatory_triggers": triggers,
    }


# ---- prompt ------------------------------------------------------------

PRECALL_SYSTEM = """You write a pre-call brief for a field rep at an HVAC/mechanical
equipment manufacturers' rep firm, standing outside a building about to make a call.
They will read this in about ninety seconds. Everything in it must earn its place.

You are given Scout's own internal data on one project or contractor (developer,
stage, contacts, prior outreach, license status, nearby regulatory triggers --
whatever applies to this entity) as a JSON block below. Treat that block as ground
truth about Scout's OWN records -- do not re-verify it, do not contradict it, just use
it.

Then use web_search to find what is NOT in Scout's own data and IS worth knowing
before this call: what the company actually does, its size and recent activity, and
any real news in roughly the last 12 months (a funding round, expansion, executive
change, layoffs, an incident, an award -- anything that would change what a rep says
on the phone). Search by the company/developer/contractor name plus its location; 2-4
searches is usually enough, do not pad with redundant queries. Keep any reasoning
between searches minimal -- narrate your plan only if it's genuinely useful to you,
never as commentary for the reader; the reader only ever sees the final brief below.

Discipline, absolute:
- Every fact that comes from the web carries its source inline, in this exact form:
  (Source Name, YYYY-MM-DD retrieved, URL). Never state a web-sourced fact without it.
- If you cannot corroborate something a rep would want to know (company size, whether
  they're still active, who currently runs the project), write "unknown" plainly.
  Never infer, estimate, or fill a gap with something plausible-sounding. This is the
  same discipline Scout's own extraction pipeline holds itself to -- guessing here is
  worse than an empty field, because a rep will act on what you write.
- Keep Scout's own data and web research visually distinct -- a rep must be able to
  tell at a glance which claims are internal record and which are today's web result.
- Under 500 words total, and say so if you had to cut something -- this is a page,
  not a dossier.

Structure, in this order, with these exact headers:
BOTTOM LINE -- one or two sentences: why this call matters right now, in plain
  language a rep could repeat on the phone in the first ten seconds.
WHO TO CALL -- name, phone/email, title, and how Scout knows them (source), straight
  from Scout's own data. If nobody is reachable, say so plainly.
SCOUT DATA -- the handful of internal facts that actually matter (stage/window, size,
  regulatory triggers, prior outreach) -- terse, not a restatement of the whole
  record.
FROM TODAY'S WEB RESEARCH -- what you found about the company, each fact cited inline
  as specified above. If web_search found nothing usable, say exactly that -- do not
  leave the section blank without explanation.

Write plain text meant to be read on a phone screen -- short paragraphs or tight
bullet lines, no markdown tables, no headings styled as markdown (write BOTTOM LINE:
not # Bottom Line). No preamble, no "Here is the brief" -- start directly with BOTTOM
LINE."""


def _serialize_project(ing: dict) -> dict:
    from app.accounts import SOCAL_CARD_DISCLOSURE
    from app.call_target import CALL_TARGET_LABELS
    p, b, age = ing["project"], ing["brief"], ing["stage_age"]
    ct = ing["call_target"]
    tons = None
    if p.tons_estimate_low:
        tons = (f"{p.tons_estimate_low:,.0f}-{p.tons_estimate_high:,.0f} tons"
                + (" (LOW CONFIDENCE -- sized from square footage)" if p.estimate_low_confidence else ""))
    stage_evidence = "no dated evidence on file"
    if age and age.days is not None:
        stage_evidence = f"{age.label} ago" + (" -- UNVERIFIED" if age.unverified() else " -- confirmed")
    ladder_rows = [
        {"rung": r["rung_label"], "name": r["name"], "title": r.get("title"),
         "phone": r.get("phone"), "email": r.get("email"), "source": r.get("source_url")}
        for r in ing["ladder"][:6]
    ]
    return {
        "name": p.name, "developer": p.developer, "county": p.county, "state": p.state,
        "category": p.category.value, "stage": p.stage.value, "window": p.window.value,
        "status": p.status, "stage_last_evidenced": stage_evidence,
        "days_to_estimated_bid": p.days_to_estimated_bid,
        "priority_score": round(p.score, 2) if p.score is not None else None,
        "estimated_tonnage": tons or "not yet estimated",
        "estimate_basis": p.estimate_basis,
        "equipment_value_range_usd": (
            f"${p.equipment_value_low:,.0f}-${p.equipment_value_high:,.0f}"
            if p.equipment_value_low else None),
        "engineer_of_record": b["engineer_of_record"], "general_contractor": b["gc"],
        "call_target": {
            "type": ct.target.value, "label": CALL_TARGET_LABELS[ct.target],
            "rule": ct.rule, "reason": ct.reason,
            "who": ct.who_label, "who_detail": ct.who_detail,
        },
        "notes": p.notes, "next_action": p.next_action,
        "contact_ladder": ladder_rows or "no contact at any rung -- nobody named on this project yet",
        "prior_outreach": [
            {"date": o.date.strftime("%Y-%m-%d"), "channel": o.channel, "notes": o.notes,
             "next_action": o.next_action}
            for o in b["outreach"][:5]
        ] or "none logged",
        "recent_signals": [
            {"date": t["date"].strftime("%Y-%m-%d"), "type": t["type"], "summary": t["summary"],
             "url": t["url"]}
            for t in b["timeline"][:6]
        ],
        "line_card_roles_that_apply": [
            {"role": ro.label, "gap": ro.gap, "lines": [ln.name for ln in ro.lines]}
            for ro in ing["role_offerings"] if ro.relevant
        ] or "not determined (facility type unknown)",
        "line_card_scope": SOCAL_CARD_DISCLOSURE,
        "regulatory_triggers": ("not applicable to this project -- SB1206/EBEWE/CARB triggers are "
                                "tracked for the separate retrofit-building population, not "
                                "early-signal projects like this one"),
    }


def _serialize_contractor(ing: dict) -> dict:
    c, triggers = ing["contractor"], ing["regulatory_triggers"]
    trig_summary = "unknown -- this contractor has not been geocoded, so nearby buildings can't be found"
    if triggers is not None:
        mu = triggers["most_urgent"]
        most_urgent = None
        if mu is not None:
            most_urgent = mu.address or f"APN {mu.apn}"
            if mu.service_life_status:
                most_urgent += f" -- {mu.service_life_status}"
                if mu.service_life_years_past is not None:
                    most_urgent += f", {mu.service_life_years_past:+.0f}yr past service life"
        trig_summary = {
            "nearby_replacement_candidate_buildings": triggers["n_nearby"],
            "within_miles": triggers["radius_miles"],
            "overdue": triggers["n_overdue"], "due": triggers["n_due"],
            "ebewe_arcx_due_this_year": triggers["n_ebewe_arcx_due"],
            "sb1206_in_effect": triggers["n_sb1206_in_effect"],
            "carb_candidate": triggers["n_carb_candidate"],
            "most_urgent_nearby_building": most_urgent,
        }
    return {
        "business_name": c.business_name, "license_no": c.license_no,
        "classifications": c.classifications, "license_status": c.primary_status,
        "secondary_status": c.secondary_status, "phone": c.business_phone,
        "address": f"{c.business_address}, {c.city}, {c.state} {c.zip_code}",
        "county": c.county, "workers_comp": c.workers_comp_coverage_type,
        "ua_local_250_signatory": c.ua_local_250_signatory,
        "license_expires": c.expiration_date.strftime("%Y-%m-%d") if c.expiration_date else None,
        "nearby_regulatory_triggers": trig_summary,
        "line_card_roles_that_apply": ("not tracked at the contractor level in Scout -- see "
                                       "nearby regulatory triggers above for why nearby buildings "
                                       "might need mechanical work"),
        "prior_outreach": "not tracked for contractors in this system -- Scout's outreach log "
                          "only covers projects",
    }


def _build_prompt(entity_type: str, ingredients: dict) -> tuple[str, str]:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if entity_type == "project":
        payload = _serialize_project(ingredients)
    else:
        payload = _serialize_contractor(ingredients)
    header = f"{entity_type.upper()} #{ingredients['entity_id']}: {ingredients['name']}"
    user_content = (
        f"Today's date: {today}\n{header}\n\n"
        "Scout's own data (ground truth about Scout's records -- do not re-verify):\n"
        f"{json.dumps(payload, indent=2, default=str)}\n\n"
        "Research the company/developer/contractor named above with web_search, then "
        "write the brief."
    )
    return PRECALL_SYSTEM, user_content


# ---- LLM call ------------------------------------------------------------

def _client():
    import anthropic
    key = anthropic_api_key()
    if not key:
        raise PrecallUnavailable("ANTHROPIC_API_KEY is not set")
    return anthropic.Anthropic(api_key=key)


def _call_llm(cfg: Config, system: str, user_content: str, *, stage: str, max_uses: int = 5) -> dict:
    from app.spend import check_budget, price, record

    check_budget(stage)  # global, run, AND this stage's own daily cap -- see module docstring
    model = cfg.get("llm.precall_model", cfg.get("llm.extract_model"))
    client = _client()
    resp = client.messages.create(
        model=model,
        # Generous relative to the ~500-word brief itself. web_search_20260209's
        # dynamic filtering runs its own code_execution rounds AND extended
        # thinking against every search (measured 2026-08-17: ~1,600 thinking
        # tokens on a real 3-search call, not requested via a `thinking` param
        # -- Sonnet 5 turns it on itself for this tool). All of that is output
        # tokens against the SAME max_tokens budget the final brief text has
        # to fit in. 1400 and 3000 both measured truncating the brief itself
        # mid-sentence (stop_reason="max_tokens") before this was raised.
        max_tokens=8000,
        system=system,
        tools=[{"type": "web_search_20260209", "name": "web_search", "max_uses": max_uses}],
        messages=[{"role": "user", "content": user_content}],
    )
    # Only the TRAILING run of text blocks is the finished brief. When the
    # model uses web_search across several rounds, it emits its own
    # between-search narration ("Good, that confirms X, let me check Y
    # next") as earlier text blocks interleaved with each search's
    # server_tool_use/web_search_tool_result pair -- joining every text
    # block indiscriminately puts that narration on the page a rep reads.
    # Everything after the LAST non-text block is the answer the system
    # prompt asked for ("start directly with BOTTOM LINE").
    last_non_text = max((i for i, b in enumerate(resp.content) if b.type != "text"), default=-1)
    text = "".join(b.text for b in resp.content[last_non_text + 1:] if b.type == "text").strip()
    n_searches = sum(1 for block in resp.content
                     if block.type == "server_tool_use" and block.name == "web_search")
    token_cost = price(cfg, model, resp.usage.input_tokens, resp.usage.output_tokens)
    search_cost = n_searches * WEB_SEARCH_COST_PER_CALL_USD
    # extra_cost_usd folds the flat web_search charge into this ONE row rather
    # than a second row per call -- see app.spend.record's own docstring.
    record(stage, model, resp.usage.input_tokens, resp.usage.output_tokens,
          extra_cost_usd=search_cost)
    log.info("precall %s: model=%s in=%d out=%d searches=%d cost=$%.4f",
             stage, model, resp.usage.input_tokens, resp.usage.output_tokens, n_searches,
             token_cost + search_cost)
    return {
        "text": text, "model": model,
        "input_tokens": resp.usage.input_tokens, "output_tokens": resp.usage.output_tokens,
        "web_searches": n_searches,
        "token_cost_usd": round(token_cost, 4), "search_cost_usd": round(search_cost, 4),
        "cost_usd": round(token_cost + search_cost, 4),
    }


# ---- rendering -----------------------------------------------------------

_SECTION_HEADERS = ["BOTTOM LINE", "WHO TO CALL", "SCOUT DATA", "FROM TODAY'S WEB RESEARCH"]
_SECTION_PATTERN = re.compile(
    r"^(" + "|".join(re.escape(h) for h in _SECTION_HEADERS) + r")\s*:?\s*$",
    re.IGNORECASE | re.MULTILINE,
)


def _parse_blocks(text: str) -> list[dict]:
    blocks = []
    for chunk in re.split(r"\n\s*\n", text.strip()):
        lines = [ln.strip() for ln in chunk.splitlines() if ln.strip()]
        if not lines:
            continue
        if all(ln.startswith(("-", "•", "*")) for ln in lines):
            # "lines", not "items" -- Jinja's dot-access on a plain dict tries
            # getattr() first, and dict.items is a bound builtin method that
            # would silently shadow a dict key of that exact name in the
            # template (confirmed: {% for x in piece.items %} raised
            # "'builtin_function_or_method' object is not iterable").
            blocks.append({"type": "bullets", "lines": [ln.lstrip("-•* ").strip() for ln in lines]})
        else:
            blocks.append({"type": "para", "text": " ".join(lines)})
    return blocks


def brief_sections(text: str) -> list[dict]:
    """Split a generated brief's plain text into its four labeled sections
    for rendering -- see PRECALL_SYSTEM's required header list. Falls back
    to one unlabeled section if the model didn't emit the exact headers
    (free-form text output, so not guaranteed) -- a formatting miss degrades
    to a plain read, never a blank page."""
    matches = list(_SECTION_PATTERN.finditer(text))
    if not matches:
        return [{"header": None, "blocks": _parse_blocks(text)}]
    sections = []
    if matches[0].start() > 0:
        lead = text[:matches[0].start()].strip()
        if lead:
            sections.append({"header": None, "blocks": _parse_blocks(lead)})
    for i, m in enumerate(matches):
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        sections.append({"header": m.group(1).upper(), "blocks": _parse_blocks(text[start:end])})
    return sections


# ---- orchestrator --------------------------------------------------------

def pre_call_brief(session: Session, entity_type: str, entity_id: int, *,
                   force_refresh: bool = False) -> dict:
    """Everything worth knowing before dialing entity_type #entity_id
    ("project" or "contractor"), from cache unless force_refresh or nothing
    is cached yet. Raises ValueError if the entity doesn't exist,
    PrecallUnavailable if ANTHROPIC_API_KEY isn't set, BudgetExceeded if
    today's LLM spend cap is already hit."""
    if entity_type not in ("project", "contractor"):
        raise ValueError(f"entity_type must be 'project' or 'contractor', got {entity_type!r}")

    if not force_refresh:
        cached = _read_cache(entity_type, entity_id)
        if cached is not None:
            return {**cached, "from_cache": True}

    if entity_type == "project":
        if session.get(Project, entity_id) is None:
            raise ValueError(f"no project {entity_id}")
        ingredients = _project_ingredients(session, entity_id)
    else:
        ingredients = _contractor_ingredients(session, entity_id)  # raises ValueError itself

    system, user_content = _build_prompt(entity_type, ingredients)
    cfg = load_config()
    result = _call_llm(cfg, system, user_content, stage=f"precall_{entity_type}")

    now = datetime.now(timezone.utc)
    entry = {
        "entity_type": entity_type, "entity_id": entity_id, "name": ingredients["name"],
        "generated_at": now.isoformat(),  # machine-parseable, for the cache file and cost report
        "generated_at_display": now.strftime("%Y-%m-%d %H:%M") + "Z",  # for MCP text / template use
        "text": result["text"], "model": result["model"],
        "input_tokens": result["input_tokens"], "output_tokens": result["output_tokens"],
        "web_searches": result["web_searches"],
        "token_cost_usd": result["token_cost_usd"], "search_cost_usd": result["search_cost_usd"],
        "cost_usd": result["cost_usd"],
    }
    _write_cache(entity_type, entity_id, entry)
    return {**entry, "from_cache": False}
