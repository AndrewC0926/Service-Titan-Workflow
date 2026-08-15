"""NOTIFY: daily digest — a decision, not a dump.

Redesigned because the old digest was a list: new projects, stage changes,
review queue, source failures, saved searches, one recommended call — six
sections, unbounded length, read on a laptop or not at all. This is four
sections, capped, meant to be read on a phone in the sixty seconds before a
laptop opens:

1. Three calls to make today, ranked by score x reachability x how close the
   stage is to the influence window closing — not a menu, a decision.
2. What changed since yesterday — new projects, stage changes, newly
   contactable rows. Nothing else. Says so plainly when nothing did.
3. Overdue and due next actions, oldest first — the accountability half.
4. One thing worth knowing: LLM budget, a stale source, a county gone quiet,
   or how many things changed today. Whichever is most urgent, one line.

Dropped from the email on purpose, not forgotten: the review queue (ambiguous
match candidates), the full source-failure list, and saved-search results.
All three still live on the dashboard (the review queue and source health
there, /searches for saved searches) — they just do not belong in something
meant to fit a phone screen. See docs for the "Today" page (Phase 2), which
is meant to carry more of this detail, clickable, without becoming an email.

DigestLog rows record what was already reported, same mechanism as before:
an item reappears only when it is new or has materially changed.
"""
from __future__ import annotations

import logging
import os
import smtplib
from datetime import timedelta
from email.mime.text import MIMEText

import httpx
from sqlmodel import Session, select

from app.config import Config
from app.ladder import build_ladders, contact_status
from app.models import (
    ACTIVE_STATUSES, DigestLog, Outreach, Project, SourceRun, run_name_source, utcnow,
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


# ---- section 1: three calls today -----------------------------------------

def _call_priority(project: Project, cs: dict) -> float:
    """score x reachability x window-proximity.

    Reachability discounts how far from the spec decision the callable
    contact sits: proximity_given_up is 0 when the closest person to the
    decision is also the one with a phone or email, and grows the further out
    on the ladder we had to reach to find someone callable (see
    app.ladder.contact_status). Window proximity discounts how far out the
    estimated bid date is — sooner is more urgent. Both are damping factors,
    not scores in their own right; score still leads the ranking.
    """
    reach = 1.0 / (1.0 + cs["proximity_given_up"])
    days = project.days_to_estimated_bid
    window = 1.0 / (1.0 + days / 30.0) if days is not None else 0.5
    return project.score * reach * window


def _call_reason(project: Project, age) -> str:
    bits = [project.window.value.replace("_", "-")]
    if project.days_to_estimated_bid is not None:
        if project.days_to_estimated_bid_low is not None:
            bits.append(f"~{project.days_to_estimated_bid}d to est. bid "
                       f"(95% CI {project.days_to_estimated_bid_low}-{project.days_to_estimated_bid_high}d)")
        else:
            bits.append(f"~{project.days_to_estimated_bid}d to est. bid")
    if age is not None and age.days is not None:
        bits.append(f"stage UNVERIFIED, {age.label} old" if age.unverified()
                    else f"stage confirmed {age.label} ago")
    return ", ".join(bits)


def three_calls_today(session: Session, projects: list[Project] | None = None,
                      ladders: dict[int, list[dict]] | None = None) -> list[dict]:
    """Top 3 contactable active projects by _call_priority — never more.

    Accepts precomputed projects/ladders so a caller that also needs
    _detect_changes (the Today page does) can build the one genuinely
    expensive thing — build_ladders over every active project — ONCE rather
    than once per section. See today_brief() below.
    """
    from app.staleness import stage_ages

    if projects is None:
        projects = session.exec(select(Project).where(Project.status.in_(ACTIVE_STATUSES))).all()
    if not projects:
        return []
    if ladders is None:
        ladders = build_ladders(session, projects)
    ages = stage_ages(session, projects)

    ranked = []
    for p in projects:
        cs = contact_status(session, p, ladder=ladders[p.id])
        if cs["status"] != "contactable":
            continue
        ranked.append((_call_priority(p, cs), p, cs))
    ranked.sort(key=lambda t: -t[0])

    return [{"project": p, "contact": cs["best_reachable"], "reason": _call_reason(p, ages.get(p.id))}
            for _, p, cs in ranked[:3]]


def _render_calls(calls: list[dict]) -> str:
    if not calls:
        return "THREE TO CALL\n  Nobody contactable on the board right now."
    lines = ["THREE TO CALL"]
    for i, c in enumerate(calls, 1):
        p, contact = c["project"], c["contact"]
        reach = contact.get("phone") or contact.get("email") or "no contact info"
        tons = f"{p.tons_estimate_low:,.0f}-{p.tons_estimate_high:,.0f} tons" if p.tons_estimate_low else ""
        lines.append(f"{i}. {contact['name']} — {reach}")
        lines.append(f"   re: {p.name} ({p.county or '?'} Co) {('— ' + tons) if tons else ''}")
        lines.append(f"   why: {c['reason']}")
    return "\n".join(lines)


# ---- section 2: what changed since yesterday -------------------------------

def _detect_changes(session: Session, cfg: Config, projects: list[Project] | None = None,
                    ladders: dict[int, list[dict]] | None = None,
                    ) -> tuple[list[str], list[tuple[str, int, str]]]:
    """Read-only: what WOULD be reported, and the (kind, ref_id, fingerprint)
    marks that recognizing it requires. Marking is a separate step (see
    _changes_since_last_digest) so the Today page can show this same list on
    every page load without consuming tomorrow's digest — see that function's
    docstring for why interleaving detection and marking is the wrong shape
    for anything called more than once a day. Accepts precomputed
    projects/ladders for the same reason three_calls_today does.
    """
    min_score = cfg.get("scoring.min_digest_score", 0.15)
    lines: list[str] = []
    marks: list[tuple[str, int, str]] = []
    if projects is None:
        projects = session.exec(select(Project).where(Project.status.in_(ACTIVE_STATUSES))).all()
    if ladders is None:
        ladders = build_ladders(session, projects) if projects else {}

    # new, above threshold
    for p in sorted(projects, key=lambda p: -p.score):
        if p.score < min_score or _already_sent(session, "new_project", p.id, "v1"):
            continue
        tons = (f"{p.tons_estimate_low:,.0f}-{p.tons_estimate_high:,.0f} tons"
               if p.tons_estimate_low else "size unknown")
        lines.append(f"NEW: {p.name} — {p.county or '?'} Co — {tons} — score {p.score:.2f}")
        marks.append(("new_project", p.id, "v1"))

    # stage changes — only once a previous stage was itself recorded, so a
    # project's first-ever stage reading never reads as a "change". Marked
    # every run regardless of whether it was reported: that baseline is what
    # lets a FUTURE stage change be recognized as one.
    for p in projects:
        fp = f"stage:{p.stage.value}"
        if _already_sent(session, "stage_change", p.id, fp):
            continue
        prior = session.exec(
            select(DigestLog).where(DigestLog.kind == "stage_change",
                                    DigestLog.ref_id == p.id)).first()
        if prior is not None and _already_sent(session, "new_project", p.id, "v1"):
            lines.append(f"STAGE: {p.name} -> {p.stage.value} ({p.window.value})")
        marks.append(("stage_change", p.id, fp))

    # newly contactable — same shape, fires once the first time a project
    # crosses into "contactable"
    for p in projects:
        cs = contact_status(session, p, ladder=ladders.get(p.id))
        fp = cs["status"]
        if _already_sent(session, "contactable", p.id, fp):
            continue
        prior = session.exec(
            select(DigestLog).where(DigestLog.kind == "contactable",
                                    DigestLog.ref_id == p.id)).first()
        if prior is not None and fp == "contactable":
            best = cs["best_reachable"]
            reach = best.get("phone") or best.get("email")
            lines.append(f"NOW CALLABLE: {p.name} — {best['name']} ({reach})")
        marks.append(("contactable", p.id, fp))

    return lines, marks


def _changes_since_last_digest(session: Session, cfg: Config, projects: list[Project] | None = None,
                               ladders: dict[int, list[dict]] | None = None) -> list[str]:
    """For the real digest send: detect AND mark (staged, uncommitted — see
    run_notify for why the commit belongs after the send)."""
    lines, marks = _detect_changes(session, cfg, projects=projects, ladders=ladders)
    for kind, ref_id, fingerprint in marks:
        _mark(session, kind, ref_id, fingerprint)
    return lines


def changes_preview(session: Session, cfg: Config, projects: list[Project] | None = None,
                    ladders: dict[int, list[dict]] | None = None) -> list[str]:
    """For the Today page: the same detection, no marking. Safe to call on
    every page load — tomorrow's digest still sees these as unreported."""
    lines, _ = _detect_changes(session, cfg, projects=projects, ladders=ladders)
    return lines


def _render_changes(lines: list[str]) -> str:
    if not lines:
        return "CHANGED\n  Nothing changed since yesterday."
    return "CHANGED\n" + "\n".join(f"  {ln}" for ln in lines)


# ---- section 3: overdue and due --------------------------------------------

def _overdue_and_due(session: Session, cfg: Config,
                     projects: list[Project] | None = None) -> list[str]:
    """The most recent Outreach entry per project that still carries an open
    next_action — a later outreach entry on the same project supersedes it,
    same as the dashboard's own single next_action field works.

    Two queries total regardless of board size, not one per project: an
    earlier per-project query loop against 325 active projects was the
    dominant cost on the Today page (each a network round trip to a remote
    Postgres), not the ladder-building everyone assumes is the expensive
    part — see app/ladder.py's own "289 rows in under a second" precedent
    for build_ladders, which this now matches by the same batching move.
    """
    due_soon_days = cfg.get("digest.due_soon_days", 3)
    horizon = utcnow() + timedelta(days=due_soon_days)
    now = utcnow()

    if projects is None:
        projects = session.exec(select(Project).where(Project.status.in_(ACTIVE_STATUSES))).all()
    if not projects:
        return []
    by_id = {p.id: p for p in projects}

    all_outreach = session.exec(
        select(Outreach).where(Outreach.project_id.in_(by_id.keys()))
        .order_by(Outreach.date.desc())
    ).all()
    latest_by_project: dict[int, Outreach] = {}
    for o in all_outreach:
        latest_by_project.setdefault(o.project_id, o)  # first hit per id = latest (sorted desc)

    rows = []
    for pid, o in latest_by_project.items():
        if not o.next_action or o.next_action_date is None or o.next_action_date > horizon:
            continue
        rows.append((o.next_action_date, by_id[pid], o))
    rows.sort(key=lambda t: t[0])

    lines = []
    for due_date, p, o in rows:
        overdue = due_date < now
        days = abs((now - due_date).days)
        when = (f"{days}d overdue" if overdue else "due today" if days == 0 else f"due in {days}d")
        lines.append(f"{'OVERDUE' if overdue else 'DUE'}: {p.name} — {o.next_action} ({when})")
    return lines


def _render_overdue(lines: list[str]) -> str | None:
    if not lines:
        return None
    return "DUE\n" + "\n".join(f"  {ln}" for ln in lines)


# ---- section 4: one thing worth knowing ------------------------------------

def _stale_sources(session: Session, cfg: Config) -> list[str]:
    """Cheap staleness check only — deliberately NOT app.ops.doctor(), which
    also probes the Anthropic API. The digest should not pay for an LLM call
    just to decide what to say about itself.

    A source with NO run of any kind recorded is skipped, not flagged: that is
    a setup question (never enabled, never deployed) for `scout doctor` and
    the dashboard's source health to answer, not a daily nag on the phone
    digest. This only flags a source that ran successfully before and has
    since gone quiet.
    """
    stale_cutoff = utcnow() - timedelta(hours=36)
    all_runs = session.exec(select(SourceRun)).all()
    by_source: dict[str, list[SourceRun]] = {}
    for run in all_runs:
        by_source.setdefault(run_name_source(run.source), []).append(run)

    out = []
    for name in [n for n in cfg.data.get("sources", {}) if cfg.source_enabled(n)]:
        if name == "manual":
            continue
        runs = by_source.get(name, [])
        if not runs:
            continue
        oks = [r for r in runs if r.ok]
        if not oks or max(r.started_at for r in oks) < stale_cutoff:
            out.append(name)
    return out


def _quietest_county(session: Session, min_projects: int = 3,
                     threshold_days: int = 60) -> tuple[str, int] | None:
    """A county that used to have activity and has gone silent — not a county
    that was always quiet. Requires at least min_projects active rows so one
    lonely project's silence does not read as a trend."""
    projects = session.exec(select(Project).where(Project.status.in_(ACTIVE_STATUSES))).all()
    by_county: dict[str, list[Project]] = {}
    for p in projects:
        if p.county:
            by_county.setdefault(p.county, []).append(p)

    now = utcnow()
    worst: tuple[str, int] | None = None
    for county, plist in by_county.items():
        if len(plist) < min_projects:
            continue
        last = max((p.last_signal_at for p in plist if p.last_signal_at), default=None)
        if last is None:
            continue
        days = (now - last).days
        if days >= threshold_days and (worst is None or days > worst[1]):
            worst = (county, days)
    return worst


def _one_thing_worth_knowing(session: Session, cfg: Config, n_changes: int) -> str | None:
    from app.spend import budget_status

    st = budget_status()
    if st["exhausted"]:
        return f"LLM budget EXHAUSTED — pipeline stopped. ${st['month_usd']:.2f} spent this month."
    if st["warn"]:
        return (f"LLM budget at warning threshold: ${st['today_usd']:.2f} of "
                f"${st['daily_budget_usd']:.2f} today.")

    stale = _stale_sources(session, cfg)
    if stale:
        return f"{stale[0]} has not run successfully in over 36 hours."

    quiet = _quietest_county(session)
    if quiet:
        county, days = quiet
        return f"{county} has gone quiet — no new signal in {days} days across its board rows."

    if n_changes:
        return f"{n_changes} item(s) changed today — see above."

    return None


# ---- assembly ---------------------------------------------------------------

def build_digest(session: Session, cfg: Config) -> tuple[str, dict] | None:
    """Returns (text, stats) or None when there is nothing to say at all.

    Leaves its DigestLog marks UNCOMMITTED — see run_notify for why the commit
    belongs after the send.
    """
    projects = session.exec(select(Project).where(Project.status.in_(ACTIVE_STATUSES))).all()
    ladders = build_ladders(session, projects) if projects else {}

    calls = three_calls_today(session, projects=projects, ladders=ladders)
    change_lines = _changes_since_last_digest(session, cfg, projects=projects, ladders=ladders)
    overdue_lines = _overdue_and_due(session, cfg, projects=projects)
    one_thing = _one_thing_worth_knowing(session, cfg, len(change_lines))

    # A quiet day with nothing to call and nothing due is a genuinely empty
    # digest — everything else always has SOMETHING to say (even "nothing
    # changed"), so this is the one case worth skipping the send entirely.
    if not calls and not change_lines and not overdue_lines and not one_thing:
        return None

    sections = [_render_calls(calls), _render_changes(change_lines)]
    overdue_section = _render_overdue(overdue_lines)
    if overdue_section:
        sections.append(overdue_section)
    if one_thing:
        sections.append(f"ONE THING\n  {one_thing}")

    body = f"DMG Scout — {utcnow():%a %b %d}\n\n" + "\n\n".join(sections) + "\n"
    stats = {
        "calls": len(calls), "changes": len(change_lines), "overdue": len(overdue_lines),
    }
    return body, stats


def today_brief(session: Session, cfg: Config) -> dict:
    """The Today page's data, in one pass: the same four sections as the
    email, sharing the one genuinely expensive query (build_ladders over
    every active project) across all of them instead of recomputing it per
    section. Read-only throughout — changes_preview, not
    _changes_since_last_digest, so loading this page never consumes a change
    tomorrow's real digest email would otherwise report.
    """
    projects = session.exec(select(Project).where(Project.status.in_(ACTIVE_STATUSES))).all()
    ladders = build_ladders(session, projects) if projects else {}

    calls = three_calls_today(session, projects=projects, ladders=ladders)
    changes = changes_preview(session, cfg, projects=projects, ladders=ladders)
    overdue = _overdue_and_due(session, cfg, projects=projects)
    one_thing = _one_thing_worth_knowing(session, cfg, len(changes))
    return {"calls": calls, "changes": changes, "overdue": overdue, "one_thing": one_thing}


def send_digest(cfg: Config, body: str) -> str:
    """Returns the transport used. 'console' prints (for dev and cron logs)."""
    transport = cfg.get("digest.transport", "console")
    to = cfg.get("digest.to", [])
    subject = f"{cfg.get('digest.subject_prefix', '[DMG Scout]')} {utcnow():%a %b %d}"

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
        # DigestLog rows are staged for every active project's stage and
        # contactable status whether or not anything is reported, so a project
        # never accumulates a prior baseline and its next real change is
        # silently never reported. Nothing was withheld from anyone on this
        # path — there was nothing to send — so there is nothing to take back.
        session.commit()
        return {"sent": False, "reason": "nothing to report"}
    body, stats = built
    try:
        transport = send_digest(cfg, body)
    except Exception:
        # Discard the marks. Re-raised rather than swallowed: a digest that did
        # not go out is not a quiet no-op, it is the one failure mode that hides
        # a lead, and the cron must exit non-zero so the dead man's switch and
        # the run log both show it.
        session.rollback()
        log.error("digest send failed — items left unreported and will be "
                  "included in the next run: %s", stats)
        raise
    session.commit()
    return {"sent": True, "transport": transport, **stats}
