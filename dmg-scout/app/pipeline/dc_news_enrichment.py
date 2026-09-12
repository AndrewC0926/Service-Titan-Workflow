"""Data Center Frontier / Data Center Dynamics RSS — ENRICHMENT ONLY (Phase
6f). Never lead generation: this module has no code path to Project() at
all, by construction, not by a runtime check. An article that matches no
existing project is discarded, never queued for review and never used to
create one.

Both feeds are confirmed real, working, and unrestricted for this purpose:
  - DCD: https://www.datacenterdynamics.com/en/rss/ (RSS 2.0)
  - DCF: no conventional /feed/ path; discoverable via the site's own
    <link rel="alternate"> tag, parameterized by section — hyperscale and
    site-selection sections carry the most matchable developer+location+MW
    text (DCF's default "home" feed skews toward long-form analysis with
    much lower hit rate, so it's deliberately not used here).

Only the feed's own title + description is ever read — never the article
body. That is not a rate-limiting courtesy, it is the whole point: the task
is matching a headline against projects already known from CEQA/GOED, not
building a second news-scraping source.

Matching is conservative on purpose: exact normalized developer-name match
against an ACTIVE data_center project, disambiguated by location text only
when one developer has multiple active projects. No fuzzy matching, no
partial-name matching — a wrong corroborating signal on someone else's
project is worse than a missed one.
"""
from __future__ import annotations

import calendar
import hashlib
import logging

import feedparser
from sqlmodel import select

from app.config import Config
from app.http import PoliteClient
from app.llm import dc_news_facts
from app.models import (
    ACTIVE_STATUSES, Category, Project, RawDocument, Signal, SignalType, from_unix,
)
from app.normalize import normalize_name
from app.sources.base import scrub

log = logging.getLogger(__name__)

FEEDS = {
    "dcd": "https://www.datacenterdynamics.com/en/rss/",
    "dcf_hyperscale": 'https://www.datacenterfrontier.com/__rss/website-scheduled-content.xml?input={"sectionAlias":"hyperscale"}',
    "dcf_site_selection": 'https://www.datacenterfrontier.com/__rss/website-scheduled-content.xml?input={"sectionAlias":"site-selection"}',
}


def _match_project(session, developer: str, location_text: str | None) -> Project | None:
    dev_norm = normalize_name(developer)
    if not dev_norm:
        return None
    candidates = session.exec(
        select(Project).where(Project.status.in_(ACTIVE_STATUSES),
                              Project.category == Category.data_center)).all()
    matches = [p for p in candidates if p.developer and normalize_name(p.developer) == dev_norm]
    if not matches:
        return None
    if len(matches) == 1:
        return matches[0]
    # Same developer, multiple active projects — disambiguate by whether the
    # article's location text mentions the project's county/state. If it
    # can't be disambiguated, skip rather than guess which project it's about.
    if not location_text:
        return None
    loc_lower = location_text.lower()
    narrowed = [
        p for p in matches
        if (p.county and p.county.lower() in loc_lower) or (p.state and p.state.lower() in loc_lower)
    ]
    return narrowed[0] if len(narrowed) == 1 else None


def run_dc_news_enrichment(session, cfg: Config, client: PoliteClient) -> dict:
    """Fetch DCD/DCF, match each entry against existing active data-center
    projects, attach a corroborating Signal to matches. Returns per-feed and
    total stats. Safe to re-run: RawDocument's (source, source_uid) unique
    constraint means an already-seen article is skipped, not duplicated.

    Rescopes run_size_score() to every project actually touched this run
    (deduped -- the same project can be matched from more than one feed
    entry) so a project's score/window reflect the just-attached signal in
    the SAME transaction as the attach, instead of sitting stale until the
    next `scout pipeline` run -- this function is a standalone CLI command,
    not one of that pipeline's own stages, so nothing else would rescore it.

    Refuses to run at all when SCOUT_VERIFYING_AGAINST_PROD is set -- see
    app.runguard.refuse_if_verifying_against_prod's own docstring."""
    from app.pipeline.resolve import link_signal_to_project
    from app.pipeline.size_score import run_size_score
    from app.runguard import refuse_if_verifying_against_prod

    refuse_if_verifying_against_prod("run_dc_news_enrichment")

    stats = {"entries_seen": 0, "matched": 0, "attached": 0, "feeds": {}}
    touched_ids: set[int] = set()
    for feed_name, feed_url in FEEDS.items():
        feed_stats = {"entries": 0, "matched": 0, "attached": 0, "errors": 0}
        try:
            raw = client.get_bytes(feed_url)
        except Exception as exc:  # noqa: BLE001 — one feed's outage shouldn't kill the others
            log.warning("dc_news_enrichment: feed %s failed: %s", feed_name, exc)
            feed_stats["errors"] += 1
            stats["feeds"][feed_name] = feed_stats
            continue

        parsed = feedparser.parse(raw)
        for entry in parsed.entries:
            title = entry.get("title", "")
            summary = entry.get("summary", "") or entry.get("description", "")
            link = entry.get("link", "")
            if not title or not link:
                continue
            feed_stats["entries"] += 1
            stats["entries_seen"] += 1

            # Deduped by link ALONE, not (feed_name, link): DCF's hyperscale
            # and site-selection sections can legitimately both list the same
            # underlying article, and that must not attach two redundant
            # signals for one story.
            already = session.exec(
                select(RawDocument).where(RawDocument.source.in_(FEEDS.keys()),
                                          RawDocument.source_uid == link)).first()
            if already:
                continue

            try:
                facts = dc_news_facts(title, summary)
            except Exception as exc:  # noqa: BLE001 — one bad extraction shouldn't kill the run
                log.warning("dc_news_enrichment: extraction failed for %r: %s", title, exc)
                feed_stats["errors"] += 1
                continue
            if not facts.get("is_data_center_project") or not facts.get("developer"):
                continue

            project = _match_project(session, facts["developer"], facts.get("location_text"))
            if project is None:
                continue  # NEVER creates a project — see module docstring
            feed_stats["matched"] += 1
            stats["matched"] += 1

            published = None
            for key in ("published_parsed", "updated_parsed"):
                t = entry.get(key)
                if t:
                    published = from_unix(calendar.timegm(t))
                    break

            text = scrub(f"{title}\n\n{summary}")
            doc = RawDocument(source=feed_name, source_uid=link, url=link, title=scrub(title),
                              published_at=published, raw_text=text,
                              content_hash=hashlib.sha256(text.encode()).hexdigest())
            session.add(doc)
            session.flush()

            signal = Signal(
                raw_document_id=doc.id, signal_type=SignalType.news_report,
                category=Category.data_center, developer_or_owner=facts["developer"],
                mw_total=facts.get("mw"), summary_one_line=title[:300],
                confidence=0.4,  # matches scoring.signal_certainty's news_report prior
                event_date=published,
            )
            session.add(signal)
            session.flush()

            link_signal_to_project(session, cfg, signal, project, confidence=1.0, method="dc_news_enrichment")
            touched_ids.add(project.id)
            feed_stats["attached"] += 1
            stats["attached"] += 1

        stats["feeds"][feed_name] = feed_stats

    if touched_ids:
        run_size_score(session, cfg, only_project_ids=list(touched_ids))
    else:
        session.commit()
    return stats
