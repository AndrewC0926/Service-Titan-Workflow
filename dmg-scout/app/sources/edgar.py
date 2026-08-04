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

    def fetch(self, cfg: Config, client: PoliteClient) -> Iterator[FetchedDoc]:
        src = cfg.source(self.name)
        base = src.get("base_url", "https://efts.sec.gov/LATEST/search-index")
        forms = ",".join(src.get("forms", []))
        lookback = int(src.get("lookback_days", 14))
        startdt = (datetime.utcnow() - timedelta(days=lookback)).strftime("%Y-%m-%d")
        enddt = datetime.utcnow().strftime("%Y-%m-%d")

        queries = list(src.get("queries", []))
        # Also watch named developers from the companies section.
        for group in ("developers",):
            for company in cfg.get(f"companies.{group}", []):
                for term in company.get("edgar_terms", []):
                    queries.append(f'"{term}"')

        failures = 0
        for q in queries:
            params = {"q": q, "dateRange": "custom", "startdt": startdt, "enddt": enddt}
            if forms:
                params["forms"] = forms
            try:
                data = client.get(base, params=params).json()
            except Exception as exc:  # noqa: BLE001
                log.warning("edgar query %r failed: %s", q, exc)
                failures += 1
                continue
            for hit in data.get("hits", {}).get("hits", []):
                yield self._hit_to_doc(hit, q)
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
        text = (
            f"SEC EDGAR filing.\nForm: {form}\nFiled: {filed}\nEntities: {'; '.join(names)}\n"
            f"Matched query: {query}\nDocument: {url}"
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
