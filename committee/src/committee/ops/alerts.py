"""Alerting: email (SMTP_URL), optional ntfy/Pushover-style webhook, always the journal.

SMTP_URL format: smtp[s]://user:password@host:port ; DIGEST_TO: recipient.
"""

from __future__ import annotations

import smtplib
import ssl
from collections.abc import Callable
from dataclasses import dataclass, field
from email.message import EmailMessage
from urllib.parse import unquote, urlparse

import httpx

Sender = Callable[[str, str], None]


def smtp_sender(smtp_url: str, to: str, sender: str = "committee@localhost") -> Sender:
    u = urlparse(smtp_url)

    def send(subject: str, body: str) -> None:
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = (
            unquote(u.username or sender) if u.username and "@" in unquote(u.username) else sender
        )
        msg["To"] = to
        msg.set_content(body)
        host, port = u.hostname or "localhost", u.port or (465 if u.scheme == "smtps" else 587)
        if u.scheme == "smtps":
            with smtplib.SMTP_SSL(
                host, port, context=ssl.create_default_context(), timeout=30
            ) as s:
                if u.username:
                    s.login(unquote(u.username), unquote(u.password or ""))
                s.send_message(msg)
        else:
            with smtplib.SMTP(host, port, timeout=30) as s:
                s.starttls(context=ssl.create_default_context())
                if u.username:
                    s.login(unquote(u.username), unquote(u.password or ""))
                s.send_message(msg)

    return send


def ntfy_sender(topic_url: str, transport: httpx.BaseTransport | None = None) -> Sender:
    def send(subject: str, body: str) -> None:
        with httpx.Client(timeout=15, transport=transport) as c:
            c.post(topic_url, content=body.encode(), headers={"Title": subject[:200]})

    return send


@dataclass
class Alerter:
    senders: list[Sender] = field(default_factory=list)
    sent: list[tuple[str, str]] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)

    def alert(self, level: str, subject: str, body: str) -> None:
        full = f"[Committee {level}] {subject}"
        self.sent.append((full, body))
        for s in self.senders:
            try:
                s(full, body)
            except Exception as e:  # alerting must never crash the job that raised it
                self.failures.append(f"{type(e).__name__}: {e}"[:200])
