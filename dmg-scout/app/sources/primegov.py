"""PrimeGov (Granicus) public portal — city agendas for non-Legistar jurisdictions.

**Disabled in config, and not because it does not work.**
reno.primegov.com/robots.txt is "User-agent: * / Disallow: /", so the whole host
is off-limits to automated clients. PoliteClient refuses it and we do not work
around robots. Everything below was verified by hand and is left in place so the
adapter is ready if a PrimeGov tenant ever permits crawling — but enabling
`sources.primegov` against Reno only produces RobotsDisallowed.

Built for **City of Reno, NV**. Verified live against reno.primegov.com:

  GET /api/v2/PublicPortal/ListUpcomingMeetings          -> [meeting]
  GET /api/v2/PublicPortal/ListArchivedMeetings?year=Y   -> [meeting]  (187 for 2026)
  GET /Portal/Meeting?meetingTemplateId=<templateId>     -> agenda as HTML

The one non-obvious part, and the thing that cost the most time: the portal wants
`documentList[].templateId`, **not** `documentList[].id`. Passing `id` returns a
200 with the body "The requested meeting document is no longer available", which
looks like a purge rather than a wrong parameter. Each meeting's documentList
carries several templates ("HTML Agenda", "Agenda", "HTML Packet", "Packet",
"Minutes"); we prefer the HTML agenda, since it renders as text rather than PDF.

Unlike CivicPlus this needs no PDF parsing — agendas come back as ~8-10k chars of
HTML per meeting, so it is cheap to screen every meeting in a year.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Iterator

from selectolax.parser import HTMLParser

from app.config import Config
from app.http import PoliteClient
from app.models import SignalType, utcnow
from app.sources.base import (
    FetchedDoc, SourceAdapter, SourceFailure, TargetResult, fanout_verify, keyword_match,
)

log = logging.getLogger(__name__)

# Document templates in preference order: text beats PDF, agenda beats packet.
TEMPLATE_PREFERENCE = ("HTML Agenda", "Agenda", "HTML Packet", "Packet")


def _agenda_text(html: str) -> str:
    tree = HTMLParser(html)
    for sel in ("script", "style", "nav", "header", "footer"):
        for node in tree.css(sel):
            node.decompose()
    return tree.body.text(separator="\n", strip=True) if tree.body else ""


def _pick_template(document_list: list[dict]) -> dict | None:
    for wanted in TEMPLATE_PREFERENCE:
        for doc in document_list or []:
            if doc.get("templateName") == wanted and doc.get("templateId"):
                return doc
    return None


class PrimeGovAdapter(SourceAdapter):
    name = "primegov"

    def _jurisdictions(self, cfg: Config) -> list[dict]:
        return cfg.source(self.name).get("jurisdictions", [])

    def _committee_filter(self, juris: dict) -> list[str]:
        """Substrings of meeting `title` to keep. Empty = every committee."""
        return [c.lower() for c in juris.get("committees", [])]

    def _meetings(self, client: PoliteClient, base: str, year: int) -> list[dict]:
        archived = client.get_json(
            f"{base}/api/v2/PublicPortal/ListArchivedMeetings", params={"year": year})
        meetings = list(archived) if isinstance(archived, list) else []
        if year == utcnow().year:
            upcoming = client.get_json(f"{base}/api/v2/PublicPortal/ListUpcomingMeetings")
            if isinstance(upcoming, list):
                seen = {m.get("id") for m in meetings}
                meetings += [m for m in upcoming if m.get("id") not in seen]
        return meetings

    def _years(self, since: datetime) -> list[int]:
        return list(range(since.year, utcnow().year + 1))

    def backfill_chunks(self, cfg: Config, since: datetime) -> list[dict]:
        return [
            {"key": f"{j['name']}:{year}", "jurisdiction": j["name"], "year": year}
            for j in self._jurisdictions(cfg)
            for year in self._years(since)
        ]

    def fetch_chunk(self, cfg: Config, client: PoliteClient, since: datetime,
                    chunk: dict) -> Iterator[FetchedDoc]:
        juris = next((j for j in self._jurisdictions(cfg)
                      if j["name"] == chunk["jurisdiction"]), None)
        if juris is not None:
            yield from self._pull(cfg, client, juris, chunk["year"], since)[0]

    def fetch(self, cfg: Config, client: PoliteClient,
              since: datetime | None = None) -> Iterator[FetchedDoc]:
        src = cfg.source(self.name)
        if since is None:
            since = utcnow() - timedelta(days=int(src.get("lookback_days", 60)))

        results: list[TargetResult] = []
        for juris in self._jurisdictions(cfg):
            for year in self._years(since):
                try:
                    docs, seen = self._pull(cfg, client, juris, year, since)
                except Exception as exc:  # noqa: BLE001
                    log.warning("primegov %s year %s failed: %s", juris["name"], year, exc)
                    results.append(TargetResult(f"{juris['name']}/{year}", False,
                                                f"{type(exc).__name__}: {exc}"))
                    continue
                results.append(TargetResult(f"{juris['name']}/{year}", True,
                                            records=seen, docs=len(docs)))
                yield from docs

        failed = [r for r in results if not r.ok]
        if results and len(failed) > len(results) / 2:
            raise SourceFailure(
                f"{len(failed)}/{len(results)} PrimeGov jurisdiction-years failed: "
                + ", ".join(f"{r.name} [{r.detail}]" for r in failed[:8]))

    def _pull(self, cfg: Config, client: PoliteClient, juris: dict, year: int,
              since: datetime) -> tuple[list[FetchedDoc], int]:
        base = juris["base_url"].rstrip("/")
        wanted = self._committee_filter(juris)
        meetings = self._meetings(client, base, year)

        docs: list[FetchedDoc] = []
        considered = 0
        for m in meetings:
            title = m.get("title") or ""
            if wanted and not any(w in title.lower() for w in wanted):
                continue
            when = None
            for key in ("dateTime", "date"):
                raw = m.get(key)
                if not raw:
                    continue
                for fmt in ("%Y-%m-%dT%H:%M:%S", "%b %d, %Y"):
                    try:
                        when = datetime.strptime(raw, fmt)
                        break
                    except ValueError:
                        continue
                if when:
                    break
            if when is None or when < since:
                continue
            template = _pick_template(m.get("documentList"))
            if template is None:
                continue
            considered += 1
            url = f"{base}/Portal/Meeting?meetingTemplateId={template['templateId']}"
            try:
                text = _agenda_text(client.get_text(url))
            except Exception as exc:  # noqa: BLE001
                log.warning("primegov agenda %s failed: %s", url, exc)
                continue
            if not text.strip():
                log.warning("primegov agenda %s returned no text", url)
                continue
            header = (
                f"{juris['name']} — {title}\n"
                f"Meeting date: {when:%Y-%m-%d}\n"
                f"Document: {template.get('templateName')}\n"
                f"Source: {url}\n"
            )
            if not keyword_match(header + text, cfg):
                continue
            docs.append(FetchedDoc(
                source=self.name,
                source_uid=f"{base.split('//')[-1]}:{m.get('id')}:{template['templateId']}",
                url=url,
                title=f"[{juris['name']}] {title} {when:%Y-%m-%d}"[:250],
                raw_text=header + "\n" + text,
                published_at=when,
                meta={"jurisdiction": juris["name"], "county": juris.get("county"),
                      "state": juris.get("state"), "body": title,
                      "document_kind": template.get("templateName"),
                      "platform": "primegov", "meeting_id": m.get("id"),
                      "location": m.get("location")},
                default_signal_type=SignalType.planning_agenda,
            ))
        return docs, considered

    def verify(self, cfg: Config, client: PoliteClient) -> dict:
        """List this year's meetings per jurisdiction. Counts meetings that have
        a usable agenda template — a portal that lists meetings but exposes no
        templates would otherwise look healthy while yielding nothing."""
        results: list[TargetResult] = []
        year = utcnow().year
        for juris in self._jurisdictions(cfg):
            base = juris["base_url"].rstrip("/")
            wanted = self._committee_filter(juris)
            try:
                meetings = self._meetings(client, base, year)
            except Exception as exc:  # noqa: BLE001
                results.append(TargetResult(juris["name"], False,
                                            f"{type(exc).__name__}: {exc}"))
                continue
            matching = [m for m in meetings
                        if not wanted or any(w in (m.get("title") or "").lower()
                                             for w in wanted)]
            with_docs = [m for m in matching if _pick_template(m.get("documentList"))]
            detail = "" if with_docs else "meetings listed but no agenda templates"
            results.append(TargetResult(juris["name"], bool(with_docs), detail,
                                        records=len(matching), docs=len(with_docs)))
        return fanout_verify("jurisdictions", results)
