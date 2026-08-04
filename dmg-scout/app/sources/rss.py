"""Generic RSS/Atom adapter: trade press, regional news, Google Alerts feeds."""
from __future__ import annotations

import calendar
import logging
from datetime import datetime
from typing import Iterator

import feedparser

from app.config import Config
from app.http import PoliteClient
from app.models import SignalType
from app.sources.base import FetchedDoc, SourceAdapter, SourceFailure, keyword_match

log = logging.getLogger(__name__)


class RssAdapter(SourceAdapter):
    name = "rss"

    def fetch(self, cfg: Config, client: PoliteClient) -> Iterator[FetchedDoc]:
        src = cfg.source(self.name)
        feeds = src.get("feeds", [])
        failures = 0
        for feed_cfg in feeds:
            feed_name, feed_url = feed_cfg["name"], feed_cfg["url"]
            try:
                raw = client.get_bytes(feed_url)
            except Exception as exc:  # noqa: BLE001
                log.warning("rss feed %s failed: %s", feed_name, exc)
                failures += 1
                continue
            parsed = feedparser.parse(raw)
            if parsed.bozo and not parsed.entries:
                log.warning("rss feed %s unparseable: %s", feed_name, parsed.bozo_exception)
                continue
            for entry in parsed.entries:
                title = entry.get("title", "")
                summary = entry.get("summary", "") or entry.get("description", "")
                link = entry.get("link", "")
                uid = entry.get("id") or link
                if not uid:
                    continue
                text = f"{title}\n\n{summary}"
                if not keyword_match(text, cfg):
                    continue
                published = None
                for key in ("published_parsed", "updated_parsed"):
                    t = entry.get(key)
                    if t:
                        published = datetime.utcfromtimestamp(calendar.timegm(t))
                        break
                yield FetchedDoc(
                    source=self.name,
                    source_uid=f"{feed_name}:{uid}"[:500],
                    url=link,
                    title=f"[{feed_name}] {title[:180]}",
                    raw_text=text,
                    published_at=published,
                    meta={"feed": feed_name},
                    default_signal_type=SignalType.news_report,
                )
        if feeds and failures == len(feeds):
            raise SourceFailure(f"all {failures} RSS feeds failed")
