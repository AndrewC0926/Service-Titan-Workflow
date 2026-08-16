"""UA Local 250's own public signatory/service-agreement contractor list --
a partial substitute for the DIR public works registry, which turned out to
be blocked (see app/web/main.py's /contractors docstring / this task's
commit message: services.dir.ca.gov and cadir.my.salesforce-sites.com both
disallow automated access sitewide in robots.txt, and DIR publishes no
bulk/API alternative).

Compliance, checked before any automated access (2026-08-16): socalhvacr.info
(UA Local 250's actual site hosting the contractor list, confirmed via the
page itself -- "Copyright (c) 2026 UA Local 250") has a robots.txt disallowing
only /404, with a Sitemap directive -- no restriction on /contractors. No
dedicated Terms of Use page is linked from the site; the only usage text
present is a standard copyright notice and a cookie-consent banner, neither
of which restricts automated reading of a public page. PoliteClient (below)
re-checks robots.txt live at request time regardless, same as every other
adapter in this codebase -- this module gets no special exemption.

Cal-SMACNA (sheet metal signatories) was evaluated too and is NOT built here:
cal-smacna.org's own membership-directory page states plainly "If you would
like a copy of the CAL SMACNA membership directory, please contact our office
at team@cal-smacna.org" -- there is no public, browsable, or bulk directory
to access at all, regardless of what robots.txt permits (which is itself
permissive -- only /wp-admin/ disallowed). Nothing to scrape means nothing
built; if this data is wanted, it is a manual email to that address, not a
pipeline.

Mechanism: socalhvacr.info/contractors is a single static, server-rendered
page (GoDaddy Website Builder) -- the entire ~108-contractor list renders
into the initial HTML with no pagination, search, or login, structured as
a flat sequence of <p> tags: a <p><strong>NAME</strong></p> starts each
record, followed by <p> tags for street address, "City, ST ZIP", "Phone:
...", and an optional website link, until the next <strong>-tagged <p>.
Confirmed working end to end against the real site, 2026-08-16.

Deliberately NOT a durable raw table the way EbeweBenchmark is: EBEWE's raw
values (EUI, ENERGY STAR score) are independently useful and displayed as
board columns in their own right. Local 250's name/address/phone carry no
value beyond the match itself, so this module fetches fresh each run and
writes only the result -- a boolean, a matched-name audit trail, and a
timestamp -- directly onto Contractor. Simpler, and nothing to keep in sync.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime

from rapidfuzz import fuzz
from selectolax.parser import HTMLParser
from sqlmodel import Session, select

from app.http import PoliteClient
from app.models import Contractor, SourceRun, utcnow

log = logging.getLogger(__name__)

# Deliberately NOT app.normalize.normalize_name: that function is tuned for
# PROJECT/SPE names (strips "Building A", "Site 3", trailing project codes
# like "NV11") and is actively wrong here -- caught 2026-08-16 when it
# silently reduced the real company "Building Aire Inc." to an empty string
# (its own _PHASE regex treats "building" + up to 4 trailing characters as
# a strippable project-building label, and "aire" is exactly 4 characters),
# which then spuriously tied against every OTHER company whose name also
# happened to collapse to empty (single-letter/coded CSLB names like "DB1",
# "M C M" -- rapidfuzz scores two empty strings as a perfect 100 match).
# This module's own minimal cleaner only strips legal suffixes and
# punctuation -- nothing that a real company name can collide with.
_LEGAL_SUFFIXES = ("llc", "inc", "incorporated", "corp", "corporation", "co",
                   "company", "lp", "llp", "ltd", "limited")

SOURCE = "ua_local_250"
CONTRACTORS_URL = "https://socalhvacr.info/contractors"

_CITY_STATE_ZIP = re.compile(r"^(.+?),\s*([A-Z]{2})\s+(\d{5})")
_PHONE = re.compile(r"Phone:\s*(.+)")
_PARENS = re.compile(r"\([^)]*\)")

# Name-only matches need to clear a high bar since this writes an unreviewed
# flag directly onto a Contractor row (no human review step, unlike
# app.voice_match's MATCH_THRESHOLD=60, which only ever feeds a
# human-picked suggestion list). When the two sides also agree on city,
# that agreement is real corroboration -- an independent fact neither
# normalize_name nor rapidfuzz could fabricate -- so the bar drops slightly,
# same reasoning EBEWE's ain_last3 checksum used to tighten confidence
# rather than loosen it. Both are judgment calls, not measured against
# labeled outcomes -- see app/assumptions.py's "Union signatory match
# thresholds" entry.
NAME_ONLY_THRESHOLD = 95.0
NAME_WITH_CITY_THRESHOLD = 90.0

# A normalized name shorter than this carries almost no discriminative power
# for fuzzy matching (CSLB's own roster has real short/coded business names
# like "DB1", "M C M") -- excluded as a candidate entirely rather than risk
# a coincidental high score against an unrelated short name.
_MIN_NAME_LEN = 4


def _parse_contractors(html: str) -> list[dict]:
    """Every record the page states, in order, exactly as written -- never
    deduplicated or filtered here. A record with no city (2 of 108 as of
    2026-08-16: the page itself lists "Air Flow Mechanical (website only)"
    and "Schultz" with no city/state/zip) is a gap in the SOURCE, not a
    parser bug -- passed through with city=None rather than dropped."""
    tree = HTMLParser(html)
    records: list[dict] = []
    cur: dict | None = None
    for p in tree.css("p"):
        strong = p.css_first("strong")
        text = p.text(strip=True)
        if strong and strong.text(strip=True):
            if cur:
                records.append(cur)
            cur = {"name": strong.text(strip=True), "address": None, "city": None,
                   "state": None, "zip": None, "phone": None, "website": None}
            continue
        if cur is None or not text:
            continue
        m = _CITY_STATE_ZIP.match(text)
        a = p.css_first("a")
        if m:
            cur["city"], cur["state"], cur["zip"] = m.group(1), m.group(2), m.group(3)
        elif text.startswith("Phone:"):
            pm = _PHONE.match(text)
            if pm:
                cur["phone"] = pm.group(1).strip()
        elif a and a.attributes.get("href"):
            cur["website"] = a.attributes.get("href")
        elif cur["address"] is None and not text.startswith("http"):
            cur["address"] = text
    if cur:
        records.append(cur)
    return records


def _clean_name(name: str) -> str:
    """Lowercase, strip parenthetical aliasing notes ("(Formerly THERMA
    LLC)"), punctuation, and legal suffixes only -- see the module-level
    comment on why this does NOT reuse app.normalize.normalize_name."""
    s = _PARENS.sub(" ", name).lower().strip()
    s = s.replace(".", "")
    s = re.sub(r"[^\w\s-]", " ", s)
    for suf in _LEGAL_SUFFIXES:
        s = re.sub(rf"\b{re.escape(suf)}\b", " ", s)
    return re.sub(r"[\s-]+", " ", s).strip()


def match_local250(session: Session, records: list[dict]) -> dict:
    """Resets every Contractor's ua_local_250_signatory to False, then
    re-applies matches from THIS fetch -- a full refresh, not an
    accumulate-forever set, because signatory status genuinely lapses (a
    contractor's agreement can end) and Local 250's own list can shrink.
    Safe to re-run.

    A Local 250 entry matches a Contractor only when exactly one CSLB
    contractor clears the threshold -- ties are excluded as ambiguous
    rather than guessed at, same discipline app.pipeline.ebewe's join
    uses for its own many-to-one collisions."""
    contractors = session.exec(select(Contractor)).all()
    # Precomputed once, not re-normalized on every one of the 108 records --
    # a contractor with both business_name and full_business_name appears
    # twice, each candidate name matched independently.
    candidates: list[tuple[Contractor, str]] = []
    for c in contractors:
        for candidate_name in (c.business_name, c.full_business_name):
            if not candidate_name:
                continue
            norm_candidate = _clean_name(candidate_name)
            if len(norm_candidate) >= _MIN_NAME_LEN:
                candidates.append((c, norm_candidate))
        c.ua_local_250_signatory = False
        c.ua_local_250_matched_name = None
        session.add(c)

    checked_at = utcnow()
    matched, ambiguous, unmatched = 0, 0, 0
    for rec in records:
        norm = _clean_name(rec["name"])
        if len(norm) < _MIN_NAME_LEN:
            unmatched += 1
            continue
        rec_city = (rec.get("city") or "").strip().lower()
        best_score = 0.0
        best_by_id: dict[int, Contractor] = {}
        for c, norm_candidate in candidates:
            score = fuzz.token_sort_ratio(norm, norm_candidate)
            same_city = bool(rec_city and c.city and rec_city == c.city.strip().lower())
            threshold = NAME_WITH_CITY_THRESHOLD if same_city else NAME_ONLY_THRESHOLD
            if score < threshold:
                continue
            if score > best_score:
                best_score, best_by_id = score, {c.id: c}
            elif score == best_score:
                best_by_id[c.id] = c
        best = list(best_by_id.values())
        if len(best) == 1:
            c = best[0]
            c.ua_local_250_signatory = True
            c.ua_local_250_matched_name = rec["name"]
            c.ua_local_250_checked_at = checked_at
            session.add(c)
            matched += 1
        elif len(best) > 1:
            ambiguous += 1
            log.info("local250: ambiguous match for %r -- %d CSLB candidates tied, none applied",
                     rec["name"], len(best))
        else:
            unmatched += 1

    session.commit()
    return {"local250_total": len(records), "matched": matched,
            "ambiguous": ambiguous, "unmatched": unmatched}


def fetch_and_match_local250(session: Session, client: PoliteClient | None = None,
                             html: str | None = None) -> dict:
    """Fetches the current list (unless html is given, for tests/replays),
    matches it onto the CSLB roster, and records a SourceRun -- so a change
    to the page's markup, or the page going away, shows up on /health
    instead of silently freezing every contractor's signatory flag at
    whatever it last was. See app.cli's weekly pipeline wiring."""
    run = SourceRun(source=SOURCE)
    session.add(run)
    session.commit()

    error = None
    stats = {"local250_total": 0, "matched": 0, "ambiguous": 0, "unmatched": 0}
    try:
        if html is None:
            owns_client = client is None
            client = client or PoliteClient()
            try:
                resp = client.get(CONTRACTORS_URL)
                html = resp.text
            finally:
                if owns_client:
                    client.close()
        records = _parse_contractors(html)
        if not records:
            raise RuntimeError("parsed zero contractor records -- page structure may have changed")
        stats = match_local250(session, records)
    except Exception as exc:  # noqa: BLE001 — recorded on the SourceRun, not raised past this stage
        session.rollback()
        error = f"{type(exc).__name__}: {exc}"
        log.error("UA Local 250 fetch/match failed: %s", error)

    run.finished_at = utcnow()
    run.records_fetched = stats["local250_total"]
    run.records_new = stats["matched"]
    run.ok = error is None
    run.error = error
    session.add(run)
    session.commit()
    return {**stats, "ok": error is None, "error": error}
