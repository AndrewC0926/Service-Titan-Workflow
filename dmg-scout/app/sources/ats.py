"""Public ATS job boards — Greenhouse, Lever, Ashby JSON APIs. Never LinkedIn.

A geo-specific data center hire by a developer, GC, or MEP firm is a 3-9 month
leading indicator of contracted work. Endpoints:
  Greenhouse: https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true
  Lever:      https://api.lever.co/v0/postings/{company}?mode=json
  Ashby:      https://api.ashbyhq.com/posting-api/job-board/{name}?includeCompensation=false
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Iterator

from app.config import Config
from app.http import PoliteClient
from app.models import SignalType
from app.sources.base import FetchedDoc, SourceAdapter, SourceFailure

log = logging.getLogger(__name__)


def _geo_hit(text: str, geo_terms: list[str]) -> str | None:
    lower = text.lower()
    for term in geo_terms:
        if term.lower() in lower:
            return term
    return None


class AtsAdapter(SourceAdapter):
    name = "ats"

    def fetch(self, cfg: Config, client: PoliteClient,
              since=None) -> Iterator[FetchedDoc]:
        # Job boards only expose current postings; `since` is ignored.
        src = cfg.source(self.name)
        geo_terms = src.get("geo_terms", [])
        companies = []
        for group in ("developers", "gcs", "mep_firms"):
            for c in cfg.get(f"companies.{group}", []):
                companies.append((c, group))

        self._attempts = self._failures = 0
        for company, group in companies:
            cname = company["name"]
            if company.get("greenhouse"):
                yield from self._greenhouse(client, company["greenhouse"], cname, group, geo_terms)
            if company.get("lever"):
                yield from self._lever(client, company["lever"], cname, group, geo_terms)
            if company.get("ashby"):
                yield from self._ashby(client, company["ashby"], cname, group, geo_terms)
        if self._attempts and self._failures == self._attempts:
            raise SourceFailure(f"all {self._attempts} ATS boards failed")

    def _emit(self, cname: str, group: str, uid: str, url: str, title: str,
              location: str, body: str, geo: str, published: datetime | None) -> FetchedDoc:
        text = (
            f"Job posting by {cname} ({group}).\nTitle: {title}\nLocation: {location}\n"
            f"Geo match: {geo}\n\n{body[:4000]}"
        )
        return FetchedDoc(
            source=self.name,
            source_uid=uid,
            url=url,
            title=f"[{cname}] {title} — {location}"[:250],
            raw_text=text,
            published_at=published,
            meta={"company": cname, "company_group": group, "location": location, "geo_match": geo},
            default_signal_type=SignalType.job_posting,
        )

    def _greenhouse(self, client: PoliteClient, token: str, cname: str, group: str,
                    geo_terms: list[str]) -> Iterator[FetchedDoc]:
        url = f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true"
        self._attempts += 1
        try:
            data = client.get_json(url)
        except Exception as exc:  # noqa: BLE001
            log.warning("greenhouse board %s failed: %s", token, exc)
            self._failures += 1
            return
        for job in data.get("jobs", []):
            loc = (job.get("location") or {}).get("name", "")
            title = job.get("title", "")
            content = job.get("content", "")
            geo = _geo_hit(f"{loc} {title} {content}", geo_terms)
            if not geo:
                continue
            published = None
            try:
                published = datetime.fromisoformat(job.get("updated_at", "").split("+")[0].rstrip("Z"))
            except ValueError:
                pass
            yield self._emit(cname, group, f"greenhouse:{token}:{job.get('id')}",
                             job.get("absolute_url", url), title, loc, content, geo, published)

    def _lever(self, client: PoliteClient, slug: str, cname: str, group: str,
               geo_terms: list[str]) -> Iterator[FetchedDoc]:
        url = f"https://api.lever.co/v0/postings/{slug}?mode=json"
        self._attempts += 1
        try:
            data = client.get_json(url)
        except Exception as exc:  # noqa: BLE001
            log.warning("lever board %s failed: %s", slug, exc)
            self._failures += 1
            return
        if not isinstance(data, list):
            return
        for job in data:
            loc = (job.get("categories") or {}).get("location", "") or ""
            title = job.get("text", "")
            body = job.get("descriptionPlain", "") or ""
            geo = _geo_hit(f"{loc} {title} {body}", geo_terms)
            if not geo:
                continue
            published = None
            ts = job.get("createdAt")
            if isinstance(ts, (int, float)):
                published = datetime.utcfromtimestamp(ts / 1000.0)
            yield self._emit(cname, group, f"lever:{slug}:{job.get('id')}",
                             job.get("hostedUrl", url), title, loc, body, geo, published)

    def _ashby(self, client: PoliteClient, board: str, cname: str, group: str,
               geo_terms: list[str]) -> Iterator[FetchedDoc]:
        url = f"https://api.ashbyhq.com/posting-api/job-board/{board}?includeCompensation=false"
        self._attempts += 1
        try:
            data = client.get_json(url)
        except Exception as exc:  # noqa: BLE001
            log.warning("ashby board %s failed: %s", board, exc)
            self._failures += 1
            return
        for job in data.get("jobs", []):
            loc = job.get("location", "") or ""
            title = job.get("title", "")
            body = job.get("descriptionPlain", "") or ""
            geo = _geo_hit(f"{loc} {title} {body}", geo_terms)
            if not geo:
                continue
            yield self._emit(cname, group, f"ashby:{board}:{job.get('id')}",
                             job.get("jobUrl", url), title, loc, body, geo, None)
