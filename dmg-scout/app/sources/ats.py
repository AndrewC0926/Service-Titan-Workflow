"""Public ATS job boards — Greenhouse, Lever, Ashby, Workday JSON APIs. Never LinkedIn.

A geo-specific data center hire by a developer, GC, or MEP firm is a 3-9 month
leading indicator of contracted work. Endpoints:
  Greenhouse: https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true
  Lever:      https://api.lever.co/v0/postings/{company}?mode=json
  Ashby:      https://api.ashbyhq.com/posting-api/job-board/{name}?includeCompensation=false
  Workday:    POST https://{host}/wday/cxs/{tenant}/{site}/jobs   (paginated)

Board identifiers are opaque vendor strings, not company names, and guessing
them produces two distinct failure modes — both of which we hit:

* **404** — no such board. `vantagedatacenters`, `cyrusone`, `aligneddatacenters`
  (Greenhouse) and `tract` (Lever) were all invented and all 404'd.
* **A different company's board.** Far worse, because it silently poisons the
  pipeline with irrelevant postings. `greenhouse/aligned` is alignedup.com, a
  sales-enablement SaaS; `ashby/vantage` is vantage.sh, a cloud-cost tool.
  Neither is a data center operator. Always confirm a candidate board's job
  titles and apply URLs before adding it.

Every identifier in config.yaml was confirmed live against the vendor API and
checked to belong to the right company.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Iterator

from selectolax.parser import HTMLParser

from app.config import Config
from app.http import PoliteClient
from app.models import SignalType
from app.sources.base import (
    FetchedDoc, SourceAdapter, SourceFailure, TargetResult, fanout_verify,
)

log = logging.getLogger(__name__)

WORKDAY_PAGE = 20   # Workday's own page size; larger values are ignored


def _geo_hit(text: str, geo_terms: list[str]) -> str | None:
    lower = text.lower()
    for term in geo_terms:
        if term.lower() in lower:
            return term
    return None


def _strip_html(html: str) -> str:
    if not html:
        return ""
    tree = HTMLParser(html)
    return tree.text(separator="\n", strip=True) if tree.body else html


class AtsAdapter(SourceAdapter):
    name = "ats"

    def _companies(self, cfg: Config) -> list[tuple[dict, str]]:
        out = []
        for group in ("developers", "gcs", "mep_firms"):
            for c in cfg.get(f"companies.{group}", []):
                out.append((c, group))
        return out

    def _probe_all(self, cfg: Config, client: PoliteClient
                   ) -> Iterator[tuple[TargetResult, list[FetchedDoc]]]:
        """Walk every configured board, yielding (health, docs) per board."""
        src = cfg.source(self.name)
        geo_terms = src.get("geo_terms", [])
        backends = (
            ("greenhouse", self._greenhouse),
            ("lever", self._lever),
            ("ashby", self._ashby),
            ("workday", self._workday),
        )
        for company, group in self._companies(cfg):
            cname = company["name"]
            for key, fn in backends:
                board = company.get(key)
                if not board:
                    continue
                label = f"{key}:{board if isinstance(board, str) else board.get('site', cname)}"
                try:
                    records, docs = fn(client, board, cname, group, geo_terms)
                except Exception as exc:  # noqa: BLE001 — one board must not kill the run
                    log.warning("%s board for %s failed: %s", key, cname, exc)
                    yield TargetResult(label, False, f"{type(exc).__name__}: {exc}"), []
                else:
                    yield TargetResult(label, True, records=records, docs=len(docs)), docs

    def fetch(self, cfg: Config, client: PoliteClient,
              since=None) -> Iterator[FetchedDoc]:
        # Job boards only expose current postings; `since` is ignored.
        results: list[TargetResult] = []
        for result, docs in self._probe_all(cfg, client):
            results.append(result)
            yield from docs
        failed = [r for r in results if not r.ok]
        if results and len(failed) > len(results) / 2:
            raise SourceFailure(
                f"{len(failed)}/{len(results)} ATS boards failed: "
                + ", ".join(f"{r.name} [{r.detail}]" for r in failed))

    def verify(self, cfg: Config, client: PoliteClient) -> dict:
        results = [r for r, _ in self._probe_all(cfg, client)]
        return fanout_verify("boards", results)

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

    # ---- backends: each returns (records_seen, matching_docs) --------------

    def _greenhouse(self, client: PoliteClient, token: str, cname: str, group: str,
                    geo_terms: list[str]) -> tuple[int, list[FetchedDoc]]:
        url = f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true"
        jobs = client.get_json(url).get("jobs", [])
        docs = []
        for job in jobs:
            loc = (job.get("location") or {}).get("name", "")
            title = job.get("title", "")
            content = job.get("content", "")
            geo = _geo_hit(f"{loc} {title} {content}", geo_terms)
            if not geo:
                continue
            published = None
            try:
                published = datetime.fromisoformat(
                    job.get("updated_at", "").split("+")[0].rstrip("Z"))
            except ValueError:
                pass
            docs.append(self._emit(cname, group, f"greenhouse:{token}:{job.get('id')}",
                                   job.get("absolute_url", url), title, loc, content,
                                   geo, published))
        return len(jobs), docs

    def _lever(self, client: PoliteClient, slug: str, cname: str, group: str,
               geo_terms: list[str]) -> tuple[int, list[FetchedDoc]]:
        url = f"https://api.lever.co/v0/postings/{slug}?mode=json"
        data = client.get_json(url)
        if not isinstance(data, list):
            return 0, []
        docs = []
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
            docs.append(self._emit(cname, group, f"lever:{slug}:{job.get('id')}",
                                   job.get("hostedUrl", url), title, loc, body, geo, published))
        return len(data), docs

    def _ashby(self, client: PoliteClient, board: str, cname: str, group: str,
               geo_terms: list[str]) -> tuple[int, list[FetchedDoc]]:
        url = f"https://api.ashbyhq.com/posting-api/job-board/{board}?includeCompensation=false"
        jobs = client.get_json(url).get("jobs", [])
        docs = []
        for job in jobs:
            loc = job.get("location", "") or ""
            title = job.get("title", "")
            body = job.get("descriptionPlain", "") or ""
            geo = _geo_hit(f"{loc} {title} {body}", geo_terms)
            if not geo:
                continue
            docs.append(self._emit(cname, group, f"ashby:{board}:{job.get('id')}",
                                   job.get("jobUrl", url), title, loc, body, geo, None))
        return len(jobs), docs

    def _workday(self, client: PoliteClient, board: dict, cname: str, group: str,
                 geo_terms: list[str]) -> tuple[int, list[FetchedDoc]]:
        """Workday's board API is POST-only and paginated. The listing carries
        title + location but no description, so we geo-match on the listing and
        fetch the description only for the postings that already matched."""
        host = board["host"]
        site = board["site"]
        tenant = board.get("tenant") or host.split(".")[0]
        api = f"https://{host}/wday/cxs/{tenant}/{site}"

        postings: list[dict] = []
        offset = 0
        while True:
            page = client.post_json(
                f"{api}/jobs",
                json={"appliedFacets": {}, "limit": WORKDAY_PAGE, "offset": offset,
                      "searchText": ""},
                headers={"Accept": "application/json"},
            )
            batch = page.get("jobPostings") or []
            postings.extend(batch)
            offset += WORKDAY_PAGE
            total = page.get("total") or 0
            if not batch or offset >= total or offset >= int(board.get("max_postings", 400)):
                break

        docs = []
        for job in postings:
            loc = job.get("locationsText", "") or ""
            title = job.get("title", "") or ""
            geo = _geo_hit(f"{loc} {title}", geo_terms)
            if not geo:
                continue
            path = job.get("externalPath") or ""
            url = f"https://{host}/{site}{path}"
            body = ""
            published = None
            try:
                info = client.get_json(f"{api}{path}",
                                       headers={"Accept": "application/json"}
                                       ).get("jobPostingInfo", {})
            except Exception as exc:  # noqa: BLE001 — listing row already stands alone
                log.warning("workday detail fetch failed for %s: %s", url, exc)
            else:
                body = _strip_html(info.get("jobDescription") or "")
                url = info.get("externalUrl") or url
                loc = info.get("location") or loc
                try:
                    published = datetime.fromisoformat(info.get("startDate", ""))
                except ValueError:
                    pass
            # bulletFields normally carries the requisition ID (e.g. "R24110"),
            # but it can be absent or empty — fall back to the unique path.
            bullets = job.get("bulletFields") or []
            req_id = bullets[0] if bullets else path
            docs.append(self._emit(cname, group, f"workday:{tenant}:{req_id}",
                                   url, title, loc, body, geo, published))
        return len(postings), docs
