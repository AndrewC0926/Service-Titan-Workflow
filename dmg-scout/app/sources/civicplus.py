"""CivicPlus AgendaCenter — county/city agendas for jurisdictions with no Legistar.

Built for **Storey County, NV**, which is the Tahoe Reno Industrial Center
(Vantage, Tract, Google, Switch). Nevada has no CEQA equivalent, so a county
that publishes only through CivicPlus is otherwise invisible to this system —
and the backfill made the stakes plain: 25 months of CEQAnet across seven
Southern California counties yielded 16 documents, while one Nevada agency
(GOED) yielded 75.

Endpoints, verified live against storeycounty.org:

  GET  /AgendaCenter                      current year, all categories
  POST /AgendaCenter/UpdateCategoryList   {year, catID} -> HTML fragment of rows
  GET  /AgendaCenter/ViewFile/Agenda/_MMDDYYYY-<docid>   -> application/pdf

The AJAX fragment is the only way to reach prior years (the page's year links
call it via `changeYear(year, catID)`), and it returns the same
`<tr class="catAgendaRow">` markup as the full page, so one parser serves both.
robots.txt disallows /admin, /search.aspx and friends — /AgendaCenter is allowed.

Cost control matters here: a single Board of Commissioners packet is 268 pages
and 16 MB. We extract only the first `max_pdf_pages` (the agenda item list lives
up front), keyword-screen that, and keep the document only if it mentions data
centers. Everything else is downloaded, screened, and discarded.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta
from typing import Iterator

from app.config import Config
from app.http import PoliteClient
from app.models import SignalType, utcnow
from app.pdftext import pdf_to_text
from app.sources.base import (
    FetchedDoc, SourceAdapter, SourceFailure, TargetResult, fanout_verify, keyword_match,
)

log = logging.getLogger(__name__)

# <tr ... class="catAgendaRow"> ... </tr>
_ROW = re.compile(r"<tr[^>]*class=\"[^\"]*catAgendaRow[^\"]*\"[^>]*>.*?</tr>", re.S | re.I)
# /AgendaCenter/ViewFile/<kind>/_MMDDYYYY-<docid>
_VIEWFILE = re.compile(
    r"href=\"(?P<href>/AgendaCenter/ViewFile/(?P<kind>\w+)/_(?P<mmddyyyy>\d{8})-(?P<docid>\d+))\"",
    re.I)
_TAGS = re.compile(r"<[^>]+>")


def _clean(html: str) -> str:
    return re.sub(r"\s+", " ", _TAGS.sub(" ", html)).strip()


def _row_title(row_html: str, href: str) -> str:
    """Anchor text for this ViewFile link, else any <strong> date label."""
    m = re.search(re.escape(href) + r"\"[^>]*>(?P<txt>.*?)</a>", row_html, re.S | re.I)
    if m and _clean(m.group("txt")):
        return _clean(m.group("txt"))
    strong = re.search(r"<strong[^>]*>(.*?)</strong>", row_html, re.S | re.I)
    return _clean(strong.group(1)) if strong else "agenda"


class CivicPlusAdapter(SourceAdapter):
    name = "civicplus"

    # ---- config ----------------------------------------------------------

    def _jurisdictions(self, cfg: Config) -> list[dict]:
        return cfg.source(self.name).get("jurisdictions", [])

    def _kinds(self, cfg: Config) -> set[str]:
        kinds = cfg.source(self.name).get("document_kinds") or ["Agenda"]
        return {k.strip().lower() for k in kinds}

    # ---- fetching --------------------------------------------------------

    def _category_html(self, client: PoliteClient, base: str, cat_id: int,
                       year: int) -> str:
        """One category+year as an HTML fragment of catAgendaRow rows."""
        return client.post(
            f"{base}/AgendaCenter/UpdateCategoryList",
            data={"year": str(year), "catID": str(cat_id)},
            headers={"X-Requested-With": "XMLHttpRequest"},
        ).text

    def _rows(self, html: str, kinds: set[str]) -> list[dict]:
        """Every wanted document link in a category fragment, newest first."""
        out: list[dict] = []
        seen: set[str] = set()
        for row_match in _ROW.finditer(html):
            row = row_match.group(0)
            for m in _VIEWFILE.finditer(row):
                kind = m.group("kind")
                if kind.lower() not in kinds:
                    continue
                href, docid = m.group("href"), m.group("docid")
                uid = f"{kind}/_{m.group('mmddyyyy')}-{docid}"
                if uid in seen:
                    continue          # each row lists the link twice
                seen.add(uid)
                try:
                    meeting_date = datetime.strptime(m.group("mmddyyyy"), "%m%d%Y")
                except ValueError:
                    continue
                out.append({"href": href, "kind": kind, "docid": docid, "uid": uid,
                            "date": meeting_date, "title": _row_title(row, href)})
        return out

    def _years(self, since: datetime) -> list[int]:
        return list(range(since.year, utcnow().year + 1))

    def backfill_chunks(self, cfg: Config, since: datetime) -> list[dict]:
        """One chunk per jurisdiction per category per year."""
        return [
            {"key": f"{j['name']}:{cat['id']}:{year}", "jurisdiction": j["name"],
             "cat_id": cat["id"], "year": year}
            for j in self._jurisdictions(cfg)
            for cat in j.get("categories", [])
            for year in self._years(since)
        ]

    def fetch_chunk(self, cfg: Config, client: PoliteClient, since: datetime,
                    chunk: dict) -> Iterator[FetchedDoc]:
        juris = next((j for j in self._jurisdictions(cfg)
                      if j["name"] == chunk["jurisdiction"]), None)
        if juris is None:
            return
        cat = next((c for c in juris.get("categories", [])
                    if c["id"] == chunk["cat_id"]), {"id": chunk["cat_id"], "label": "?"})
        docs, _ = self._pull(cfg, client, juris, cat, chunk["year"], since)
        yield from docs

    def fetch(self, cfg: Config, client: PoliteClient,
              since: datetime | None = None) -> Iterator[FetchedDoc]:
        src = cfg.source(self.name)
        if since is None:
            since = utcnow() - timedelta(days=int(src.get("lookback_days", 60)))

        results: list[TargetResult] = []
        # A document is yielded once per run even if it appears under more than
        # one year query — the pipeline would dedupe it anyway, but emitting
        # duplicates makes every per-run count wrong.
        emitted: set[str] = set()
        for juris in self._jurisdictions(cfg):
            for cat in juris.get("categories", []):
                for year in self._years(since):
                    try:
                        docs, seen = self._pull(cfg, client, juris, cat, year, since,
                                                emitted)
                    except Exception as exc:  # noqa: BLE001 — one category must not kill the run
                        log.warning("civicplus %s cat %s year %s failed: %s",
                                    juris["name"], cat.get("id"), year, exc)
                        results.append(TargetResult(
                            f"{juris['name']}/{cat.get('label', cat['id'])}/{year}",
                            False, f"{type(exc).__name__}: {exc}"))
                        continue
                    results.append(TargetResult(
                        f"{juris['name']}/{cat.get('label', cat['id'])}/{year}",
                        True, records=seen, docs=len(docs)))
                    yield from docs

        failed = [r for r in results if not r.ok]
        if results and len(failed) > len(results) / 2:
            raise SourceFailure(
                f"{len(failed)}/{len(results)} CivicPlus categories failed: "
                + ", ".join(f"{r.name} [{r.detail}]" for r in failed[:8]))

    def _pull(self, cfg: Config, client: PoliteClient, juris: dict, cat: dict,
              year: int, since: datetime,
              emitted: set[str] | None = None) -> tuple[list[FetchedDoc], int]:
        src = cfg.source(self.name)
        base = juris["base_url"].rstrip("/")
        max_pages = int(src.get("max_pdf_pages", 40))
        label = cat.get("label") or str(cat["id"])
        emitted = emitted if emitted is not None else set()

        html = self._category_html(client, base, cat["id"], year)
        rows = [r for r in self._rows(html, self._kinds(cfg)) if r["date"] >= since]

        docs: list[FetchedDoc] = []
        for row in rows:
            uid = f"{base.split('//')[-1]}:{row['uid']}"
            if uid in emitted:
                continue
            emitted.add(uid)
            url = f"{base}{row['href']}"
            try:
                text = pdf_to_text(client.get_bytes(url), max_pages=max_pages)
            except Exception as exc:  # noqa: BLE001 — one packet must not kill the category
                log.warning("civicplus PDF %s failed: %s", url, exc)
                continue
            if not text.strip():
                # Scanned PDF with no text layer: say so rather than silently
                # treating it as "no data centers mentioned".
                log.warning("civicplus PDF %s has no extractable text layer", url)
                continue
            header = (
                f"{juris['name']} — {label}\n"
                f"Meeting date: {row['date']:%Y-%m-%d}\n"
                f"Document: {row['kind']} ({row['title']})\n"
                f"Source: {url}\n"
            )
            if not keyword_match(header + text, cfg):
                continue
            docs.append(FetchedDoc(
                source=self.name,
                source_uid=uid,
                url=url,
                title=f"[{juris['name']}] {label} {row['date']:%Y-%m-%d} — {row['title']}"[:250],
                raw_text=header + "\n" + text,
                published_at=row["date"],
                meta={"jurisdiction": juris["name"], "county": juris.get("county"),
                      "state": juris.get("state"), "body": label,
                      "document_kind": row["kind"], "platform": "civicplus",
                      "pdf_pages_parsed": max_pages},
                default_signal_type=SignalType.planning_agenda,
            ))
        return docs, len(rows)

    def verify(self, cfg: Config, client: PoliteClient) -> dict:
        """Probe each jurisdiction/category for the current year, and download the
        single newest packet to prove PDFs still yield text.

        Listing rows alone is not enough: Storey County posts some documents as
        image-only scans (its quorum-notice category is entirely scanned), and a
        category that lists 14 rows yielding 0 extractable characters would
        otherwise look perfectly healthy.
        """
        results: list[TargetResult] = []
        year = utcnow().year
        kinds = self._kinds(cfg)
        floor = self.min_doc_chars(cfg)
        max_pages = int(cfg.source(self.name).get("max_pdf_pages", 40))
        probed = False

        for juris in self._jurisdictions(cfg):
            base = juris["base_url"].rstrip("/")
            for cat in juris.get("categories", []):
                name = f"{juris['name']}/{cat.get('label', cat['id'])}"
                try:
                    html = self._category_html(client, base, cat["id"], year)
                except Exception as exc:  # noqa: BLE001
                    results.append(TargetResult(name, False, f"{type(exc).__name__}: {exc}"))
                    continue
                rows = self._rows(html, kinds)
                if not rows:
                    results.append(TargetResult(name, True, "no documents listed this year"))
                    continue
                if probed:
                    results.append(TargetResult(name, True, records=len(rows), docs=len(rows)))
                    continue
                # One PDF per verify run, on the first non-empty category.
                probed = True
                url = f"{base}{rows[0]['href']}"
                try:
                    text = pdf_to_text(client.get_bytes(url), max_pages=max_pages)
                except Exception as exc:  # noqa: BLE001
                    results.append(TargetResult(name, False, f"PDF failed: {exc}"))
                    continue
                if len(text.strip()) < floor:
                    results.append(TargetResult(
                        name, False,
                        f"PDF yielded {len(text.strip())} chars (< {floor}); "
                        f"likely image-only scan: {url}", records=len(rows)))
                    continue
                results.append(TargetResult(
                    name, True, f"newest packet {len(text)} chars",
                    records=len(rows), docs=len(rows)))
        return fanout_verify("categories", results)
