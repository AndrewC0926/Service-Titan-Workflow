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
  3. The manufacturer's own product page(s), fetched two ways in order:
     (a) candidate_domains -- a domain extracted from this line's OWN
     already-researched basis fields (country_of_manufacture_basis,
     ahri_certified_basis, etc.) or a previously-discovered
     ProductLine.official_domain, never invented, never a guessed
     "{name}.com". Measured against the real production line card,
     2026-09-03: 48 of 69 eligible lines had a plausible domain this way.
     (b) discover_domain_via_web_search -- added 2026-09-04, ONE
     web_search-enabled Sonnet call (app.llm.find_line_domain), used ONLY
     as a fallback when (a) produced nothing fetchable, and ONLY once per
     line ever (never retried once ProductLine.official_domain is set,
     even if a later fetch of it fails). The domain it returns is trusted
     only after fetch_product_pages actually retrieves the home page AND
     the line's own name is found on it (_page_contains_line_name) -- a
     web_search citation alone is never enough. Whichever way a domain
     was found, fetch_product_pages fetches TWO pages -- the home page and
     the most product-like page linked from it (matching "product",
     "catalog", or "solutions" in its link text or href) -- and grounds
     against both concatenated, since a manufacturer's real capability
     claims routinely live on a dedicated catalog page, not the home
     page's marketing copy. A line with no fetchable domain by either
     method gets NO product-page fetch and therefore NO factual
     (numeric/certification/capability) claims at all -- only the
     role/category-level pitch content that doesn't need grounding.

GROUNDING (rewritten 2026-09-03 -- round 2 of this feature). Every
sentence in a generated field is scanned for claim FRAGMENTS, not just
numeric ranges: _extract_claim_fragments pulls out unit-bearing numbers
(tons, CFM, %, SEER, dB, kW, ...), qualified quantities including
spelled-out forms ("over 200", "dozens of", "hundreds of" --
_CLAIM_QUALIFIED_QTY_RE/_CLAIM_WORD_QTY_RE), named
certifications/standards (AHRI, UL, ETL, OSHPD, NSF, ISO, ASHRAE, ENERGY
STAR, cETLus), and short capability/integration windows following a
trigger phrase ("compatible with", "integrates with", "certified for",
...). Each fragment is checked against the fetched page text with
app.grounding.quote_grounded (deliberately chosen over
app.grounding.name_grounded for this: name_grounded's variant-generation
is built for personal/firm names and produces false positives on
arbitrary claims -- name_grounded("500 tons", a page that never states
"500") returns True purely because its last-token variant "tons"
matches). A sentence is kept only if ALL of its extracted fragments
ground; otherwise the whole sentence is DROPPED, never softened, never
rewritten to hedge -- per the brief's explicit "or it is dropped, not
softened." This closes the original gap that let a line ship a
capability claim with zero fragments even attempted (the Airzone "over
200 compatible AC brands" case, which had no matching pattern before this
rewrite -- see test_line_pitch.py's regression test for it). A line with
no fetched page never reaches this grounding pass at all -- see
pitch_scope below. grounded_claim_count/dropped_claim_count are recorded
per LinePitch row, not just in the aggregate run stats, so coverage is
visible on the pitch itself.

Non-claim sentences (role framing, "why it fits") pass through untouched
apart from register sanitization -- the grounding pass only ever removes
sentences it can positively identify as making a checkable factual
assertion; it does not touch everything else. The single differentiator
field is the one exception: because its entire purpose is to BE a
specific checkable claim, ground_required_claim treats an item with no
extractable claim fragment at all as ungrounded (dropped), unlike the
general list/sentence path which passes non-claim content through.

REGISTER (added 2026-09-03). The reader is a mechanical engineer or a
contractor's PM, not a homeowner. Every field is sanitized
(_sanitize_field: wrapping quote characters stripped, "!" replaced with
".") and checked against BANNED_PHRASES ("you name it", "that's the
pitch", "one rep relationship", "cleans things up") -- a sentence/item
containing a banned phrase is dropped under the same discipline as an
ungrounded claim, not just cosmetically cleaned. differentiators and
engineer_questions are capped to exactly ONE each (down from "up to 3"),
per the four-part structure: what it is (one sentence), where it fits
(one sentence), one grounded differentiator, one question to ask.

PITCH_SCOPE (added 2026-09-03). A line whose page fetch did not succeed
("fetch_failed", "no_url_found", "robots_disallowed", etc.) never reaches
the LLM at all -- generate_one_line short-circuits to
line_row_only_pitch(), a deterministic template built only from the
line's own building_role and confirmed branches, with NO capability or
product-category claims and NO competitor rows (there is no capability
fact to position a competitor comparison against). This is
pitch_scope='line_row_only', shown in the UI distinctly from
pitch_scope='full' (a page was fetched and an LLM wrote a grounded
pitch). This replaces round 1's behavior, where a fetch_failed line still
got a full LLM-written pitch that then had every claim dropped by
grounding, leaving a vague, unlabeled fragment (the Aldes row, this
feature's own first production run). A 'full' line whose every field
still ends up empty after grounding is reported as "insufficient
source" rather than shown as a bare fragment.

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
    r"gpm\b|psi\b|btu\b|amps?\b|volts?\b|hz\b|rpm\b|years?\b|weeks?\b|"
    r"models?\b|brands?\b|zones?\b|units?\b|plants?\b|employees?\b|countries\b|projects?\b|%"
    r")",
    re.IGNORECASE,
)
# Claim-bearing signal #1b: a QUALIFIED quantity ("over 200", "more than
# 42,000", "up to 10", "nearly 100") -- the specific shape this module's
# first production run MISSED entirely. Confirmed real: Airzone's own
# elevator pitch said "over 200 compatible AC brands", which tripped
# NEITHER the number+unit regex above (no unit word matched -- "brands"
# wasn't in the unit list yet) NOR the certification keyword list, so it
# went out with ZERO claims attempted -- a genuinely true, on-page fact
# (airzonecontrol.com's own text: "over 200 brands of AC units can be
# managed", "+200 Compatible brands", "+42K Compatible models") that
# simply never got checked. This pattern plus the widened unit list above
# both exist specifically to catch that shape going forward.
_CLAIM_QUALIFIED_QTY_RE = re.compile(
    r"(?:over|more than|up to|at least|nearly|roughly|approximately|fewer than|less than)\s+"
    r"[\d,]+(?:\.\d+)?\s*[kKmM]?\+?(?:\s+\w+){0,3}",
    re.IGNORECASE,
)
# Claim-bearing signal #1c: a spelled-out quantity ("dozens of models",
# "hundreds of installations") -- no digit at all, but still a checkable
# quantity claim per the brief's explicit "any quantity including
# spelled-out forms" instruction.
_CLAIM_WORD_QTY_RE = re.compile(
    r"\b(?:dozens?|hundreds?|thousands?)\b(?:\s+of\s+\w+){0,2}", re.IGNORECASE)
# Claim-bearing signal #2: a named certification/standard -- checkable
# against a page even with no number attached ("AHRI certified").
_CLAIM_KEYWORDS = (
    "ahri", "ul listed", "ul-listed", "cetlus", "etl listed", "etl-listed",
    "nsf", "iso 9001", "iso 14001", "ashrae 90.1", "ashrae 62.1", "energy star",
    "oshpd", "hcai osp", "ufc 4-010-06", "cul", " ce mark", "ce certified", "leed",
)
# Claim-bearing signal #3: a capability/integration/product-category TRIGGER
# -- per the brief's explicit "product categories, capabilities... and
# integrations" scope, not numbers/certifications alone. Each trigger match
# captures itself plus a short following window (see
# _CAPABILITY_WINDOW_WORDS) as ONE claim phrase, ground-checked the same
# fragment-level way as a numeric claim. Deliberately does NOT attempt to
# grounds-check a bare descriptive noun phrase with no trigger word at all
# (e.g. "packaged rooftop units" sitting on its own with no
# "compatible with"/"certified for"/etc. nearby) -- see this module's own
# docstring for why a whole-phrase verbatim check would reject correctly-
# paraphrased, TRUE content (the same failure mode already found and fixed
# for whole-sentence numeric grounding), which is the opposite of the
# intended effect. This is a disclosed scope limit, not an oversight.
_CAPABILITY_TRIGGERS = (
    "compatible with", "compatible across", "integrates with", "integrated with",
    "integration with", "works with", "connects to", "connects with",
    "built-in", "built in", "factory-installed", "factory installed",
    "compliant with", "certified for", "certified to", "rated for",
    "designed for", "engineered for", "approved for", "proprietary", "patented",
)
_CAPABILITY_WINDOW_WORDS = 8
_CAPABILITY_STOP_RE = re.compile(r"[.,;!?]")
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")

# ---- register: banned filler, wrapping quotes, exclamation marks ----------
# Per the 2026-09-03 rewrite: the reader is a mechanical engineer or a
# contractor's PM, not a homeowner. A sentence carrying any of these reads
# as ad copy, not a fact a rep can repeat to an engineer -- dropped
# wholesale like an ungrounded claim, not rewritten to soften it, same
# discipline throughout this module. Exclamation marks and wrapping quote
# characters are cosmetic, not content problems, so those are FIXED in
# place (see _sanitize_field) rather than causing a drop.
BANNED_PHRASES = ("you name it", "that's the pitch", "one rep relationship", "cleans things up")

_WRAPPING_QUOTE_PAIRS = (('"', '"'), ("'", "'"), ("“", "”"), ("‘", "’"))


def _has_banned_phrase(text: str) -> bool:
    lowered = text.lower()
    return any(phrase in lowered for phrase in BANNED_PHRASES)


def _sanitize_field(text: str | None) -> str | None:
    """Cosmetic-only fixes, applied before grounding: strip a single
    matching pair of wrapping quote characters, and replace exclamation
    marks with a period -- neither changes the factual content of the
    text, so neither counts as a claim being dropped."""
    if not text:
        return text
    stripped = text.strip()
    for lq, rq in _WRAPPING_QUOTE_PAIRS:
        if len(stripped) > 1 and stripped.startswith(lq) and stripped.endswith(rq):
            stripped = stripped[1:-1].strip()
            break
    stripped = stripped.replace("!", ".")
    return stripped


def _capability_window(sentence: str, trigger_start: int) -> str:
    """From `trigger_start` to the next clause-ending punctuation or
    _CAPABILITY_WINDOW_WORDS words, whichever comes first."""
    rest = sentence[trigger_start:]
    stop = _CAPABILITY_STOP_RE.search(rest)
    if stop:
        rest = rest[:stop.start()]
    words = rest.split()
    return " ".join(words[:_CAPABILITY_WINDOW_WORDS]).strip()


def _extract_claim_fragments(sentence: str) -> list[str]:
    """The SHORT substrings within `sentence` that make a checkable factual
    claim -- a quantity (numeric or spelled-out), a certification keyword,
    or a capability/integration trigger phrase -- NOT the whole sentence.
    Grounding a short fragment with quote_grounded is the right
    granularity: grounding the whole SENTENCE would fail on any faithful
    paraphrase around a real, correctly-cited number (confirmed while
    building this -- "Capacities run from 3 to 230 tons." fails a
    whole-sentence check against a page that says "capacities from 3 to
    230 tons" purely because of the surrounding words, even though the
    number itself is exactly right). A word-level check
    (app.grounding.name_grounded) was tried and rejected for the numeric
    case for the opposite reason: its variant-matching treats "500 tons"
    as grounded by a page that only contains the word "tons" somewhere
    else entirely -- exactly the false-positive this module cannot afford
    on a number. quote_grounded on the isolated fragment avoids both
    failure modes and is used uniformly here for every claim type,
    including capability/integration phrases, for the same reason --
    app.grounding.name_grounded's variant-matching risk (a fuzzy match
    that can key off a single common trailing word) generalizes to any
    multi-word claim, not just numeric ones.

    A quantity like "up to 500 tons" matches BOTH _CLAIM_NUMBER_RE ("500
    tons") and _CLAIM_QUALIFIED_QTY_RE ("up to 500") since "up to" is one
    of that regex's own qualifiers -- without de-duplication this double-
    counts the SAME claim as two fragments (caught by this module's own
    test suite: dropped_claim_count came back doubled for any qualified
    numeric phrase already covered by the number+unit regex). Fixed by
    tracking character spans and skipping a qualified/word-quantity match
    that overlaps a number-regex match already collected -- the two
    regexes are independent for the cases that need both (a bare
    "brands"/"models" count with no adjacent unit word, or a spelled-out
    "hundreds of" with no digit at all), overlap only for the case that's
    genuinely the same fragment counted twice."""
    spans: list[tuple[int, int]] = []
    fragments: list[str] = []

    def _add(m: re.Match) -> None:
        spans.append((m.start(), m.end()))
        fragments.append(m.group(0).strip())

    def _overlaps(m: re.Match) -> bool:
        return any(m.start() < e and s < m.end() for s, e in spans)

    for m in _CLAIM_NUMBER_RE.finditer(sentence):
        _add(m)
    for m in _CLAIM_QUALIFIED_QTY_RE.finditer(sentence):
        if not _overlaps(m):
            _add(m)
    for m in _CLAIM_WORD_QTY_RE.finditer(sentence):
        if not _overlaps(m):
            _add(m)
    lowered = sentence.lower()
    for kw in _CLAIM_KEYWORDS:
        if kw in lowered:
            fragments.append(kw.strip())
    for trigger in _CAPABILITY_TRIGGERS:
        idx = lowered.find(trigger)
        if idx != -1:
            window = _capability_window(sentence, idx)
            if window:
                fragments.append(window)
    return fragments


def ground_text(text: str | None, page_text: str | None) -> tuple[str | None, int, int]:
    """Sanitize (strip wrapping quotes, fix exclamation marks), then split
    into sentences. A sentence carrying a banned filler phrase is dropped
    outright (a register violation, not a grounding question). A sentence
    with no claim fragment otherwise passes through unchanged. A sentence
    WITH one or more claim fragments is kept only if EVERY fragment in it
    is grounded (quote_grounded against page_text) -- if page_text is
    None, every claim fragment is automatically ungrounded (nothing to
    check it against). Dropped wholesale, not softened: a sentence with
    one fabricated claim and one real one is dropped entirely, never
    rewritten to keep only the real half. Returns (kept_text_or_None,
    grounded_fragment_count, dropped_fragment_count)."""
    text = _sanitize_field(text)
    if not text or not text.strip():
        return None, 0, 0
    sentences = [s.strip() for s in _SENTENCE_SPLIT_RE.split(text.strip()) if s.strip()]
    kept: list[str] = []
    grounded = dropped = 0
    for s in sentences:
        if _has_banned_phrase(s):
            continue
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
    """Same fragment-level rule as ground_text, applied per LIST ITEM (an
    engineer question, which carries no factual claim of its own but is
    still sanitized and register-checked) -- an item carrying an
    ungrounded claim fragment or a banned phrase is dropped from the list
    entirely (never padded back to a target length with an invented
    replacement)."""
    kept: list[str] = []
    grounded = dropped = 0
    for item in items or []:
        item = _sanitize_field(item)
        if not item:
            continue
        if _has_banned_phrase(item):
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


def ground_required_claim(item: str | None, page_text: str | None) -> tuple[str | None, int, int]:
    """For a slot that MUST itself be a grounded claim -- the single
    differentiator, per the 2026-09-03 register rewrite ('one grounded
    differentiator'). Unlike ground_list's general items (which pass
    through untouched if they carry no claim at all -- fine for an
    engineer question, which isn't a factual assertion), an item with NO
    extractable claim fragment is treated as ungrounded HERE: the whole
    point of this one slot is that it be a specific, checkable fact, not a
    vague statement of quality. Returns (kept_or_None, grounded_count,
    dropped_count)."""
    item = _sanitize_field(item)
    if not item:
        return None, 0, 0
    if _has_banned_phrase(item):
        return None, 0, 1
    fragments = _extract_claim_fragments(item)
    if not fragments:
        return None, 0, 1  # required to BE a claim -- nothing to check counts as ungrounded
    results = [bool(page_text) and quote_grounded(f, page_text).problem is None for f in fragments]
    if all(results):
        return item, len(fragments), 0
    return None, 0, len(fragments)


def _norm_key(name: str) -> str:
    return _NON_ALNUM.sub("", name.lower())


def candidate_domains(line: ProductLine) -> list[str]:
    """Domains extractable from THIS line's own already-researched basis
    fields whose registrable name plausibly matches the line's own name --
    never a web search, never a guessed '{name}.com'. See module docstring
    for the measured 48/69 real-corpus hit rate.

    line.official_domain (set at most once per line, by
    discover_domain_via_web_search, after the fetched page was checked
    for the line's own name -- see that function and ProductLine's own
    docstring) is tried FIRST when present, ahead of anything extracted
    from basis text here -- it is the one candidate that has already been
    validated against a real fetch, not merely a citation."""
    if line.official_domain:
        return [line.official_domain]
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


# ---- two-page fetch (added 2026-09-04): a manufacturer's own capability
# claims routinely live on a dedicated products/catalog page, not the home
# page's marketing copy, so grounding against the home page alone under-
# counts what's actually checkable. Same robots/retry discipline as
# fetch_product_page above (which stays in place, unchanged, for anything
# that only needs a single page and for the existing test suite's mocks).

_PRODUCT_LINK_KEYWORDS = ("product", "catalog", "solutions")


def _extract_product_like_link(tree, base_url: str) -> str | None:
    """The first link on the home page whose own text or href matches a
    product-page keyword, checked in _PRODUCT_LINK_KEYWORDS order so a
    'product' link is preferred over a merely 'solutions' one when a page
    has both. Resolved against base_url; a link to a different host is
    never followed (a home page's footer routinely links to a parent
    corporation, a certification body, or a social profile -- none of
    those are this line's own deeper content)."""
    from urllib.parse import urljoin, urlparse

    base_host = urlparse(base_url).netloc
    best: tuple[int, str] | None = None
    for a in tree.css("a"):
        href = a.attributes.get("href")
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        text = (a.text() or "").strip().lower()
        href_l = href.lower()
        for rank, kw in enumerate(_PRODUCT_LINK_KEYWORDS):
            if kw in text or kw in href_l:
                resolved = urljoin(base_url, href)
                if urlparse(resolved).netloc != base_host:
                    break
                if best is None or rank < best[0]:
                    best = (rank, resolved)
                break
    return best[1] if best else None


def _parse_page_text(html: str) -> str:
    from selectolax.parser import HTMLParser

    tree = HTMLParser(html)
    for tag in tree.css("script, style, noscript"):
        tag.decompose()
    return tree.body.text(separator=" ", strip=True) if tree.body else ""


def fetch_product_pages(domain: str) -> tuple[str | None, str | None, str, list[str]]:
    """Home page plus, if one is linked, the most product-like page
    (products/catalog/solutions) -- grounded against both, concatenated
    into one page_text (every caller downstream, ground_text/ground_list/
    ground_required_claim, already just takes a single page_text string,
    so no other code needs to know there were two fetches).

    Returns (combined_page_text_or_None, home_url_or_None, fetch_status,
    urls_fetched). fetch_status describes the HOME page fetch only --
    'fetched' | 'robots_disallowed' | 'fetch_failed', same three values as
    fetch_product_page. A second-page fetch failure (robots, network, no
    matching link found at all) just means grounding runs against one
    page instead of two; it is never a reason to fail the whole line, so
    it is swallowed here and logged, not raised or returned as a status.

    Both requests share ONE PoliteClient, so robots.txt is fetched once
    per host but re-checked (app.http.PoliteClient.request calls
    rp.can_fetch on the FULL url every time) before the second page --
    a host that blocks /products/ while allowing / is still respected."""
    from selectolax.parser import HTMLParser

    from app.http import PoliteClient, RobotsDisallowed

    home_url = f"https://{domain}/"
    try:
        with PoliteClient(interval=0.5, max_retries=1, max_retries_5xx=0) as client:
            resp = client.get(home_url, timeout=FETCH_TIMEOUT_SECONDS)
            tree = HTMLParser(resp.text)
            for tag in tree.css("script, style, noscript"):
                tag.decompose()
            home_text = tree.body.text(separator=" ", strip=True) if tree.body else ""
            if not home_text.strip():
                return None, home_url, "fetch_failed", []

            urls_fetched = [home_url]
            texts = [home_text[:MAX_PAGE_CHARS]]

            product_url = _extract_product_like_link(tree, home_url)
            if product_url and product_url != home_url:
                try:
                    presp = client.get(product_url, timeout=FETCH_TIMEOUT_SECONDS)
                    product_text = _parse_page_text(presp.text)
                    if product_text.strip():
                        texts.append(product_text[:MAX_PAGE_CHARS])
                        urls_fetched.append(product_url)
                except RobotsDisallowed:
                    log.info("line_pitch: product page disallowed by robots.txt: %s", product_url)
                except Exception as exc:  # noqa: BLE001 -- a second-page failure is never fatal
                    log.warning("line_pitch: product-page fetch failed for %s: %s", product_url, exc)
    except RobotsDisallowed:
        return None, None, "robots_disallowed", []
    except Exception as exc:  # noqa: BLE001 -- one bad domain must not kill the whole run
        log.warning("line_pitch: fetch failed for %s: %s", home_url, exc)
        return None, None, "fetch_failed", []

    return "\n\n".join(texts), home_url, "fetched", urls_fetched


def _page_contains_line_name(page_text: str | None, line_name: str) -> bool:
    """The plausibility gate for a web_search-discovered domain (see
    discover_domain_via_web_search): the fetched home page must actually
    mention the line's own name somewhere, normalized the same way
    _norm_key does everywhere else in this module -- a web_search
    citation alone is never trusted, only a fetch that positively
    confirms it. Deliberately a plain normalized substring check, not a
    fuzzy/token-scatter one: a compound or slash name ('TCF/Twin City
    Fan') may legitimately fail this even for the line's own real
    domain if the page only ever spells out one half -- a disclosed,
    accepted false-negative risk, not a bug, since the alternative (a
    looser match) would let an unrelated page through instead."""
    if not page_text or not line_name:
        return False
    norm_page = " ".join(_NON_ALNUM.sub(" ", page_text.lower()).split())
    norm_name = " ".join(_NON_ALNUM.sub(" ", line_name.lower()).split())
    return bool(norm_name) and norm_name in norm_page


def discover_domain_via_web_search(cfg: Config, line: ProductLine) -> dict:
    """ONE web_search-enabled Sonnet call (app.llm.find_line_domain) to
    find a domain for a line whose own basis text named none -- used ONLY
    as a fallback, never in place of candidate_domains. Fetches the
    returned domain's home page and accepts it ONLY if the page actually
    names this line (_page_contains_line_name) -- the model's own
    citation is never trusted on its own. Never writes to `line` itself;
    the caller (generate_one_line) does that, since only it knows whether
    the fetch that follows actually succeeded end to end.

    Returns {"domain": str|None, "url": str|None, "page_text": str|None,
    "fetch_status": str, "searched": True, "cost_usd": float}. Only
    BudgetExceeded propagates; any other failure (web_search call itself
    errors, fetch fails, name doesn't match) comes back as domain=None,
    same as "we looked and found nothing" -- a bad domain lookup must
    never crash the run any more than a bad LLM pitch call does."""
    from app.llm import find_line_domain
    from app.spend import BudgetExceeded

    try:
        result = find_line_domain(line.name, line.category, line.description)
    except BudgetExceeded:
        raise
    except Exception as exc:  # noqa: BLE001 -- a failed search must not kill the whole run
        log.warning("line_pitch: domain search failed for %s: %s", line.name, exc)
        return {"domain": None, "url": None, "page_text": None, "fetch_status": "fetch_failed",
               "searched": True, "cost_usd": 0.0}

    domain = result.get("domain")
    if not domain or domain in EXCLUDED_DOMAINS:
        return {"domain": None, "url": None, "page_text": None, "fetch_status": "no_domain_found",
               "searched": True, "cost_usd": result.get("cost_usd", 0.0)}

    page_text, url, fetch_status, _urls = fetch_product_pages(domain)
    if fetch_status != "fetched" or not _page_contains_line_name(page_text, line.name):
        log.info("line_pitch: web_search domain %s for %s rejected (fetch_status=%s, name_match=%s)",
                 domain, line.name, fetch_status, _page_contains_line_name(page_text, line.name))
        return {"domain": None, "url": None, "page_text": None, "fetch_status": "no_domain_found",
               "searched": True, "cost_usd": result.get("cost_usd", 0.0)}

    return {"domain": domain, "url": url, "page_text": page_text, "fetch_status": "fetched",
           "searched": True, "cost_usd": result.get("cost_usd", 0.0)}


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


def _confirmed_branches(session: Session, line: ProductLine) -> list[str]:
    return sorted({
        b.branch for b in session.exec(
            select(ProductLineBranch).where(
                ProductLineBranch.product_line_id == line.id,
                ProductLineBranch.status == "confirmed_covered")).all()
    })


def line_row_only_pitch(session: Session, line: ProductLine) -> dict:
    """The restricted pitch for a line with NO fetched product page, for
    any reason -- built ONLY from this line's own building_role and
    confirmed branch coverage, zero LLM call, zero capability or
    product-category claims of any kind. See app.models.LinePitch's own
    docstring for why this replaced the old behavior (a full LLM-written
    pitch that then had every claim dropped by grounding, leaving a vague,
    unlabeled fragment -- the Aldes row in this feature's first
    production run)."""
    from app.accounts import ROLE_LABELS

    branches = _confirmed_branches(session, line)
    role_label = ROLE_LABELS.get(line.building_role, line.building_role)
    what_it_is = f"{line.name} is filed under DMG/ToroAire's {role_label} role on the line card."
    where_it_fits = (f"Confirmed on the line card at {', '.join(branches)}." if branches
                     else "Not yet confirmed on any branch's line card.")
    return {"what_it_is": what_it_is, "where_it_fits": where_it_fits,
           "typical_project_types": None, "elevator_pitch": None,
           "differentiators": [], "engineer_questions": [], "competitors": []}


def generate_one_line(session: Session, cfg: Config, line: ProductLine) -> dict:
    """One Sonnet call for this line -- but ONLY if a real product page was
    fetched. Per the 2026-09-03 rewrite: a line with no fetched page (no
    domain found, robots disallowed, or the fetch failed) gets
    line_row_only_pitch's deterministic template instead, with NO LLM call
    at all -- there is nothing to write a grounded capability claim
    against, so none is attempted. Never raises for a bad LLM response
    (records an error instead) -- ONLY lets app.spend.BudgetExceeded
    propagate, since that one must stop the whole run, not just this line.

    2026-09-04: if candidate_domains() (this line's own basis text, or a
    previously-discovered line.official_domain) produces nothing fetchable,
    ONE web_search-based domain lookup is tried (discover_domain_via_web_search)
    -- but ONLY when line.official_domain is not already set, since that
    field means a search already ran for this line at some point and found
    nothing worth trusting further, or found something and there's no
    reason to search again. A domain the search finds AND validates gets
    written onto the line immediately (before the pitch LLM call, so it
    persists even if that call then fails or the run's budget runs out) --
    see candidate_domains, which tries this field first from then on."""
    branches = _confirmed_branches(session, line)

    competitor_rows = competing_lines_by_role(session).get(line.building_role, [])
    own_key = _norm_key(line.name)
    candidates = sorted({
        row.manufacturer for row in competitor_rows
        if _norm_key(row.manufacturer) != own_key
    })

    domains = candidate_domains(line)
    page_text = page_url = None
    fetch_status = "no_domain_found"
    domain_source = "basis_text" if domains else None
    if domains:
        page_text, page_url, fetch_status, _urls = fetch_product_pages(domains[0])

    search_result = None
    if fetch_status != "fetched" and not line.official_domain:
        search_result = discover_domain_via_web_search(cfg, line)
        if search_result["domain"]:
            line.official_domain = search_result["domain"]
            line.official_domain_source = "web_search"
            line.official_domain_url = search_result["url"]
            session.add(line)
            session.commit()
            page_text, page_url = search_result["page_text"], search_result["url"]
            fetch_status = "fetched"
            domain_source = "web_search"
            domains = [search_result["domain"]]

    if fetch_status != "fetched":
        return {
            "ok": True, "line_id": line.id, "line_name": line.name,
            "raw": line_row_only_pitch(session, line),
            "page_text": None, "page_url": None, "fetch_status": fetch_status,
            "pitch_scope": "line_row_only", "domains_tried": domains, "model": None,
            "domain_source": None,
            "web_search_attempted": search_result is not None,
            "web_search_cost_usd": (search_result or {}).get("cost_usd", 0.0),
        }

    user_content = _build_user_content(line, branches, candidates, page_text, page_url)
    model = cfg.get("llm.line_pitch_model", "claude-sonnet-4-6")

    try:
        raw = generate_line_pitch(user_content)
    except BudgetExceeded:
        raise
    except Exception as exc:  # noqa: BLE001 -- one bad line must not kill the whole run, falls back below
        log.warning("line_pitch: LLM call failed for %s, falling back to line_row_only: %s",
                   line.name, exc)
        return {
            "ok": True, "line_id": line.id, "line_name": line.name,
            "raw": line_row_only_pitch(session, line),
            "page_text": None, "page_url": None, "fetch_status": "fetch_failed",
            "pitch_scope": "line_row_only", "domains_tried": domains, "model": None,
            "llm_error": f"{type(exc).__name__}: {exc}",
            "domain_source": domain_source,
            "web_search_attempted": search_result is not None,
            "web_search_cost_usd": (search_result or {}).get("cost_usd", 0.0),
        }

    return {
        "ok": True, "line_id": line.id, "line_name": line.name, "raw": raw,
        "page_text": page_text, "page_url": page_url, "fetch_status": fetch_status,
        "pitch_scope": "full", "domains_tried": domains, "model": model,
        "domain_source": domain_source,
        "web_search_attempted": search_result is not None,
        "web_search_cost_usd": (search_result or {}).get("cost_usd", 0.0),
    }


def _truncate_to_words(text: str, max_words: int) -> tuple[str, bool]:
    """Prefer cutting at the last complete SENTENCE that fits within
    max_words -- a hard word-count cut produces a pitch that trails off
    mid-thought ("We carry them across Central Coast, Central" -- a real
    case caught while reviewing this module's own first production run,
    2026-09-02). Only falls back to the hard word cut when no sentence
    boundary exists within the budget at all (a single very long
    sentence), so the field is never silently left empty."""
    words = text.split()
    if len(words) <= max_words:
        return text, False
    sentences = _SENTENCE_SPLIT_RE.split(text.strip())
    kept: list[str] = []
    word_count = 0
    for s in sentences:
        s_words = len(s.split())
        if word_count + s_words > max_words:
            break
        kept.append(s)
        word_count += s_words
    if kept:
        return " ".join(kept).strip(), True
    return " ".join(words[:max_words]), True


def _validate_competitor_names(competitors: list[dict], candidates: list[str]) -> list[dict]:
    """Never trust the schema's own honesty -- drop any competitor the
    model returned that isn't in the candidate set, in code, unconditionally."""
    candidate_set = {c.lower() for c in candidates}
    return [c for c in competitors if (c.get("competitor_name") or "").lower() in candidate_set]


def _write_pitch_and_competitors(session: Session, line: ProductLine, result: dict) -> dict:
    """pitch_scope='line_row_only' (see generate_one_line/line_row_only_pitch)
    writes what_it_is/where_it_fits verbatim (already deterministic,
    capability-claim-free template text -- nothing to ground, nothing to
    sanitize beyond the same pass everything else gets for consistency)
    and leaves elevator_pitch/typical_project_types/differentiators/
    engineer_questions empty, with NO competitor rows written -- there is
    no capability fact to position a competitor comparison against.

    pitch_scope='full' runs the complete grounding pass: EVERY field is
    sanitized (wrapping quotes stripped, '!' -> '.') and claim-checked
    (see ground_text/ground_list), the elevator pitch is truncated at a
    sentence boundary under MAX_ELEVATOR_PITCH_WORDS, and -- per the
    2026-09-03 register rewrite -- differentiators/engineer_questions are
    each capped to exactly ONE entry, with the single differentiator
    additionally REQUIRED to be a grounded claim (ground_required_claim,
    not the general list-item passthrough ground_list uses for a
    non-factual engineer question)."""
    raw = result["raw"]
    page_text = result["page_text"]
    pitch_scope = result.get("pitch_scope", "full")

    if pitch_scope == "line_row_only":
        what_it_is = _sanitize_field(raw.get("what_it_is"))
        where_it_fits = _sanitize_field(raw.get("where_it_fits"))
        typical = elevator = None
        diffs, questions = [], []
        truncated = False
        grounded_total = dropped_total = 0
    else:
        what_it_is, g1, d1 = ground_text(raw.get("what_it_is"), page_text)
        where_it_fits, g2, d2 = ground_text(raw.get("where_it_fits"), page_text)
        typical, g3, d3 = ground_text(raw.get("typical_project_types"), page_text)
        elevator, g4, d4 = ground_text(raw.get("elevator_pitch"), page_text)
        truncated = False
        if elevator:
            elevator, truncated = _truncate_to_words(elevator, MAX_ELEVATOR_PITCH_WORDS)

        # Capped to ONE each, per the 2026-09-03 register rewrite (was up
        # to 3) -- sliced from the model's raw output BEFORE grounding, so
        # no effort is spent checking items 2/3 that would be discarded
        # regardless of what the model returned.
        raw_diffs = (raw.get("differentiators") or [])[:1]
        raw_questions = (raw.get("engineer_questions") or [])[:1]
        one_diff, g5, d5 = ground_required_claim(raw_diffs[0] if raw_diffs else None, page_text)
        diffs = [one_diff] if one_diff else []
        questions, g6, d6 = ground_list(raw_questions, page_text)

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
    pitch.pitch_scope = pitch_scope
    pitch.grounded_claim_count = grounded_total
    pitch.dropped_claim_count = dropped_total
    pitch.model = result["model"]
    pitch.generated_at = utcnow()
    pitch.updated_at = utcnow()
    session.add(pitch)
    session.commit()
    session.refresh(pitch)

    n_competitors_written = competitors_offered = competitors_rejected = 0
    if pitch_scope == "full":
        role_rows = competing_lines_by_role(session).get(line.building_role, [])
        source_url_by_name = {row.manufacturer: row.source_url for row in role_rows}

        candidates = list(source_url_by_name.keys())
        offered = raw.get("competitors") or []
        valid_competitors = _validate_competitor_names(offered, candidates)
        competitors_offered = len(offered)
        competitors_rejected = len(offered) - len(valid_competitors)
        for c in valid_competitors:
            name = c["competitor_name"]
            existing_c = session.exec(
                select(LineCompetitor).where(LineCompetitor.product_line_id == line.id,
                                             LineCompetitor.competitor_name == name)).first()
            if existing_c is not None and existing_c.review_status in ("confirmed", "rejected"):
                continue  # a human decision on this pair is never overwritten
            why_lose = _sanitize_field(c.get("why_we_lose"))
            why_win = _sanitize_field(c.get("why_we_win"))
            if why_lose and _has_banned_phrase(why_lose):
                why_lose = None
            if why_win and _has_banned_phrase(why_win):
                why_win = None
            row = existing_c or LineCompetitor(product_line_id=line.id, competitor_name=name)
            row.why_we_lose = why_lose
            row.why_we_win = why_win
            row.evidence_url = source_url_by_name.get(name)
            row.review_status = "draft"
            row.model = result["model"]
            row.generated_at = utcnow()
            row.updated_at = utcnow()
            session.add(row)
            n_competitors_written += 1
        session.commit()

    return {
        "pitch_id": pitch.id, "pitch_scope": pitch_scope,
        "grounded_claims": grounded_total, "dropped_claims": dropped_total,
        "claims_extracted": grounded_total + dropped_total,
        "elevator_pitch_truncated": truncated, "competitors_written": n_competitors_written,
        "competitors_offered": competitors_offered,
        "competitors_rejected_not_on_map": competitors_rejected,
        "insufficient": pitch_scope == "full" and not (what_it_is or elevator or diffs),
    }


def run_line_pitch_generation(session: Session, cfg: Config, *, cap_usd: float = 5.0,
                              only_line_ids: set[int] | None = None) -> dict:
    """The whole run: every eligible ProductLine, one Sonnet call each,
    hard-capped at cap_usd total. Skips (a) any line with
    existence_verified is False (per the brief), (b) any line whose
    LinePitch is already 'confirmed' (a human's review is never silently
    redone). Does NOT skip a 'rejected' or 'draft' pitch -- a rejected
    pitch is a signal to try again differently, not a permanent no.

    only_line_ids (added 2026-09-04): when given, every other eligible
    line is skipped without touching its existing row at all -- for a
    targeted rerun of just the lines a specific fix affects (see `scout
    generate-line-pitches --only-lines`), leaving every other line's
    current draft exactly as it is rather than regenerating the whole
    card every time one thing changes."""
    lines = session.exec(
        select(ProductLine).where(ProductLine.existence_verified.is_not(False))
        .order_by(ProductLine.name)
    ).all()
    if only_line_ids is not None:
        lines = [l for l in lines if l.id in only_line_ids]

    stats = {
        "lines_total": len(lines), "lines_generated": 0, "lines_skipped_existence_false": 0,
        "lines_skipped_already_confirmed": 0, "lines_skipped_budget": 0, "lines_errored": 0,
        "grounded_claims_total": 0, "dropped_claims_total": 0, "claims_extracted_total": 0,
        "competitors_written_total": 0, "lines_line_row_only": 0, "lines_insufficient": 0,
        "web_search_attempted": 0, "web_search_domain_found": 0, "web_search_domain_not_found": 0,
        "web_search_cost_usd": 0.0,
        "fetch_status_counts": {}, "errors": [], "sample_pitches": [], "per_line_claims": [],
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
            except Exception as exc:  # noqa: BLE001 -- one bad line must never crash a 69-line batch
                stats["lines_errored"] += 1
                stats["errors"].append({"line": line.name, "error": f"{type(exc).__name__}: {exc}"})
                continue

            stats["fetch_status_counts"][result.get("fetch_status", "error")] = (
                stats["fetch_status_counts"].get(result.get("fetch_status", "error"), 0) + 1)

            if result.get("web_search_attempted"):
                stats["web_search_attempted"] += 1
                stats["web_search_cost_usd"] += result.get("web_search_cost_usd", 0.0)
                if result.get("domain_source") == "web_search":
                    stats["web_search_domain_found"] += 1
                else:
                    stats["web_search_domain_not_found"] += 1

            if not result.get("ok"):
                stats["lines_errored"] += 1
                stats["errors"].append({"line": line.name, "error": result.get("error")})
                continue

            write_stats = _write_pitch_and_competitors(session, line, result)
            stats["lines_generated"] += 1
            stats["grounded_claims_total"] += write_stats["grounded_claims"]
            stats["dropped_claims_total"] += write_stats["dropped_claims"]
            stats["claims_extracted_total"] += write_stats["claims_extracted"]
            stats["competitors_written_total"] += write_stats["competitors_written"]
            if write_stats["pitch_scope"] == "line_row_only":
                stats["lines_line_row_only"] += 1
            if write_stats["insufficient"]:
                stats["lines_insufficient"] += 1
            written_pitch = session.get(LinePitch, write_stats["pitch_id"])
            stats["per_line_claims"].append({
                "line": line.name, "pitch_scope": write_stats["pitch_scope"],
                "domain_source": result.get("domain_source"),
                "claims_extracted": write_stats["claims_extracted"],
                "claims_grounded": write_stats["grounded_claims"],
                "claims_dropped": write_stats["dropped_claims"],
                "insufficient": write_stats["insufficient"],
                "has_differentiator": bool(written_pitch.differentiators),
            })
            if len(stats["sample_pitches"]) < 5:
                pitch = session.get(LinePitch, write_stats["pitch_id"])
                stats["sample_pitches"].append({
                    "line": line.name, "elevator_pitch": pitch.elevator_pitch,
                    "what_it_is": pitch.what_it_is, "where_it_fits": pitch.where_it_fits,
                    "differentiators": pitch.differentiators,
                    "engineer_questions": pitch.engineer_questions,
                    "source_fetch_status": pitch.source_fetch_status,
                    "pitch_scope": pitch.pitch_scope,
                })
        stats["cost_usd"] = run.spent_usd
        stats["web_search_cost_usd"] = round(stats["web_search_cost_usd"], 4)

    return stats
