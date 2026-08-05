"""SEC EDGAR full-text search (efts.sec.gov/LATEST/search-index) — JSON API.

Purpose is narrow: **data center ABS/CMBS deal documents that name specific
campuses, tenants, MW and delivery dates**, plus filings by the developers in
`companies.developers`. It is not a general "mentions data center" firehose.

The first backfill got this badly wrong — 3,016 rows averaging 202 characters,
mostly chip and software companies whose 10-K risk factors say "data center".
Two independent defects, both fixed here:

1. **It never fetched the filing.** It stored the search-result stub (form,
   date, entity, a URL it never followed) and deferred the body to extract time,
   which meant *triage* had to judge relevance from ~200 characters of metadata.
   Now the document is fetched and converted to text at fetch time, so triage
   sees the real filing and `app.sections` chunks it the same way it chunks EIRs.

2. **The query was far too broad.** Precision does not come from better search
   terms — it comes from filtering on **who filed**. Verified on a 468-hit live
   sample: 93 kept, 375 dropped. The gate separates

     kept:    "Vantage Data Centers Holdings, LLC" files an ABS-15G
     dropped: "JPMORGAN CHASE & CO" files a 424B2 that mentions STACK

   so `issuer_patterns` (matched against the filer's display name) is the real
   precision control, and broad keyword queries are gone. Entity and deal-phrase
   queries only, on forms that actually carry deal terms.

Also: one filing yields many full-text hits (one per document/exhibit), so hits
are deduped by accession number — otherwise a single ABS-15G becomes a dozen rows.

SEC fair-access policy: identify yourself in the User-Agent and stay well under
10 req/s (our 2s/domain throttle is far below).
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta
from typing import Iterator

from selectolax.parser import HTMLParser

from app.config import Config
from app.http import PoliteClient
from app.models import SignalType
from app.sources.base import FetchedDoc, SourceAdapter, SourceFailure

log = logging.getLogger(__name__)

_WS = re.compile(r"[ \t]{2,}")
_BLANKS = re.compile(r"\n{3,}")


def filing_to_text(body: bytes, content_type: str = "") -> str:
    """SEC documents are .htm, .txt or raw SGML. Reduce any of them to text."""
    try:
        raw = body.decode("utf-8")
    except UnicodeDecodeError:
        raw = body.decode("latin-1", errors="replace")
    looks_html = "html" in content_type.lower() or "<" in raw[:2000]
    if looks_html:
        tree = HTMLParser(raw)
        for sel in ("script", "style"):
            for node in tree.css(sel):
                node.decompose()
        raw = tree.body.text(separator="\n", strip=True) if tree.body else raw
    raw = _WS.sub(" ", raw)
    return _BLANKS.sub("\n\n", raw).strip()


class EdgarAdapter(SourceAdapter):
    name = "edgar"

    # ---- query construction ----------------------------------------------

    def _query_plan(self, cfg: Config) -> list[tuple[str, str]]:
        """(query, forms) pairs.

        Deal-document forms carry the campus/tenant/MW detail; developer forms
        are where a named developer announces a project. Keeping them separate
        stops '8-K' from dragging in every crypto miner that says "data center".
        """
        src = cfg.source(self.name)
        deal_forms = ",".join(src.get("forms", []))
        dev_forms = ",".join(src.get("developer_forms", []))
        plan: list[tuple[str, str]] = []

        for phrase in src.get("deal_phrases", []):
            plan.append((f'"{phrase}"', deal_forms))
        for entity in src.get("issuer_queries", []):
            plan.append((f'"{entity}"', f"{deal_forms},{dev_forms}".strip(",")))
        # Developers named in the companies section, on developer forms.
        for company in cfg.get("companies.developers", []):
            for term in company.get("edgar_terms", []):
                plan.append((f'"{term}"', dev_forms))

        # issuer_queries and companies.developers.edgar_terms overlap heavily;
        # dedupe so we do not spend two requests on the same search.
        seen: set[tuple[str, str]] = set()
        return [p for p in plan if not (p in seen or seen.add(p))]

    def _issuer_patterns(self, cfg: Config) -> list[str]:
        return [p.lower() for p in cfg.source(self.name).get("issuer_patterns", [])]

    def _filer_is_relevant(self, names: list[str], patterns: list[str]) -> bool:
        joined = " ; ".join(names).lower()
        return any(p in joined for p in patterns)

    # ---- backfill --------------------------------------------------------

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

    # ---- fetching --------------------------------------------------------

    def _search(self, client: PoliteClient, base: str, q: str, forms: str,
                startdt: str, enddt: str, page: int) -> list[dict]:
        params = {"q": q, "dateRange": "custom", "startdt": startdt, "enddt": enddt}
        if forms:
            params["forms"] = forms
        if page:
            params["from"] = page * 10
        data = client.get(base, params=params).json()
        return data.get("hits", {}).get("hits", [])

    def _fetch_range(self, cfg: Config, client: PoliteClient,
                     startdt: str, enddt: str) -> Iterator[FetchedDoc]:
        src = cfg.source(self.name)
        base = src.get("base_url", "https://efts.sec.gov/LATEST/search-index")
        max_pages = int(src.get("max_pages", 3))  # FTS pages are 10 hits each
        patterns = self._issuer_patterns(cfg)
        plan = self._query_plan(cfg)

        seen_accessions: set[str] = set()
        attempts = failures = 0
        kept = skipped = 0

        for q, forms in plan:
            attempts += 1
            for page in range(max_pages):
                try:
                    hits = self._search(client, base, q, forms, startdt, enddt, page)
                except Exception as exc:  # noqa: BLE001
                    log.warning("edgar query %r failed: %s", q, exc)
                    if page == 0:
                        failures += 1
                    break
                for hit in hits:
                    source = hit.get("_source", {})
                    names = source.get("display_names") or []
                    if not self._filer_is_relevant(names, patterns):
                        skipped += 1
                        continue
                    accession = (hit.get("_id", "") or "").partition(":")[0]
                    if accession in seen_accessions:
                        continue      # one filing, many full-text hits
                    seen_accessions.add(accession)
                    doc = self._hit_to_doc(cfg, client, hit, q)
                    if doc is not None:
                        kept += 1
                        yield doc
                if len(hits) < 10:
                    break

        log.info("edgar %s..%s: %d filings kept, %d hits dropped on filer, %d queries",
                 startdt, enddt, kept, skipped, attempts)
        if attempts and failures == attempts:
            raise SourceFailure(f"all {failures} EDGAR full-text queries failed")

    def _hit_to_doc(self, cfg: Config, client: PoliteClient, hit: dict,
                    query: str) -> FetchedDoc | None:
        src = cfg.source(self.name)
        max_body = int(src.get("max_body_chars", 400_000))

        src_data = hit.get("_source", {})
        _id = hit.get("_id", "")  # "accession:filename"
        accession, _, filename = _id.partition(":")
        acc_nodash = accession.replace("-", "")
        ciks = src_data.get("ciks", [])
        cik = ciks[0].lstrip("0") if ciks else ""
        if not (cik and filename):
            log.warning("edgar hit %r has no resolvable document URL", _id)
            return None
        url = f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc_nodash}/{filename}"

        names = src_data.get("display_names", [])
        form = src_data.get("root_forms", [""])[0] or src_data.get("file_type", "")
        filed = src_data.get("file_date", "")
        published = None
        try:
            published = datetime.strptime(filed, "%Y-%m-%d")
        except ValueError:
            pass

        header = (
            f"SEC EDGAR filing.\nForm: {form}\nFiled: {filed}\n"
            f"Entities: {'; '.join(names)}\nDocument: {url}\n"
        )

        # Fetch the actual filing. A stub row is worse than no row: triage cannot
        # tell a real deal document from a passing mention, so if the body cannot
        # be retrieved we flag it for the extract-stage retry instead of pretending.
        body = ""
        body_error = None
        try:
            resp = client.get(url)
            body = filing_to_text(resp.content,
                                  resp.headers.get("Content-Type", ""))[:max_body]
        except Exception as exc:  # noqa: BLE001
            body_error = f"{type(exc).__name__}: {exc}"
            log.warning("edgar body fetch failed for %s: %s", url, body_error)

        text = f"{header}\n{body}" if body else header
        return FetchedDoc(
            source=self.name,
            source_uid=accession or _id,
            url=url,
            title=(f"{form} — {'; '.join(names)}" if names
                   else f"{form} filing {accession}")[:250],
            raw_text=text,
            published_at=published,
            meta={"form": form, "query": query, "accession": accession,
                  "filer_names": names, "body_chars": len(body),
                  # Only true when the body is still missing, so the extract
                  # stage retries exactly the failures.
                  "needs_body_fetch": not body,
                  **({"body_error": body_error} if body_error else {})},
            default_signal_type=(SignalType.abs_issuance
                                 if form.startswith(("424", "ABS", "10-D"))
                                 else SignalType.news_report),
        )

    def verify(self, cfg: Config, client: PoliteClient) -> dict:
        """A stub-length guard lives here: EDGAR's whole failure mode was storing
        200-char metadata rows, so verify reports the body size it actually got."""
        try:
            doc = next(iter(self.fetch(cfg, client)), None)
        except Exception as exc:  # noqa: BLE001
            return {"status": "fail", "detail": f"{type(exc).__name__}: {exc}"}
        if doc is None:
            return {"status": "warn",
                    "detail": "reachable, but no filings matched (narrow by design)"}
        body_chars = doc.meta.get("body_chars", 0)
        detail = (f"first filing: {doc.title[:60]!r} — {body_chars} body chars "
                  f"({len(doc.raw_text)} total) {doc.url}")
        if body_chars < int(cfg.source(self.name).get("min_body_chars", 2000)):
            return {"status": "fail",
                    "detail": "storing stubs, not filings: " + detail}
        return {"status": "ok", "detail": detail}
