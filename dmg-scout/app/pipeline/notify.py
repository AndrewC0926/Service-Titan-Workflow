"""NOTIFY: daily digest — new and materially changed only, never a full dump.
DigestLog rows record what was already reported; an item reappears only when its
fingerprint (stage, review status...) changes."""
from __future__ import annotations

import logging
import os
import smtplib
from datetime import timedelta
from email.mime.text import MIMEText

import httpx
from sqlmodel import Session, select

from app.config import Config
from app.models import (
    Contact, DigestLog, MatchCandidate, Project, ProjectContact, SourceRun, Window, utcnow,
)

log = logging.getLogger(__name__)


def _already_sent(session: Session, kind: str, ref_id: int, fingerprint: str) -> bool:
    return session.exec(
        select(DigestLog).where(DigestLog.kind == kind, DigestLog.ref_id == ref_id,
                                DigestLog.fingerprint == fingerprint)
    ).first() is not None


def _mark(session: Session, kind: str, ref_id: int, fingerprint: str) -> None:
    session.add(DigestLog(kind=kind, ref_id=ref_id, fingerprint=fingerprint))


def build_digest(session: Session, cfg: Config) -> tuple[str, dict] | None:
    """Returns (text, stats) or None when there is nothing new to say."""
    min_score = cfg.get("scoring.min_digest_score", 0.15)
    sections: list[str] = []
    stats = {"new_projects": 0, "stage_changes": 0, "reviews": 0, "failures": 0}

    # 1. New projects above threshold
    new_lines = []
    for p in session.exec(select(Project).where(Project.status == "active",
                                                Project.score >= min_score)
                          .order_by(Project.score.desc())).all():
        if _already_sent(session, "new_project", p.id, "v1"):
            continue
        tons = (f"{p.tons_estimate_low:,.0f}-{p.tons_estimate_high:,.0f} tons"
                if p.tons_estimate_low else "size unknown")
        new_lines.append(
            f"  • {p.name} — {p.developer or 'developer unknown'} — "
            f"{p.county or '?'} Co, {p.state or '?'} — {tons} — "
            f"{p.window.value} — score {p.score:.2f}\n"
            f"    why: {p.estimate_basis or 'no size input yet'}; stage {p.stage.value}"
        )
        _mark(session, "new_project", p.id, "v1")
        stats["new_projects"] += 1
    if new_lines:
        sections.append("NEW PROJECTS ABOVE THRESHOLD\n" + "\n".join(new_lines))

    # 2. Stage changes (window closing)
    stage_lines = []
    for p in session.exec(select(Project).where(Project.status == "active")).all():
        fp = f"stage:{p.stage.value}"
        if _already_sent(session, "stage_change", p.id, fp):
            continue
        if _already_sent(session, "new_project", p.id, "v1") and not _already_sent(
                session, "stage_change", p.id, fp):
            prior = session.exec(
                select(DigestLog).where(DigestLog.kind == "stage_change",
                                        DigestLog.ref_id == p.id)).all()
            if prior:  # only report once a *previous* stage was reported
                stage_lines.append(
                    f"  • {p.name}: now {p.stage.value} ({p.window.value}), "
                    f"~{p.days_to_estimated_bid} days to bid"
                )
                stats["stage_changes"] += 1
        _mark(session, "stage_change", p.id, fp)
    if stage_lines:
        sections.append("STAGE CHANGES — WINDOW CLOSING\n" + "\n".join(stage_lines))

    # 3. Review queue
    pending = session.exec(
        select(MatchCandidate).where(MatchCandidate.status == "pending")).all()
    review_lines = []
    for mc in pending:
        if _already_sent(session, "review_pending", mc.id, "v1"):
            continue
        review_lines.append(f"  • match candidate #{mc.id} (similarity {mc.similarity:.2f}) "
                            f"— review in dashboard")
        _mark(session, "review_pending", mc.id, "v1")
        stats["reviews"] += 1
    if review_lines:
        sections.append("UNCERTAIN MATCHES NEEDING REVIEW\n" + "\n".join(review_lines))

    # 4. Source failures (last 24h)
    cutoff = utcnow() - timedelta(hours=24)
    fail_lines = []
    for run in session.exec(select(SourceRun).where(SourceRun.ok == False,  # noqa: E712
                                                    SourceRun.started_at >= cutoff)).all():
        if _already_sent(session, "source_failure", run.id, "v1"):
            continue
        first_line = (run.error or "").splitlines()[0] if run.error else "unknown error"
        fail_lines.append(f"  • {run.source}: {first_line}")
        _mark(session, "source_failure", run.id, "v1")
        stats["failures"] += 1
    if fail_lines:
        sections.append("SOURCE FAILURES\n" + "\n".join(fail_lines))

    # 5. Call recommendation: top PRE_BOD project with a named engineer of record
    rec = _call_recommendation(session)
    if rec:
        sections.append("CALL THIS PERSON THIS WEEK\n" + rec)

    if not any([new_lines, stage_lines, review_lines, fail_lines]):
        return None
    body = f"DMG Scout digest — {utcnow():%Y-%m-%d}\n\n" + "\n\n".join(sections) + "\n"
    session.commit()
    return body, stats


def _call_recommendation(session: Session) -> str | None:
    top = session.exec(
        select(Project).where(Project.status == "active", Project.window == Window.PRE_BOD)
        .order_by(Project.score.desc())
    ).all()
    for p in top:
        links = session.exec(
            select(ProjectContact).where(ProjectContact.project_id == p.id,
                                         ProjectContact.role == "engineer_of_record")).all()
        for link in links:
            c = session.get(Contact, link.contact_id)
            if c:
                reach = c.phone or c.email or "no contact info on file"
                return (f"  {c.name} ({c.title or 'EOR'}, {c.company or '?'}) — {reach}\n"
                        f"  re: {p.name} ({p.county} Co) — score {p.score:.2f}, "
                        f"{p.window.value}, ~{p.days_to_estimated_bid} days to bid")
    return None


def send_digest(cfg: Config, body: str) -> str:
    """Returns the transport used. 'console' prints (for dev and cron logs)."""
    transport = cfg.get("digest.transport", "console")
    to = cfg.get("digest.to", [])
    subject = f"{cfg.get('digest.subject_prefix', '[DMG Scout]')} Daily digest {utcnow():%Y-%m-%d}"

    if transport == "resend":
        api_key = os.environ.get(cfg.get("digest.resend.api_key_env", "RESEND_API_KEY"), "")
        if not api_key:
            raise RuntimeError("resend transport selected but API key env var is empty")
        resp = httpx.post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {api_key}"},
            json={"from": cfg.get("digest.from_addr"), "to": to, "subject": subject, "text": body},
            timeout=30,
        )
        resp.raise_for_status()
        return "resend"

    if transport == "smtp":
        smtp_cfg = cfg.get("digest.smtp", {})
        password = os.environ.get(smtp_cfg.get("password_env", "SMTP_PASSWORD"), "")
        msg = MIMEText(body)
        msg["Subject"], msg["From"], msg["To"] = subject, cfg.get("digest.from_addr"), ", ".join(to)
        with smtplib.SMTP(smtp_cfg["host"], smtp_cfg.get("port", 587)) as server:
            server.starttls()
            if smtp_cfg.get("username"):
                server.login(smtp_cfg["username"], password)
            server.sendmail(cfg.get("digest.from_addr"), to, msg.as_string())
        return "smtp"

    print(body)
    return "console"


def run_notify(session: Session, cfg: Config) -> dict:
    if not cfg.get("digest.enabled", True):
        return {"sent": False, "reason": "digest disabled"}
    built = build_digest(session, cfg)
    if built is None:
        return {"sent": False, "reason": "nothing new"}
    body, stats = built
    transport = send_digest(cfg, body)
    return {"sent": True, "transport": transport, **stats}
