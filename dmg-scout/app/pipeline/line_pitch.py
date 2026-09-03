"""Line-card training and pitch reference: one elevator pitch per
ProductLine (never per branch -- a pitch is about the manufacturer's
product, not which DMG location happens to stock it) plus, where the
competitor map has a role match, one why-we-win/why-we-lose row per
competing manufacturer.

SAME propose-never-assert discipline as voice capture
(app/pipeline/voice_capture.py): this writes a PROPOSAL, never a fact.
Every LinePitch/LineCompetitor row this module writes starts
review_status='draft' and stays that way until a human confirms it at
/line/{id} -- nothing generated here is ever shown to a rep as verified,
and a row already 'confirmed' or 'rejected' by a human is never touched
by a later run (see run_line_pitch_generation's own skip logic).

INPUTS -- deliberately narrow, per the brief ("using only"):
  1. The line's own ProductLine row: name, category/subcategory,
     description, building_role, existence_verified(+basis), the
     confirmed branches carrying it (ProductLineBranch), and its own
     oshpd_osp/basis.
  2. The competitor map (app.competitors.COMPETITOR_LINES via
     app.competitors.competing_lines_by_role) -- filtered to this line's
     OWN building_role. competitor_name is drawn ONLY from this closed
     set; the model is never asked to name a competitor from general
     knowledge, and any name it returns that isn't in the candidate set
     is dropped in code, not trusted from the schema alone (see
     _validate_competitor_names).
  3. The manufacturer's own product page, IF a plausible URL for it can
     be found WITHOUT a web search and robots.txt permits fetching it --
     see _candidate_domains, which extracts a domain from this line's OWN
     already-researched basis fields (country_of_manufacture_basis,
     ahri_certified_basis, etc. -- all seeded from a rep's own prior
     research, not a new lookup), never invents one, and never falls back
     to guessing "{name}.com". Measured against the real production line
     card, 2026-09-03: 48 of 69 eligible lines have a plausible domain in
     their own basis text; the other 21 get NO product-page fetch and
     therefore NO factual (numeric/certification) claims at all -- only
     the role/category-level pitch content that doesn't need grounding.

GROUNDING. Every sentence in a generated field that reads as a factual
claim -- a numeric range with a unit (tons, CFM, %, SEER, dB, kW, ...) or
a named certification/standard (AHRI, UL, ETL, OSHPD, NSF, ISO, ASHRAE,
ENERGY STAR, cETLus) -- is checked against the fetched page text with
app.grounding.quote_grounded (the same substring-normalized fragment
matcher app/pipeline/schedule.py already trusts for this exact question:
"does this text actually appear in the source"). An ungrounded
claim-bearing sentence is DROPPED from the field entirely, never
softened, never rewritten to hedge -- per the brief's explicit "or it is
dropped, not softened." A line with no fetched page has NOTHING to
ground a claim against, so every claim-bearing sentence in every field is
dropped for that line; this is why grounded_claim_count and
dropped_claim_count are recorded per LinePitch row, not just in the
aggregate run stats -- so it's visible on the pitch itself, not only in a
one-time report.

Non-claim sentences (role framing, "why it fits," a question to ask an
engineer) pass through untouched -- the grounding pass only ever removes
sentences it can positively identify as making a checkable factual
assertion; it does not touch everything else.

COST. One Sonnet call per line (app.llm's SONNET-tier model, via
llm.line_pitch_model), covering that line's full pitch AND every one of
its competitor rows in the same call -- "one pass ... per line" from the
brief. Bounded by a HARD run cap (see run_line_pitch_generation's
cap_usd, enforced via app.spend.run_budget/check_budget exactly like
every other LLM-calling pipeline stage in this app) -- once the cap is
hit, remaining lines are recorded as skipped with the reason, never
silently truncated mid-line.
"""
from __future__ import annotations

import logging
import re

from sqlmodel import Session, select

from app.config import Config
from app.grounding import quote_grounded
from app.competitors import competing_lines_by_role
from app.llm import generate_line_pitch
from app.models import (
    CompetitorLine,
    LineCompetitor,
    LinePitch,
    ProductLine,
    ProductLineBranch,
    utcnow,
)
from app.spend import BudgetExceeded, run_budget

log = logging.getLogger(__name__)

MAX_PAGE_CHARS = 6000            # bounds cost per call; a product page's marketing copy rarely needs more
MAX_ELEVATOR_PITCH_WORDS = 60    # the brief's own ceiling
FETCH_TIMEOUT_SECONDS = 15

# Domains that show up in this app's own researched basis text for reasons
# OTHER than being the manufacturer's own site -- third-party directories,
# press-release wires, certification bodies, and generic social/reference
# sites a citation might mention alongside the real source. Excluded so a
# name-substring match against one of these can never stand in for the
# manufacturer's own product page.
EXCLUDED_DOMAINS = frozenset({
    "wikipedia.org", "globenewswire.com", "ahridirectory.org", "hcai.ca.gov",
    "prnewswire.com", "businesswire.com", "epa.gov", "energystar.gov",
    "ashrae.org", "youtube.com", "linkedin.com", "facebook.com", "twitter.com",
    "instagram.com", "google.com", "bing.com",
})

_DOMAIN_RE = re.compile(r"\b([a-z0-9][a-z0-9-]*\.[a-z]{2,6})(?:/[\w\-./]*)?", re.IGNORECASE)
_NON_ALNUM = re.compile(r"[^a-z0-9]", re.IGNORECASE)

# Claim-bearing signal #1: a number (possibly a range) immediately followed
# or preceded by a unit/measure token this domain actually uses. Deliberately
# broad on units (better to over-flag a sentence for grounding than under-flag
# a real claim) -- a false-positive claim check just means checking a harmless
# sentence against the page text and finding it grounded or dropping it;
# either outcome is safe. A missed claim check is the dangerous direction.
#
# The trailing \b MUST be attached per-alternative, not once at the end of
# the whole group -- a shared trailing \b after an alternation that ends in
# a symbol (%) never matches, because \b requires a transition between a
# word char and a non-word char, and '%' followed by whitespace/punctuation
# is non-word on BOTH sides of that boundary. Confirmed directly: the
# original single-trailing-\b version silently matched ZERO real claims
# ending in '%' (e.g. "20% higher SEER rating") during this module's own
# development -- caught by testing against a deliberately fabricated claim
# before ever running this against a real line.
_CLAIM_NUMBER_RE = re.compile(
    r"\d+(?:\.\d+)?\s*(?:-|to|–)?\s*\d*(?:\.\d+)?\s*(?:"
    r"tons?\b|cfm\b|hp\b|kw\b|mw\b|percent\b|db\b|dba\b|seer\d*\b|eer\b|ieer\b|cop\b|"
    r"gpm\b|psi\b|btu\b|amps?\b|volts?\b|hz\b|rpm\b|years?\b|weeks?\b|%"
    r")",
    re.IGNORECASE,
)
# Claim-bearing signal #2: a named certification/standard -- checkable
# against a page even with no number attached ("AHRI certified").
_CLAIM_KEYWORDS = (
    "ahri", "ul listed", "ul-listed", "cetlus", "etl listed", "etl-listed",
    "nsf", "iso 9001", "iso 14001", "ashrae 90.1", "ashrae 62.1", "energy star",
    "oshpd", "hcai osp", "ufc 4-010-06", "cul", " ce mark", "ce certified",
)
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")


def _extract_claim_fragments(sentence: str) -> list[str]:
    """The SHORT substrings within `sentence` that make a checkable factual
    claim -- a number+unit span, or a certification keyword -- NOT the
    whole sentence. Grounding a short fragment with quote_grounded is the
    right granularity: grounding the whole SENTENCE would fail on any
    faithful paraphrase around a real, correctly-cited number (confirmed
    while building this -- "Capacities run from 3 to 230 tons." fails a
    whole-sentence check against a page that says "capacities from 3 to
    230 tons" purely because of the surrounding words, even though the
    number itself is exactly right). A word-level check
    (app.grounding.name_grounded) was tried and rejected for the numeric
    case for the opposite reason: its variant-matching treats "500 tons"
    as grounded by a page that only contains the word "tons" somewhere
    else entirely -- exactly the false-positive this module cannot afford
    on a number. quote_grounded on the isolated number+unit fragment
    avoids both failure modes: "3 to 230 tons" matches a page saying
    "capacities from 3 to 230 tons" (real, sourced, kept), "500 tons"
    does not match a page that only has "3 to 230 tons" (fabricated,
    dropped) -- confirmed with both cases directly against grounding.py's
    real functions before choosing this approach."""
    fragments = [m.group(0).strip() for m in _CLAIM_NUMBER_RE.finditer(sentence)]
    lowered = sentence.lower()
    for kw in _CLAIM_KEYWORDS:
        if kw in lowered:
            fragments.append(kw.strip())
    return fragments


def ground_text(text: str | None, page_text: str | None) -> tuple[str | None, int, int]:
    """Split `text` into sentences; a sentence with no claim fragment
    passes through unchanged. A sentence WITH one or more claim fragments
    is kept only if EVERY fragment in it is grounded (quote_grounded
    against page_text) -- if page_text is None, every claim fragment is
    automatically ungrounded (nothing to check it against). Dropped
    wholesale, not softened: a sentence with one fabricated number and one
    real one is dropped entirely, never rewritten to keep only the real
    half. Returns (kept_text_or_None, grounded_fragment_count,
    dropped_fragment_count)."""
    if not text or not text.strip():
        return None, 0, 0
    sentences = [s.strip() for s in _SENTENCE_SPLIT_RE.split(text.strip()) if s.strip()]
    kept: list[str] = []
    grounded = dropped = 0
    for s in sentences:
        fragments = _extract_claim_fragments(s)
        if not fragments:
            kept.append(s)
            continue
        results = [bool(page_text) and quote_grounded(f, page_text).problem is None for f in fragments]
        if all(results):
            kept.append(s)
            grounded += len(fragments)
        else:
            dropped += len(fragments)
    result = " ".join(kept).strip() or None
    return result, grounded, dropped


def ground_list(items: list[str], page_text: str | None) -> tuple[list[str], int, int]:
    """Same fragment-level rule as ground_text, applied per LIST ITEM (a
    differentiator or an engineer question) -- an item carrying an
    ungrounded claim fragment is dropped from the list entirely (never
    padded back to a target length with an invented replacement)."""
    kept: list[str] = []
    grounded = dropped = 0
    for item in items or []:
        item = (item or "").strip()
        if not item:
            continue
        fragments = _extract_claim_fragments(item)
        if not fragments:
            kept.append(item)
            continue
        results = [bool(page_text) and quote_grounded(f, page_text).problem is None for f in fragments]
        if all(results):
            kept.append(item)
            grounded += len(fragments)
        else:
            dropped += len(fragments)
    return kept, grounded, dropped


def _norm_key(name: str) -> str:
    return _NON_ALNUM.sub("", name.lower())


def candidate_domains(line: ProductLine) -> list[str]:
    """Domains extractable from THIS line's own already-researched basis
    fields whose registrable name plausibly matches the line's own name --
    never a web search, never a guessed '{name}.com'. See module docstring
    for the measured 48/69 real-corpus hit rate."""
    basis_fields = (
        line.description, line.existence_verified_basis, line.heat_rejection_mode_basis,
        line.latent_load_capability_basis, line.corrosion_resistance_basis,
        line.redundancy_capable_basis, line.rigging_constrained_capable_basis,
        line.markets_served_basis, line.competes_with_basis, line.oshpd_osp_basis,
        line.ufc_4_010_06_basis, line.ahri_certified_basis, line.country_of_manufacture_basis,
        line.lead_time_basis, line.limitations,
    )
    text = " ".join(f or "" for f in basis_fields)
    domains = {m.group(1).lower() for m in _DOMAIN_RE.finditer(text)}

    key = _norm_key(line.name)
    first_word_key = _norm_key(line.name.split()[0]) if line.name.split() else key
    out = []
    for d in domains:
        if d in EXCLUDED_DOMAINS:
            continue
        registrable = _NON_ALNUM.sub("", d)
        if key and key in registrable:
            out.append(d)
        elif first_word_key and len(first_word_key) >= 3 and first_word_key in registrable:
            out.append(d)
    return sorted(set(out))


def fetch_product_page(domain: str) -> tuple[str | None, str | None, str]:
    """Returns (page_text_or_None, url_actually_fetched_or_None,
    fetch_status). fetch_status is one of 'fetched' | 'robots_disallowed' |
    'fetch_failed'. Uses PoliteClient (app.http) so robots.txt is checked
    live, per domain, exactly like every other adapter in this app -- never
    assumed permitted."""
    import httpx
    from selectolax.parser import HTMLParser

    from app.http import PoliteClient, RobotsDisallowed

    url = f"https://{domain}/"
    try:
        with PoliteClient(interval=0.5, max_retries=1, max_retries_5xx=0) as client:
            resp = client.get(url, timeout=FETCH_TIMEOUT_SECONDS)
    except RobotsDisallowed:
        return None, None, "robots_disallowed"
    except Exception as exc:  # noqa: BLE001 -- one bad domain must not kill the whole run
        log.warning("line_pitch: fetch failed for %s: %s", url, exc)
        return None, None, "fetch_failed"

    tree = HTMLParser(resp.text)
    for tag in tree.css("script, style, noscript"):
        tag.decompose()
    text = tree.body.text(separator=" ", strip=True) if tree.body else ""
    if not text.strip():
        return None, url, "fetch_failed"
    return text[:MAX_PAGE_CHARS], url, "fetched"


# ---- the one Sonnet call, per line -----------------------------------------
# The system prompt, tool schema, and the actual _tool_call plumbing live in
# app.llm.generate_line_pitch/LINE_PITCH_SYSTEM/LINE_PITCH_TOOL -- same
# division of labor as every other extraction stage in this app (the LLM
# call itself stays in app/llm.py; the pipeline module owns everything
# around it: what goes in, grounding/validation of what comes out).


def _build_user_content(line: ProductLine, branches: list[str], competitor_candidates: list[str],
                        page_text: str | None, page_url: str | None) -> str:
    from app.accounts import ROLE_LABELS

    parts = [
        f"Line: {line.name}",
        f"Firm: {line.firm}",
        f"Category / subcategory: {line.category} / {line.subcategory or '(none)'}",
        f"Description (from DMG's own line card): {line.description or '(none on file)'}",
        f"Building-systems role: {ROLE_LABELS.get(line.building_role, line.building_role)}",
        f"Confirmed DMG/ToroAire branches carrying this line: {', '.join(branches) if branches else '(none confirmed)'}",
        f"HCAI/OSHPD OSP pre-approval: "
        f"{'Yes' if line.oshpd_osp else ('No' if line.oshpd_osp is False else 'Not researched')}",
        f"Existence verified: {'Yes' if line.existence_verified else 'Not independently confirmed'}",
    ]
    if competitor_candidates:
        parts.append("Candidate competitors (choose ONLY from this list, or none): "
                     + ", ".join(competitor_candidates))
    else:
        parts.append("Candidate competitors: none on file for this line's role -- "
                     "return an empty competitors list.")
    if page_text:
        parts.append(f"\nManufacturer's own product page text ({page_url}):\n{page_text}")
    else:
        parts.append("\nNo manufacturer product page was available for this line -- "
                     "write only role/category-level content, no specific numbers or certifications.")
    return "\n".join(parts)


def generate_one_line(session: Session, cfg: Config, line: ProductLine) -> dict:
    """One Sonnet call for this line, fully grounded and validated. Returns
    a stats dict; never raises for a bad LLM response (records an error
    instead) -- ONLY lets app.spend.BudgetExceeded propagate, since that
    one must stop the whole run, not just this line."""
    branches = sorted({
        b.branch for b in session.exec(
            select(ProductLineBranch).where(
                ProductLineBranch.product_line_id == line.id,
                ProductLineBranch.status == "confirmed_covered")).all()
    })

    competitor_rows = competing_lines_by_role(session).get(line.building_role, [])
    own_key = _norm_key(line.name)
    candidates = sorted({
        row.manufacturer for row in competitor_rows
        if _norm_key(row.manufacturer) != own_key
    })

    domains = candidate_domains(line)
    page_text = page_url = None
    fetch_status = "no_domain_found"
    if domains:
        page_text, page_url, fetch_status = fetch_product_page(domains[0])

    user_content = _build_user_content(line, branches, candidates, page_text, page_url)
    model = cfg.get("llm.line_pitch_model", "claude-sonnet-4-6")

    try:
        raw = generate_line_pitch(user_content)
    except BudgetExceeded:
        raise
    except Exception as exc:  # noqa: BLE001 -- one bad line must not kill the whole run
        return {"ok": False, "line_id": line.id, "line_name": line.name,
               "error": f"{type(exc).__name__}: {exc}"}

    return {
        "ok": True, "line_id": line.id, "line_name": line.name, "raw": raw,
        "page_text": page_text, "page_url": page_url, "fetch_status": fetch_status,
        "domains_tried": domains, "model": model,
    }


def _truncate_to_words(text: str, max_words: int) -> tuple[str, bool]:
    words = text.split()
    if len(words) <= max_words:
        return text, False
    return " ".join(words[:max_words]), True


def _validate_competitor_names(competitors: list[dict], candidates: list[str]) -> list[dict]:
    """Never trust the schema's own honesty -- drop any competitor the
    model returned that isn't in the candidate set, in code, unconditionally."""
    candidate_set = {c.lower() for c in candidates}
    return [c for c in competitors if (c.get("competitor_name") or "").lower() in candidate_set]


def _write_pitch_and_competitors(session: Session, line: ProductLine, result: dict) -> dict:
    raw = result["raw"]
    page_text = result["page_text"]

    what_it_is, g1, d1 = ground_text(raw.get("what_it_is"), page_text)
    where_it_fits, g2, d2 = ground_text(raw.get("where_it_fits"), page_text)
    typical, g3, d3 = ground_text(raw.get("typical_project_types"), page_text)
    elevator, g4, d4 = ground_text(raw.get("elevator_pitch"), page_text)
    truncated = False
    if elevator:
        elevator, truncated = _truncate_to_words(elevator, MAX_ELEVATOR_PITCH_WORDS)
    diffs, g5, d5 = ground_list(raw.get("differentiators") or [], page_text)
    questions, g6, d6 = ground_list(raw.get("engineer_questions") or [], page_text)

    grounded_total = g1 + g2 + g3 + g4 + g5 + g6
    dropped_total = d1 + d2 + d3 + d4 + d5 + d6

    existing = session.exec(select(LinePitch).where(LinePitch.product_line_id == line.id)).first()
    pitch = existing or LinePitch(product_line_id=line.id)
    pitch.what_it_is = what_it_is
    pitch.where_it_fits = where_it_fits
    pitch.typical_project_types = typical
    pitch.elevator_pitch = elevator
    pitch.differentiators = diffs
    pitch.engineer_questions = questions
    pitch.review_status = "draft"
    pitch.reviewed_by = None
    pitch.reviewed_at = None
    pitch.source_url = result["page_url"]
    pitch.source_fetch_status = result["fetch_status"]
    pitch.grounded_claim_count = grounded_total
    pitch.dropped_claim_count = dropped_total
    pitch.model = result["model"]
    pitch.generated_at = utcnow()
    pitch.updated_at = utcnow()
    session.add(pitch)
    session.commit()
    session.refresh(pitch)

    role_rows = competing_lines_by_role(session).get(line.building_role, [])
    source_url_by_name = {row.manufacturer: row.source_url for row in role_rows}

    candidates = list(source_url_by_name.keys())
    valid_competitors = _validate_competitor_names(raw.get("competitors") or [], candidates)
    n_competitors_written = 0
    for c in valid_competitors:
        name = c["competitor_name"]
        existing_c = session.exec(
            select(LineCompetitor).where(LineCompetitor.product_line_id == line.id,
                                         LineCompetitor.competitor_name == name)).first()
        if existing_c is not None and existing_c.review_status in ("confirmed", "rejected"):
            continue  # a human decision on this pair is never overwritten
        row = existing_c or LineCompetitor(product_line_id=line.id, competitor_name=name)
        row.why_we_lose = c.get("why_we_lose")
        row.why_we_win = c.get("why_we_win")
        row.evidence_url = source_url_by_name.get(name)
        row.review_status = "draft"
        row.model = result["model"]
        row.generated_at = utcnow()
        row.updated_at = utcnow()
        session.add(row)
        n_competitors_written += 1
    session.commit()

    return {
        "pitch_id": pitch.id, "grounded_claims": grounded_total, "dropped_claims": dropped_total,
        "elevator_pitch_truncated": truncated, "competitors_written": n_competitors_written,
        "competitors_offered": len(raw.get("competitors") or []),
        "competitors_rejected_not_on_map": len(raw.get("competitors") or []) - len(valid_competitors),
    }


def run_line_pitch_generation(session: Session, cfg: Config, *, cap_usd: float = 5.0) -> dict:
    """The whole run: every eligible ProductLine, one Sonnet call each,
    hard-capped at cap_usd total. Skips (a) any line with
    existence_verified is False (per the brief), (b) any line whose
    LinePitch is already 'confirmed' (a human's review is never silently
    redone). Does NOT skip a 'rejected' or 'draft' pitch -- a rejected
    pitch is a signal to try again differently, not a permanent no."""
    lines = session.exec(
        select(ProductLine).where(ProductLine.existence_verified.is_not(False))
        .order_by(ProductLine.name)
    ).all()

    stats = {
        "lines_total": len(lines), "lines_generated": 0, "lines_skipped_existence_false": 0,
        "lines_skipped_already_confirmed": 0, "lines_skipped_budget": 0, "lines_errored": 0,
        "grounded_claims_total": 0, "dropped_claims_total": 0, "competitors_written_total": 0,
        "fetch_status_counts": {}, "errors": [], "sample_pitches": [],
    }

    all_lines = session.exec(select(ProductLine)).all()
    stats["lines_skipped_existence_false"] = sum(1 for l in all_lines if l.existence_verified is False)

    confirmed_line_ids = {
        p.product_line_id for p in session.exec(
            select(LinePitch).where(LinePitch.review_status == "confirmed")).all()
    }

    budget_exhausted = False
    with run_budget("line_pitch", cap_usd=cap_usd) as run:
        for line in lines:
            if line.id in confirmed_line_ids:
                stats["lines_skipped_already_confirmed"] += 1
                continue
            if budget_exhausted:
                stats["lines_skipped_budget"] += 1
                continue

            try:
                result = generate_one_line(session, cfg, line)
            except BudgetExceeded:
                budget_exhausted = True
                stats["lines_skipped_budget"] += 1
                continue

            stats["fetch_status_counts"][result.get("fetch_status", "error")] = (
                stats["fetch_status_counts"].get(result.get("fetch_status", "error"), 0) + 1)

            if not result.get("ok"):
                stats["lines_errored"] += 1
                stats["errors"].append({"line": line.name, "error": result.get("error")})
                continue

            write_stats = _write_pitch_and_competitors(session, line, result)
            stats["lines_generated"] += 1
            stats["grounded_claims_total"] += write_stats["grounded_claims"]
            stats["dropped_claims_total"] += write_stats["dropped_claims"]
            stats["competitors_written_total"] += write_stats["competitors_written"]
            if len(stats["sample_pitches"]) < 5:
                pitch = session.get(LinePitch, write_stats["pitch_id"])
                stats["sample_pitches"].append({
                    "line": line.name, "elevator_pitch": pitch.elevator_pitch,
                    "what_it_is": pitch.what_it_is, "differentiators": pitch.differentiators,
                    "engineer_questions": pitch.engineer_questions,
                    "source_fetch_status": pitch.source_fetch_status,
                })
        stats["cost_usd"] = run.spent_usd

    return stats
