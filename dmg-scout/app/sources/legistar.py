"""Legistar Web API (webapi.legistar.com/v1/{client}) — city/county agendas.

OData JSON API, no key required for public data. We pull recent Matters per
client and keyword-match titles. Clients whose API returns 404 are reported in
source health (many Granicus sites are InSite-only, not Legistar-API-enabled;
the verify-sources command surfaces which client slugs are wrong).
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Iterator

from app.config import Config
from app.http import PoliteClient
from app.models import SignalType
from app.sources.base import FetchedDoc, SourceAdapter, SourceFailure, keyword_match

log = logging.getLogger(__name__)


class LegistarAdapter(SourceAdapter):
    name = "legistar"

    def fetch(self, cfg: Config, client: PoliteClient) -> Iterator[FetchedDoc]:
        src = cfg.source(self.name)
        api_base = src.get("api_base", "https://webapi.legistar.com/v1").rstrip("/")
        lookback = int(src.get("lookback_days", 21))
        since = (datetime.utcnow() - timedelta(days=lookback)).strftime("%Y-%m-%dT00:00:00")

        clients = src.get("clients", [])
        failures = 0
        for entry in clients:
            slug, label = entry["client"], entry.get("label", entry["client"])
            url = (
                f"{api_base}/{slug}/matters"
                f"?$filter=MatterLastModifiedUtc+ge+datetime'{since}'&$top=200"
            )
            try:
                matters = client.get_json(url)
            except Exception as exc:  # noqa: BLE001
                log.warning("legistar client %s failed: %s", slug, exc)
                failures += 1
                continue
            if not isinstance(matters, list):
                log.warning("legistar client %s returned non-list payload", slug)
                continue
            for m in matters:
                title = m.get("MatterTitle") or m.get("MatterName") or ""
                body_name = m.get("MatterBodyName") or ""
                text = f"{title}\n{m.get('MatterName') or ''}\nBody: {body_name}\nJurisdiction: {label}"
                if not keyword_match(text, cfg):
                    continue
                mid = m.get("MatterId")
                intro = m.get("MatterIntroDate") or m.get("MatterLastModifiedUtc") or ""
                published = None
                try:
                    published = datetime.fromisoformat(intro.split(".")[0])
                except (ValueError, AttributeError):
                    pass
                web_url = f"https://{slug}.legistar.com/LegislationDetail.aspx?ID={mid}"
                yield FetchedDoc(
                    source=self.name,
                    source_uid=f"{slug}:{mid}",
                    url=web_url,
                    title=f"[{label}] {title[:180]}",
                    raw_text=text,
                    published_at=published,
                    meta={"client": slug, "jurisdiction": label,
                          "matter_type": m.get("MatterTypeName"), "file_no": m.get("MatterFile")},
                    default_signal_type=SignalType.planning_agenda,
                )
        if clients and failures == len(clients):
            raise SourceFailure(f"all {failures} Legistar clients failed")
