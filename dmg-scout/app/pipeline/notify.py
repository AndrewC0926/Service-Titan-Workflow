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
    ACTIVE_STATUSES, Contact, DigestLog, MatchCandidate, Project, ProjectContact, SourceRun,
    Window, utcnow,
)

log = logging.getLogger(__name__)


def _already_sent(session: Session, kind: str, ref_id: int, fingerprint: str) -> bool:
    return session.exec(
        select(DigestLog).where(DigestLog.kind == kind, DigestLog.ref_id == ref_id,
                                DigestLog.fingerprint == fingerprint)
    ).first() is not None


def _mark(session: Session, kind: str, ref_id: int, fingerprint: str) -> None:
    """Stage a 'reported' record. NOT durable until run_notify commits it.

    session.add rather than a plain list on purpose: the sections below query
    DigestLog to decide what to report, and autoflush makes marks staged earlier
    in this same build visible to those queries. Losing that would change which
    items a digest selects. The durability boundary is the commit, and the commit
    belongs after the send — see run_notify.
    """
    session.add(DigestLog(kind=kind, ref_id=ref_id, fingerprint=fingerprint))


def build_digest(session: Session, cfg: Config) -> tuple[str, dict] | None:
    """Returns (text, stats) or None when there is nothing new to say.

    Leaves its DigestLog marks UNCOMMITTED. Nothing here has been reported to a
    human yet — it has only been written down — and committing at this point is
    what let a failed send mark a lead as delivered.
    """
    min_score = cfg.get("scoring.min_digest_score", 0.15)
    sections: list[str] = []
    stats = {"new_projects": 0, "stage_changes": 0, "reviews": 0, "failures": 0,
             "saved_searches": 0}

    # 1. New projects above threshold
    new_lines = []
    for p in session.exec(select(Project).where(Project.status.in_(ACTIVE_STATUSES),
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
    for p in session.exec(select(Project).where(Project.status.in_(ACTIVE_STATUSES))).all():
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

    # 4b. Saved searches — standing questions the rep asked to be told about.
    # Evaluated with commit=False: like every other section here, the marks and
    # the last_seen cursor only become durable once the digest has actually been
    # sent. A saved search whose cursor advanced on a failed send would skip the
    # very change it was created to catch, and skip it silently.
    from app.searches import digest_section
    saved_text, saved_count = digest_section(session)
    if saved_text:
        sections.append(saved_text)
        stats["saved_searches"] = saved_count

    # 5. Call recommendation: top PRE_BOD project with a named engineer of record
    rec = _call_recommendation(session)
    if rec:
        sections.append("CALL THIS PERSON THIS WEEK\n" + rec)

    # 6. LLM budget warning at the configured threshold
    from app.spend import budget_status
    st = budget_status()
    if st["warn"] or st["exhausted"]:
        state = "EXHAUSTED — LLM calls are stopped" if st["exhausted"] else "at warning threshold"
        sections.append(
            f"LLM BUDGET {state}\n  today ${st['today_usd']:.2f} of "
            f"${st['daily_budget_usd']:.2f} daily budget; ${st['month_usd']:.2f} this month"
        )

    # saved_text counts: a saved search firing is a reason to send a digest
    # even on a morning when nothing else moved — that is what it is for.
    if not any([new_lines, stage_lines, review_lines, fail_lines, saved_text]):
        return None
    body = f"DMG Scout digest — {utcnow():%Y-%m-%d}\n\n" + "\n\n".join(sections) + "\n"
    return body, stats


def _call_recommendation(session: Session) -> str | None:
    """Best contact from the ladder on the top PRE_BOD project — falls down the
    rungs (EOR -> firm -> GC precon -> ... -> lead agency planner) rather than
    returning empty when no EOR is named, which is most of the time."""
    from app.ladder import best_contact
    top = session.exec(
        select(Project).where(Project.status.in_(ACTIVE_STATUSES),
                              Project.window == Window.PRE_BOD,
                              Project.in_territory == True)  # noqa: E712
        .order_by(Project.score.desc())
    ).all()
    for p in top:
        # A CRM contact with a phone number still beats a ladder rung when present.
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
        best = best_contact(session, p)
        if best:
            src = f"\n  source: {best['source_url']}" if best.get("source_url") else ""
            return (f"  {best['name']}"
                    f"{' (' + best['title'] + ')' if best.get('title') else ''}"
                    f" — rung {best['rung']}: {best['rung_label']}{src}\n"
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
    """Build, send, and only then record what was sent.

    The order matters and it used to be wrong. build_digest committed a DigestLog
    row for every item as it collected it, and send_digest ran afterwards — so an
    SMTP timeout or a 500 from Resend left every item in that digest permanently
    marked as reported. The items never reappear, because "already sent" is
    exactly what DigestLog means. A lead would vanish between two runs and there
    would be nothing anywhere to say it had ever existed.

    Now the send is the commit's precondition. The failure direction is
    deliberate: if the send succeeds and the commit then fails, the next digest
    repeats a few items, which is noise a human notices and shrugs at. The
    opposite trade loses the lead silently, and this system's whole value is that
    somebody hears about the project.
    """
    if not cfg.get("digest.enabled", True):
        return {"sent": False, "reason": "digest disabled"}
    built = build_digest(session, cfg)
    if built is None:
        # Committed, NOT rolled back, and the distinction is easy to get backwards.
        # Two kinds of row ride in DigestLog: "this was reported to a human", and
        # section 2's stage observations, which are staged for every active project
        # whether or not anything is reported. A stage change is only reported once
        # a PREVIOUS stage was recorded, so discarding those observations because
        # the digest happened to be empty means the project never accumulates a
        # prior stage and its next stage change is silently never reported. Nothing
        # was withheld from anyone on this path — there was nothing to send — so
        # there is nothing to take back.
        session.commit()
        return {"sent": False, "reason": "nothing new"}
    body, stats = built
    try:
        transport = send_digest(cfg, body)
    except Exception:
        # Discard the marks. Re-raised rather than swallowed: a digest that did
        # not go out is not a quiet no-op, it is the one failure mode that hides
        # a lead, and the cron must exit non-zero so the dead man's switch and
        # the run log both show it.
        session.rollback()
        log.error("digest send failed — %d items left unreported and will be "
                  "included in the next run", sum(stats.values()))
        raise
    session.commit()
    return {"sent": True, "transport": transport, **stats}
