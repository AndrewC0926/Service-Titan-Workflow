"""SEC EDGAR full-text search (efts.sec.gov/LATEST/search-index) — JSON API.

Data center ABS/CMBS deals and 8-Ks name campuses, tenants, MW, and delivery
dates. SEC fair-access policy: identify yourself in the User-Agent, stay well
under 10 req/s (our 2s/domain throttle is far below).
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Iterator

from app.config import Config
from app.http import PoliteClient
from app.models import SignalType
from app.sources.base import FetchedDoc, SourceAdapter, SourceFailure

log = logging.getLogger(__name__)


class EdgarAdapter(SourceAdapter):
    name = "edgar"

    def _queries(self, cfg: Config) -> list[str]:
        src = cfg.source(self.name)
        queries = list(src.get("queries", []))
        # Also watch named developers from the companies section.
        for group in ("developers",):
            for company in cfg.get(f"companies.{group}", []):
                for term in company.get("edgar_terms", []):
                    queries.append(f'"{term}"')
        return queries

    def backfill_chunks(self, cfg: Config, since: datetime) -> list[dict]:
        """One chunk per calendar month from `since` to now."""
        chunks = []
        cursor = since.replace(day=1)
        now = datetime.utcnow()
        while cursor <= now:
            nxt = (cursor.replace(day=28) + timedelta(days=4)).replace(day=1)
            chunks.append({
                "key": f"{since:%Y-%m-%d}:month:{cursor:%Y-%m}",
                "startdt": max(cursor, since).strftime("%Y-%m-%d"),
                "enddt": min(nxt - timedelta(days=1), now).strftime("%Y-%m-%d"),
            })
            cursor = nxt
        return chunks

    def fetch_chunk(self, cfg: Config, client: PoliteClient, since: datetime,
                    chunk: dict) -> Iterator[FetchedDoc]:
        yield from self._fetch_range(cfg, client, chunk["startdt"], chunk["enddt"])

    def fetch(self, cfg: Config, client: PoliteClient,
              since: datetime | None = None) -> Iterator[FetchedDoc]:
        src = cfg.source(self.name)
        if since is None:
            since = datetime.utcnow() - timedelta(days=int(src.get("lookback_days", 14)))
        yield from self._fetch_range(cfg, client, since.strftime("%Y-%m-%d"),
                                     datetime.utcnow().strftime("%Y-%m-%d"))

    def _fetch_range(self, cfg: Config, client: PoliteClient,
                     startdt: str, enddt: str) -> Iterator[FetchedDoc]:
        src = cfg.source(self.name)
        base = src.get("base_url", "https://efts.sec.gov/LATEST/search-index")
        forms = ",".join(src.get("forms", []))
        max_pages = int(src.get("max_pages", 5))  # FTS pages are 10 hits each
        queries = self._queries(cfg)

        failures = 0
        for q in queries:
            for page in range(max_pages):
                params = {"q": q, "dateRange": "custom", "startdt": startdt, "enddt": enddt}
                if forms:
                    params["forms"] = forms
                if page:
                    params["from"] = page * 10
                try:
                    data = client.get(base, params=params).json()
                except Exception as exc:  # noqa: BLE001
                    log.warning("edgar query %r failed: %s", q, exc)
                    if page == 0:
                        failures += 1
                    break
                hits = data.get("hits", {}).get("hits", [])
                for hit in hits:
                    yield self._hit_to_doc(hit, q)
                if len(hits) < 10:
                    break
        if queries and failures == len(queries):
            raise SourceFailure(f"all {failures} EDGAR full-text queries failed")

    def _hit_to_doc(self, hit: dict, query: str) -> FetchedDoc:
        src_data = hit.get("_source", {})
        _id = hit.get("_id", "")  # "accession:filename"
        accession, _, filename = _id.partition(":")
        acc_nodash = accession.replace("-", "")
        ciks = src_data.get("ciks", [])
        cik = ciks[0].lstrip("0") if ciks else ""
        url = (
            f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc_nodash}/{filename}"
            if cik and filename else f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany"
        )
        names = src_data.get("display_names", [])
        form = src_data.get("root_forms", [""])[0] or src_data.get("file_type", "")
        filed = src_data.get("file_date", "")
        published = None
        try:
            published = datetime.strptime(filed, "%Y-%m-%d")
        except ValueError:
            pass
        title = f"{form} — {'; '.join(names)}" if names else f"{form} filing {accession}"
        sig = SignalType.abs_issuance if form.startswith("424") else SignalType.news_report
        # Full-text hits don't include the document body; store the metadata summary.
        # The extract stage fetches url for the full text when triage passes.
        # NOTE: the matched query must NOT go into raw_text — the same filing hit by
        # different queries would hash differently and defeat dedupe. It lives in meta.
        text = (
            f"SEC EDGAR filing.\nForm: {form}\nFiled: {filed}\nEntities: {'; '.join(names)}\n"
            f"Document: {url}"
        )
        return FetchedDoc(
            source=self.name,
            source_uid=_id or accession,
            url=url,
            title=title,
            raw_text=text,
            published_at=published,
            meta={"form": form, "query": query, "accession": accession,
                  "needs_body_fetch": True},
            default_signal_type=sig,
        )
