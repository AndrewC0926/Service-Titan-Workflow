"""Legistar Web API (webapi.legistar.com/v1/{client}) — city/county agendas.

OData JSON API, no key required for public data. We pull recent Matters per
client and keyword-match titles.

Reading the failure modes correctly matters here, because the first live run
reported [OK] while ten of twelve clients were dead:

* **HTTP 500 with `LegistarConnectionString setting is not set up in InSite for
  client: X` means the slug does not exist.** It is a permanent configuration
  error dressed up as a server error — retrying it is pure waste, and treating
  it as a transient blip is what hid ten broken slugs. We surface it as
  "no such Legistar client".
* Slugs are *not* derivable from the city name. Verified live: Clark County is
  `clark` (not `clarkcountynv`), Washoe County is `washoe-nv`, Riverside County
  is `riversidecountyca`. Guessing produced a 2/12 hit rate.
* A jurisdiction can publish agendas through Granicus/PrimeGov/eScribe/CivicPlus
  without being a Legistar tenant at all. Those belong in their own adapters,
  not as dead slugs here — see config.yaml for the ones we removed and why.

Status is rolled up by fanout_verify(), so a majority-failing source reports
FAIL and any single dead slug reports WARN.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Iterator

from app.config import Config
from app.http import PoliteClient
from app.models import SignalType, utcnow
from app.sources.base import (
    FetchedDoc, SourceAdapter, SourceFailure, TargetResult, fanout_verify, keyword_match,
)

log = logging.getLogger(__name__)

# Marker in Legistar's 500 body that means "this client slug is not provisioned".
_UNKNOWN_CLIENT = "LegistarConnectionString setting is not set up"


def _describe(exc: Exception) -> str:
    """Turn a request failure into something a human can act on."""
    resp = getattr(exc, "response", None)
    if resp is None:
        return f"{type(exc).__name__}: {exc}"
    try:
        body = resp.text[:500]
    except Exception:  # noqa: BLE001
        body = ""
    if _UNKNOWN_CLIENT in body:
        return (f"HTTP {resp.status_code}: no such Legistar client "
                f"(slug wrong, or Web API not enabled for this tenant)")
    return f"HTTP {resp.status_code}"


class LegistarAdapter(SourceAdapter):
    name = "legistar"

    def _clients(self, cfg: Config) -> list[dict]:
        return cfg.source(self.name).get("clients", [])

    def _matters_url(self, api_base: str, slug: str, since: datetime) -> str:
        stamp = since.strftime("%Y-%m-%dT00:00:00")
        return (f"{api_base}/{slug}/matters"
                f"?$filter=MatterLastModifiedUtc+ge+datetime'{stamp}'&$top=200")

    def _since(self, cfg: Config, since: datetime | None) -> datetime:
        if since is not None:
            return since
        days = int(cfg.source(self.name).get("lookback_days", 21))
        return utcnow() - timedelta(days=days)

    def _probe(self, cfg: Config, client: PoliteClient, api_base: str,
               entry: dict, since: datetime) -> tuple[TargetResult, list[FetchedDoc]]:
        """Pull one client. Returns its health plus whatever matched."""
        slug = entry["client"]
        label = entry.get("label", slug)
        try:
            matters = client.get_json(self._matters_url(api_base, slug, since))
        except Exception as exc:  # noqa: BLE001 — one client must not kill the run
            detail = _describe(exc)
            log.warning("legistar client %s (%s) failed: %s", slug, label, detail)
            return TargetResult(slug, False, detail), []
        if not isinstance(matters, list):
            return TargetResult(slug, False, "non-list payload"), []

        docs = [d for m in matters if (d := self._to_doc(m, slug, label, cfg)) is not None]
        return TargetResult(slug, True, records=len(matters), docs=len(docs)), docs

    def _to_doc(self, m: dict, slug: str, label: str, cfg: Config) -> FetchedDoc | None:
        title = m.get("MatterTitle") or m.get("MatterName") or ""
        body_name = m.get("MatterBodyName") or ""
        text = (f"{title}\n{m.get('MatterName') or ''}\n"
                f"Body: {body_name}\nJurisdiction: {label}")
        if not keyword_match(text, cfg):
            return None
        mid = m.get("MatterId")
        intro = m.get("MatterIntroDate") or m.get("MatterLastModifiedUtc") or ""
        published = None
        try:
            published = datetime.fromisoformat(intro.split(".")[0])
        except (ValueError, AttributeError):
            pass
        return FetchedDoc(
            source=self.name,
            source_uid=f"{slug}:{mid}",
            url=f"https://{slug}.legistar.com/LegislationDetail.aspx?ID={mid}",
            title=f"[{label}] {title[:180]}",
            raw_text=text,
            published_at=published,
            meta={"client": slug, "jurisdiction": label,
                  "matter_type": m.get("MatterTypeName"), "file_no": m.get("MatterFile")},
            default_signal_type=SignalType.planning_agenda,
        )

    def fetch(self, cfg: Config, client: PoliteClient,
              since: datetime | None = None) -> Iterator[FetchedDoc]:
        src = cfg.source(self.name)
        api_base = src.get("api_base", "https://webapi.legistar.com/v1").rstrip("/")
        since = self._since(cfg, since)
        clients = self._clients(cfg)

        results: list[TargetResult] = []
        for entry in clients:
            result, docs = self._probe(cfg, client, api_base, entry, since)
            results.append(result)
            yield from docs

        # Majority failure is an outage, not a partial success. Matches the
        # threshold fanout_verify() uses so `run` and `verify-sources` agree.
        failed = [r for r in results if not r.ok]
        if clients and len(failed) > len(clients) / 2:
            raise SourceFailure(
                f"{len(failed)}/{len(clients)} Legistar clients failed: "
                + ", ".join(f"{r.name} [{r.detail}]" for r in failed))

    def verify(self, cfg: Config, client: PoliteClient) -> dict:
        src = cfg.source(self.name)
        api_base = src.get("api_base", "https://webapi.legistar.com/v1").rstrip("/")
        since = self._since(cfg, None)
        results = [self._probe(cfg, client, api_base, entry, since)[0]
                   for entry in self._clients(cfg)]
        return fanout_verify("clients", results)
