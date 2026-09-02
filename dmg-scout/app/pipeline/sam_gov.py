"""SAM.gov Division 23 spec-mention competitive intelligence -- NOT a lead
feed. See app/models.py's SpecMention/SamSolicitationCheck docstrings for
why this needed its own tables and its own pipeline, entirely separate from
RawDocument/Signal/Project: this answers "who is specifying what" (a
competitive-intelligence question about the DESIGN side), not "is there a
new building" (the question every other source in app/sources answers).

Scope: NAVFAC Southwest, USACE Los Angeles District, VA, GSA, and Air Force
(ORGANIZATIONS below, each queried unrestricted by NAICS -- these agencies
issue task orders under IDIQ/MACC/JOC vehicles that often don't carry a
"building construction" NAICS code at all) PLUS any other CA/AZ/NV
solicitation carrying MECHANICAL_NAICS -- DMG's own stated territory (see
app.llm.TRIAGE_SYSTEM: "California, Nevada and Arizona"). Organization
queries are NOT state-filtered at the API level (state filtering costs a
separate call per state); instead every notice is filtered to
placeOfPerformance state in STATES in Python after the fact. ptype=[o, p]
(Solicitation + Pre-Solicitation) -- a pre-solicitation often already
carries a draft spec package worth reading, even before the formal
solicitation posts.

Rate limit: SAM.gov's Get Opportunities API caps a personal API key around
10 requests/day against the SEARCH endpoint specifically (api.sam.gov/
opportunities/v2/search) -- confirmed real, not just documented: a live run
on 2026-08-13 hit an actual 429 after 12 search calls in one day (4 from an
earlier run that day + 8 from this module's own per-run query count).
limit=1000 per call means one call can return up to 1000 matching notices,
so the real constraint is query COUNT, not result volume. This module
spends 5 organization queries + 3 state/NAICS queries = 8 per run.
Attachment downloads (resourceLinks, hosted at sam.gov/api/prod/opps/v3/...)
are a separate endpoint, confirmed by direct test NOT to consume the same
quota.

Guard: every search call is logged to SamGovSearchCall (see app/models.py)
regardless of caller, and run_sam_gov checks the day's running total
against config.yaml's sam_gov.daily_call_budget (manual, default 8) or
sam_gov.scheduled_call_budget (is_scheduled=True, default 4) BEFORE each
call, stopping cleanly (not erroring) once the budget's spent -- whatever
notices were already found still get processed. Both budgets draw from the
SAME log, not independent allowances: the point of the smaller scheduled
budget is specifically that an unattended run must never be the reason a
same-day manual investigation finds the day's real SAM.gov quota already
gone, which is exactly what happened on 2026-08-13 before this guard
existed.

Detection: federal specs follow UFGS/CSI SectionFormat and are frequently
published as one file PER SECTION rather than a combined spec book with a
"DIVISION 23" cover page -- find_ufgs_23_series_text matches literal
"SECTION 23 XX XX" headings (which appear in both standalone-section files
and combined books, since a combined book's Division 23 IS a run of these
same section headings) rather than a division-level marker that a
standalone section file would never contain. Word/Excel attachments are
parsed via app.officetext -- federal agencies frequently publish spec
sections as .docx (confirmed directly: 2026-08-13's first real run had 7/42
attachments fail for exactly this reason, all Office formats hitting a
PDF-only parser).

FAR 11.104/11.105 discourage brand-name specifications, so a section that
specifies by performance/salient characteristics with no manufacturer named
is the EXPECTED common case, not a miss -- see SamSolicitationCheck's
"performance_spec_only" outcome and app.llm.SAM_GOV_SPEC_SYSTEM, which
asks the model to distinguish that from "nothing 23-series found at all."

Design-build solicitations carry no finished spec book at solicitation
time -- the contractor's own design team writes one later -- so they are
skipped via a title/description keyword match BEFORE spending an
attachment download, per the explicit "skip rather than store empty rows"
rule. Every solicitation actually looked at (skipped or attempted) gets
exactly one SamSolicitationCheck row, so a re-run never re-spends quota or
LLM budget on a notice already resolved either way.

Grounding: every manufacturer_name extract_division_23_mentions returns is
checked against ufgs_text (the exact text handed to the LLM for that
notice) via app.grounding.name_grounded before a SpecMention is ever
written -- this is the field the whole "who names DMG's line" conclusion
rests on, and it had NO grounding check at all until 2026-09 (confirmed
zero SpecMention rows existed in production at the time this was added, so
nothing already-persisted needed correction). A name that fails grounding
is dropped, not stored, and recorded via the ungrounded_mentions_only
outcome rather than silently folded into no_ufgs_23_series_found -- see
SamSolicitationCheck's own docstring.
"""
from __future__ import annotations

import logging
import os
import re
from datetime import datetime

import httpx
from sqlmodel import Session, func, select

from app.config import Config
from app.grounding import name_grounded
from app.llm import LLMUnavailable, extract_division_23_mentions
from app.models import SamGovSearchCall, SamSolicitationCheck, SpecMention, utcnow
from app.normalize import normalize_name
from app.officetext import docx_to_text, xlsx_to_text
from app.pdftext import pdf_to_text

log = logging.getLogger(__name__)

SAM_GOV_ENV = "SAM_GOV_API_KEY"
SEARCH_URL = "https://api.sam.gov/opportunities/v2/search"

# DMG's own stated territory -- see app.llm.TRIAGE_SYSTEM. Organization
# queries are filtered to this set in Python (see module docstring); the
# state/NAICS sweep queries the API directly per state.
STATES = ("CA", "AZ", "NV")

# One NAICS code for the state/NAICS sweep -- 236220 = Commercial and
# Institutional Building Construction, the code that actually carries a
# full architectural/engineering spec package (238220's mostly small
# service/repair task orders proved this out empirically on 2026-08-13:
# 0/42 solicitations under that code yielded a spec section at all).
MECHANICAL_NAICS = "236220"

# Broad, NAICS-unrestricted queries -- these agencies' IDIQ/MACC/JOC task
# orders often carry a NAICS code that has nothing to do with "building
# construction" even when the underlying work does. organizationName does
# a general/partial match against SAM.gov's fullParentPathName, so a
# distinctive substring is enough; results are filtered to STATES in
# Python afterward, not via a separate per-state API call each.
ORGANIZATIONS = (
    "NAVFAC SOUTHWEST",
    "LOS ANGELES DISTRICT",       # USACE Los Angeles District
    "VETERANS AFFAIRS",
    "GENERAL SERVICES ADMINISTRATION",
    "DEPT OF THE AIR FORCE",
)

# Solicitation title/description keywords indicating a design-build
# procurement -- these carry no finished spec book at solicitation time, so
# there is nothing here for this source to extract. Checked before ANY
# attachment download.
DESIGN_BUILD_KEYWORDS = (
    "design-build", "design build", "design/build", " db ", "(db)",
)

MAX_ATTACHMENTS_PER_NOTICE = 6
# Spec books run long; app.pdftext's default 80-page cap is sized for
# meeting packets, not multi-hundred-page construction specifications.
MAX_PDF_PAGES = 600
# Confirmed real, 2026-08-13: a large drawing/exhibit PDF (architectural
# floor plans, CAD-exported, heavy embedded raster images) hung pdfplumber
# and ran this process's RSS from ~2.4GB to 3.2GB+ (VmPeak 6.4GB) before it
# was killed by hand -- a page-count cap alone doesn't protect against a
# small number of enormous, image-heavy pages. Spec-section text documents
# are never anywhere near this size; a drawing set that is gets skipped
# before parsing is even attempted, recorded as fetch_failed with the
# reason, never silently dropped.
MAX_ATTACHMENT_BYTES = 15_000_000

# Matches "SECTION 23 64 26" and similar UFGS numbers, capturing the
# division (group 1) so a later match can be tested for "still 23".
_UFGS_SECTION_RE = re.compile(r"SECTION\s+(\d{2})\s?(\d{2})\s?(\d{2})(?:\.\d{2})?\b", re.IGNORECASE)


def _api_key() -> str:
    return os.environ.get(SAM_GOV_ENV, "")


def search_opportunities(*, api_key: str, posted_from: str, posted_to: str,
                         organization_name: str | None = None, state: str | None = None,
                         ncode: str | None = None, ptype: tuple[str, ...] = ("o", "p"),
                         limit: int = 1000) -> list[dict]:
    """One call against SAM.gov's rate-limited search endpoint. Callers must
    budget their own call count -- see module docstring."""
    params = {
        "api_key": api_key, "postedFrom": posted_from, "postedTo": posted_to,
        "ptype": list(ptype), "limit": limit, "offset": 0,
    }
    if organization_name:
        params["organizationName"] = organization_name
    if state:
        params["state"] = state
    if ncode:
        params["ncode"] = ncode
    resp = httpx.get(SEARCH_URL, params=params, timeout=30)
    resp.raise_for_status()
    return resp.json().get("opportunitiesData", [])


def is_design_build(notice: dict) -> bool:
    haystack = f"{notice.get('title', '')} {notice.get('description', '')}".lower()
    return any(kw in haystack for kw in DESIGN_BUILD_KEYWORDS)


def _notice_state(notice: dict) -> str | None:
    return (notice.get("placeOfPerformance") or {}).get("state", {}).get("code")


def find_ufgs_23_series_text(text: str) -> str | None:
    """From the first "SECTION 23 XX XX" heading to the next heading whose
    division number is NOT 23, or end of text if none follows. Matches
    equally well whether the source is a standalone per-section file (one
    match, runs to end) or a combined spec book (several consecutive
    23-series matches, ending at the first non-23 section) -- see module
    docstring for why this replaced a "DIVISION 23" cover-page search.
    None if no 23-series section heading is present at all -- the caller
    records this as no_ufgs_23_series_found, not an error."""
    matches = list(_UFGS_SECTION_RE.finditer(text))
    starts_23 = [m for m in matches if m.group(1) == "23"]
    if not starts_23:
        return None
    start = starts_23[0].start()
    end = len(text)
    for m in matches:
        if m.start() > start and m.group(1) != "23":
            end = m.start()
            break
    return text[start:end]


def _download_attachment(url: str, api_key: str) -> tuple[bytes, str] | None:
    """Returns (content, filename) or None on failure. filename is read
    from the Content-Disposition header where the actual manufacturer/
    parser dispatch happens -- see _text_from_attachment."""
    try:
        resp = httpx.get(url, params={"api_key": api_key}, timeout=60, follow_redirects=True)
        resp.raise_for_status()
        disposition = resp.headers.get("content-disposition", "")
        m = re.search(r'filename="?([^";]+)"?', disposition)
        filename = m.group(1) if m else str(resp.url).split("?")[0]
        return resp.content, filename
    except httpx.HTTPError as exc:
        log.warning("SAM.gov attachment download failed for %s: %s", url, exc)
        return None


def _text_from_attachment(data: bytes, filename: str) -> str:
    """Dispatches on file extension. Raises on a genuinely unparseable file
    (caller catches and records fetch_failed) -- an unrecognized extension
    is treated the same as a parse failure rather than silently skipped,
    so it still counts toward an honest fetch_failed total."""
    ext = filename.lower().rsplit(".", 1)[-1] if "." in filename else ""
    if ext == "pdf":
        return pdf_to_text(data, max_pages=MAX_PDF_PAGES)
    if ext == "docx":
        return docx_to_text(data)
    if ext in ("xlsx", "xlsm"):
        return xlsx_to_text(data)
    raise ValueError(f"unsupported attachment type: {filename!r}")


def _line_card_names_norm(cfg: Config) -> dict[str, str]:
    lines = cfg.get("accounts.line_card", []) or []
    return {normalize_name(e["name"]): e["name"] for e in lines}


def _search_calls_today(session: Session) -> int:
    """Every SamGovSearchCall logged since UTC midnight -- the shared
    counter both the manual and scheduled budgets check against. See
    SamGovSearchCall's docstring for why this has to be a real persisted
    log rather than an in-process counter: it has to see calls made by
    OTHER invocations earlier the same day, manual or scheduled.

    utcnow() is naive-but-UTC by this codebase's own convention (see its
    docstring) -- .astimezone() on a naive value assumes LOCAL system time
    and silently shifts it, which is exactly the bug this comment is here
    to stop someone from reintroducing: it made start_of_day land on the
    wrong calendar day entirely, so this count was always 0 regardless of
    how many calls had actually been logged. Stay naive throughout."""
    start_of_day = utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    return session.exec(
        select(func.count(SamGovSearchCall.id)).where(SamGovSearchCall.called_at >= start_of_day)
    ).one()


def _record_search_call(session: Session) -> None:
    session.add(SamGovSearchCall())
    session.commit()


def run_sam_gov(session: Session, cfg: Config, *, posted_from: str, posted_to: str,
                max_notices: int | None = None, is_scheduled: bool = False) -> dict:
    """Search NAVFAC Southwest, USACE LA District, VA, GSA, Air Force, and
    CA/AZ/NV mechanical solicitations broadly, extract UFGS Division 23
    manufacturer mentions from every notice not already checked, write
    SpecMention rows. Idempotent: a notice_id already present in
    sam_solicitation_checks is never re-fetched or re-charged against the
    search quota or the LLM budget.

    posted_from/posted_to: MM/dd/yyyy strings, SAM.gov's own required
    date-range parameters (max 1 year apart) -- passed explicitly rather
    than defaulted, since spending a day's search-request budget on a
    window should always be an intentional choice, not a default.

    is_scheduled: True for an unattended/automated invocation -- caps this
    run to config.yaml's sam_gov.scheduled_call_budget (default 4) rather
    than sam_gov.daily_call_budget (default 8), against the SAME shared
    daily call log -- see the module docstring's Guard section. Checked
    before EVERY search call, not just at the top of the run, since a
    manual investigation could spend part of the day's budget while a
    scheduled run is already partway through its own queries.
    """
    api_key = _api_key()
    stats = {"searched": 0, "notices_seen": 0, "already_checked": 0,
             "design_build_skipped": 0, "no_ufgs_23_series_found": 0,
             "performance_spec_only": 0, "fetch_failed": 0,
             "spec_mentions_found": 0, "spec_mentions_written": 0, "on_line_card": 0,
             "manufacturer_mentions_rejected_ungrounded": 0, "ungrounded_mentions_only": 0,
             "search_budget_stopped_early": False}
    if not api_key:
        log.warning("%s is not set -- SAM.gov source disarmed", SAM_GOV_ENV)
        return stats

    budget = cfg.get("sam_gov.scheduled_call_budget", 4) if is_scheduled else cfg.get("sam_gov.daily_call_budget", 8)
    budget_kind = "scheduled" if is_scheduled else "manual"

    def _budget_ok() -> bool:
        spent = _search_calls_today(session)
        if spent >= budget:
            log.warning("SAM.gov %s call budget reached (%d/%d spent today) -- stopping search "
                       "queries early; notices already found are still processed", budget_kind, spent, budget)
            stats["search_budget_stopped_early"] = True
            return False
        return True

    notices: dict[str, dict] = {}

    for org in ORGANIZATIONS:
        if not _budget_ok():
            break
        try:
            results = search_opportunities(api_key=api_key, posted_from=posted_from,
                                           posted_to=posted_to, organization_name=org)
            _record_search_call(session)
            stats["searched"] += 1
        except httpx.HTTPError as exc:
            log.error("SAM.gov search failed for org=%r: %s", org, exc)
            continue
        for n in results:
            if _notice_state(n) in STATES:
                notices[n["noticeId"]] = n

    for state in STATES:
        if not _budget_ok():
            break
        try:
            results = search_opportunities(api_key=api_key, posted_from=posted_from,
                                           posted_to=posted_to, state=state, ncode=MECHANICAL_NAICS)
            _record_search_call(session)
            stats["searched"] += 1
        except httpx.HTTPError as exc:
            log.error("SAM.gov search failed for state=%r: %s", state, exc)
            continue
        for n in results:
            notices[n["noticeId"]] = n

    line_card_names = _line_card_names_norm(cfg)

    for notice_id, notice in notices.items():
        if max_notices is not None and stats["notices_seen"] >= max_notices:
            break
        stats["notices_seen"] += 1
        existing = session.exec(
            select(SamSolicitationCheck).where(SamSolicitationCheck.notice_id == notice_id)
        ).first()
        if existing:
            stats["already_checked"] += 1
            continue

        posted_date = None
        if notice.get("postedDate"):
            try:
                posted_date = datetime.fromisoformat(notice["postedDate"])
            except ValueError:
                pass
        common = dict(
            notice_id=notice_id,
            solicitation_number=notice.get("solicitationNumber"),
            title=notice.get("title", ""),
            agency=notice.get("fullParentPathName"),
            state=_notice_state(notice),
            naics_code=notice.get("naicsCode"),
            posted_date=posted_date,
        )

        if is_design_build(notice):
            stats["design_build_skipped"] += 1
            session.add(SamSolicitationCheck(
                **common, outcome="design_build_skipped",
                detail="title/description matched a design-build keyword"))
            session.commit()
            continue

        ufgs_text = None
        fetch_error = None
        for link in (notice.get("resourceLinks") or [])[:MAX_ATTACHMENTS_PER_NOTICE]:
            downloaded = _download_attachment(link, api_key)
            if downloaded is None:
                fetch_error = "one or more attachment downloads failed"
                continue
            data, filename = downloaded
            if len(data) > MAX_ATTACHMENT_BYTES:
                fetch_error = f"{filename}: {len(data)} bytes exceeds MAX_ATTACHMENT_BYTES, skipped unparsed"
                log.warning("SAM.gov attachment too large to parse safely: %s", fetch_error)
                continue
            try:
                text = _text_from_attachment(data, filename)
            except Exception as exc:  # noqa: BLE001 — a bad attachment must not kill the whole run
                fetch_error = f"{filename}: parse failed: {exc}"
                continue
            section = find_ufgs_23_series_text(text)
            if section:
                ufgs_text = section
                break

        if not ufgs_text:
            outcome = "fetch_failed" if fetch_error else "no_ufgs_23_series_found"
            stats[outcome] += 1
            session.add(SamSolicitationCheck(**common, outcome=outcome, detail=fetch_error))
            session.commit()
            continue

        try:
            result = extract_division_23_mentions(
                ufgs_text, title=notice.get("title", ""), url=notice.get("uiLink", ""))
        except LLMUnavailable:
            raise
        except Exception as exc:  # noqa: BLE001 — one bad notice must not kill the whole run
            stats["fetch_failed"] += 1
            session.add(SamSolicitationCheck(
                **common, outcome="fetch_failed", detail=f"LLM extraction failed: {exc}"))
            session.commit()
            continue

        specifying_firm = result.get("specifying_firm")
        written_any = False
        rejected_ungrounded: list[str] = []
        for section in result.get("sections", []):
            mentions = []
            if section.get("basis_of_design_manufacturer"):
                mentions.append((section["basis_of_design_manufacturer"], "basis_of_design"))
            for m in section.get("or_equal_manufacturers", []):
                mentions.append((m, "or_equal"))
            for manufacturer_name, mention_type in mentions:
                # This is the field the whole competitive-intelligence conclusion
                # rests on -- an ungrounded name is dropped, not stored, same
                # reject-not-downgrade discipline as app.grounding's other checks.
                # ufgs_text is the exact text handed to the LLM for this notice.
                if not name_grounded(manufacturer_name, ufgs_text):
                    stats["manufacturer_mentions_rejected_ungrounded"] += 1
                    rejected_ungrounded.append(
                        f"{manufacturer_name!r} ({mention_type}, section {section.get('spec_section')})")
                    continue
                matched = line_card_names.get(normalize_name(manufacturer_name))
                session.add(SpecMention(
                    notice_id=notice_id, solicitation_number=notice.get("solicitationNumber"),
                    title=notice.get("title", ""), agency=notice.get("fullParentPathName"),
                    state=common["state"], source_url=notice.get("uiLink"),
                    specifying_firm=specifying_firm,
                    spec_section=section.get("spec_section"),
                    spec_section_title=section.get("spec_section_title"),
                    manufacturer_name=manufacturer_name, mention_type=mention_type,
                    on_dmg_line_card=matched is not None, dmg_line_card_name=matched,
                    posted_date=posted_date,
                ))
                stats["spec_mentions_written"] += 1
                if matched:
                    stats["on_line_card"] += 1
                written_any = True

        if written_any:
            stats["spec_mentions_found"] += 1
            session.add(SamSolicitationCheck(**common, outcome="spec_mentions_found"))
        elif result.get("performance_spec_only"):
            stats["performance_spec_only"] += 1
            session.add(SamSolicitationCheck(
                **common, outcome="performance_spec_only",
                detail="23-series UFGS text found; specifies by performance/salient "
                       "characteristics only, per FAR 11.104/11.105 -- no manufacturer named"))
        elif rejected_ungrounded:
            # Distinct from no_ufgs_23_series_found: the model DID assert
            # manufacturer names, they just don't appear anywhere in the
            # document text it was given -- a fabrication catch, not an
            # absence. Recorded, not silently folded into "nothing found".
            stats["ungrounded_mentions_only"] += 1
            session.add(SamSolicitationCheck(
                **common, outcome="ungrounded_mentions_only",
                detail=f"model named {len(rejected_ungrounded)} manufacturer mention(s) not "
                       f"found in the extracted 23-series text -- rejected, none written: "
                       + "; ".join(rejected_ungrounded)[:2000]))
        else:
            stats["no_ufgs_23_series_found"] += 1
            session.add(SamSolicitationCheck(
                **common, outcome="no_ufgs_23_series_found",
                detail="23-series text located but the model found neither manufacturer "
                       "mentions nor performance-spec content"))
        session.commit()

    return stats
