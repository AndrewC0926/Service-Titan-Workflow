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

Narration (2026-08-19): the four sections above are still assembled exactly
as before, fully grounded, and rendered to plain text FIRST. A cheap-model
pass (see narrate_or_fallback, app.llm.narrate_digest) then gets that same
data as JSON and rewords it into prose — never asked to look anything up or
compute anything, only to rephrase. Every number in the result is checked
against the input before it is trusted; any failure — the call itself, an
unparseable response, or a number that doesn't trace back to the input —
falls back to the plain-text digest that was already built, silently to
the reader. See app/llm.py's NARRATE_DIGEST_SYSTEM for the exact
constraints given to the model.
"""
from __future__ import annotations

import json
import logging
import os
import re
import smtplib
from datetime import timedelta
from email.mime.text import MIMEText

import httpx
from sqlmodel import Session, func, select

from app.config import Config
from app.ladder import build_ladders, contact_status
from app.models import (
    ACTIVE_STATUSES, Ab802Building, Ab869Plan, DigestLog, FieldIntel, HcaiProject, OpscProject,
    Outreach, Project, ScaqmdFacility, SourceRowSeen, SourceRun, run_name_source, utcnow,
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

    Cutoff is per-source via app.ops.stale_cutoff — a flat 36 hours here,
    independent of that function, is exactly what silently mis-flagged
    la_ebewe_benchmarking (weekly, Sundays only) as failing on 6 of every 7
    days until 2026-08-19: `scout doctor` already respected a source's own
    `stale_hours` override, this check just never called the same function,
    so the two disagreed about the same source's health. See that
    function's own docstring.
    """
    from app.ops import stale_cutoff

    now = utcnow()
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
        if not oks or max(r.started_at for r in oks) < stale_cutoff(cfg, name, now):
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


# ---- narration (optional prose pass over the same grounded data) -----------
#
# The plain digest above (_render_calls/_render_changes/_render_overdue/
# ONE THING) is the ground truth: every number in it already traces back to
# a query this module ran. Narration NEVER re-derives anything -- it is
# handed exactly that same data as JSON and asked to reword it into prose a
# person wants to read at 6am, nothing more. Two independent safeguards
# keep it from drifting past that:
#   1. The system prompt (app.llm.NARRATE_DIGEST_SYSTEM) instructs the model
#      explicitly not to introduce a fact, number, or inference. A prompt is
#      not a guarantee, so:
#   2. _numbers_grounded checks it after the fact -- every numeric token in
#      the narrated prose must already appear somewhere in the JSON payload
#      it was given. Fails closed: any exception, any missing/non-string
#      body, or any number not traceable to the input, and the caller falls
#      back to the plain digest untouched. Nothing about the plain digest's
#      own construction or DigestLog marking depends on narration succeeding.

_NUMBER_RE = re.compile(r"\d[\d,]*\.?\d*")


def _numbers_in(text: str) -> set[str]:
    """Every distinct numeric token in text, thousands-separator commas
    stripped so '1,234' and '1234' compare equal, but otherwise exact-string
    matched on purpose -- '0.42' must not silently satisfy '0.4', since
    accepting that would let a rounded/reformatted figure pass as
    grounded when it is actually a different number than the input gave."""
    return {tok.replace(",", "") for tok in _NUMBER_RE.findall(text)}


def _numbers_grounded(narrated_body: str, payload: dict) -> bool:
    input_numbers = _numbers_in(json.dumps(payload, default=str))
    output_numbers = _numbers_in(narrated_body)
    stray = output_numbers - input_numbers
    if stray:
        log.warning("digest narration introduced number(s) not present in the input: %s", sorted(stray))
        return False
    return True


def _structured_payload(calls: list[dict], change_lines: list[str],
                        overdue_lines: list[str], one_thing: str | None) -> dict:
    """The narration model's ENTIRE world -- exactly what the plain digest
    already shows, reshaped as JSON, nothing added. calls is re-derived
    into plain fields here (mirroring _render_calls' own formatting, e.g.
    tons banding) because the ORM Project/contact objects in `calls` aren't
    JSON-serializable; change_lines/overdue_lines are already fully-
    rendered, fact-final strings (see _detect_changes/_overdue_and_due), so
    they pass through verbatim rather than being re-derived a second way,
    which could drift from what the plain digest actually says."""
    calls_payload = []
    for c in calls:
        p, contact = c["project"], c["contact"]
        tons = (f"{p.tons_estimate_low:,.0f}-{p.tons_estimate_high:,.0f} tons"
               if p.tons_estimate_low else None)
        calls_payload.append({
            "contact_name": contact["name"],
            "phone": contact.get("phone"),
            "email": contact.get("email"),
            "project_name": p.name,
            "county": p.county,
            "tons_estimate": tons,
            "why_this_call": c["reason"],
        })
    return {
        "calls_to_make_today": calls_payload,
        "changed_since_yesterday": change_lines or "nothing changed since yesterday",
        "overdue_and_due_next_actions": overdue_lines or "nothing due or overdue",
        "one_thing_worth_knowing": one_thing,
    }


def narrate_or_fallback(cfg: Config, calls: list[dict], change_lines: list[str],
                        overdue_lines: list[str], one_thing: str | None,
                        plain_body: str) -> tuple[str, dict]:
    """Returns (body, stats). body is the narrated prose on success, or
    plain_body unchanged on ANY failure -- a bad narration must never
    become "send nothing" or "raise and abort the digest," since the plain
    digest is already fully built and known-good by the time this runs.
    stats records what happened for run_notify's return value and the
    per-digest cost this was asked to be reported alongside app.spend's
    existing tracking; cost_usd is 0.0 whenever narration didn't actually
    call the model (disabled, or budget already exhausted before trying)."""
    from app.spend import BudgetExceeded, run_budget

    if not cfg.get("digest.narrate", True):
        return plain_body, {"narrated": False, "reason": "disabled", "cost_usd": 0.0}

    payload = _structured_payload(calls, change_lines, overdue_lines, one_thing)
    try:
        from app.llm import LLMUnavailable, last_call_cost_usd, narrate_digest
        with run_budget("digest_narration", cap_usd=cfg.get("llm.digest_narration_budget_usd", 0.05)):
            result = narrate_digest(payload)
    except (LLMUnavailable, BudgetExceeded) as exc:
        log.info("digest narration skipped (%s) — sending plain digest", exc)
        return plain_body, {"narrated": False, "reason": str(exc), "cost_usd": 0.0}
    except Exception as exc:  # noqa: BLE001 — narration is best-effort, never fatal to the send
        log.warning("digest narration failed (%s) — sending plain digest", exc)
        return plain_body, {"narrated": False, "reason": str(exc), "cost_usd": 0.0}

    cost = last_call_cost_usd() or 0.0
    body = result.get("body")
    if not body or not isinstance(body, str):
        log.warning("digest narration returned no usable body — sending plain digest")
        return plain_body, {"narrated": False, "reason": "empty body", "cost_usd": cost}
    if not _numbers_grounded(body, payload):
        return plain_body, {"narrated": False, "reason": "ungrounded number", "cost_usd": cost}

    return body.strip() + "\n", {"narrated": True, "cost_usd": cost}


# ---- postscript: field intel (NOT one of the four core sections above) ----
#
# Human-sourced, unverified -- deliberately appended AFTER narration runs,
# never handed to narrate_or_fallback, and never reworded. Narration's own
# safeguards (app.llm.NARRATE_DIGEST_SYSTEM, _numbers_grounded) exist to
# keep an LLM from introducing a fact past what the four core sections
# already ground -- the right answer for a person's exact quote is not "let
# the model paraphrase it more grounded," it's "never let the model touch
# it at all." Appending here means it survives whether narration succeeds
# or falls back.

def _new_field_intel(session: Session) -> list[FieldIntel]:
    """Reported in roughly the last digest cycle -- a flat 24h window, not
    DigestLog-tracked like the four core sections: this is a small, low-
    volume table, and repeating a row once if a digest cycle is missed is
    a fair trade against the state a precise "since last digest" check
    would need. Newest first."""
    cutoff = utcnow() - timedelta(hours=24)
    return session.exec(
        select(FieldIntel).where(FieldIntel.created_at >= cutoff)
        .order_by(FieldIntel.reported_at.desc())
    ).all()


def _render_field_intel(rows: list[FieldIntel]) -> str | None:
    if not rows:
        return None
    lines = ["FIELD INTEL — UNVERIFIED, human-sourced"]
    for fi in rows:
        who = f"{fi.owner or 'unnamed owner'}" + (f", {fi.location}" if fi.location else "")
        lines.append(f"  {who} — from {fi.reported_by} ({fi.reported_at:%Y-%m-%d})")
        if fi.engineer_name or fi.mech_contractor_name:
            named = ", ".join(x for x in (
                f"engineer: {fi.engineer_name}" if fi.engineer_name else None,
                f"mech sub: {fi.mech_contractor_name}" if fi.mech_contractor_name else None,
            ) if x)
            lines.append(f"    {named}")
    lines.append(f"  See /intel — {len(rows)} logged this cycle.")
    return "\n".join(lines)


# ---- new/changed since yesterday (SourceRowSeen) ---------------------------
# docs/DAILY-BRIEF-DESIGN.md §3a item 3, brief integration only -- no
# Opportunity table, no /pipeline, no weekly xlsx (all three blocked on a
# decision, per instruction). Zero LLM calls: every line below is a plain
# f-string over fields already on the row, same discipline as
# _render_field_intel above (appended after narration, never handed to it).
#
# "the existing ranking" from the ask has no single pre-existing cross-table
# score to reuse -- Pipeline A's Project.score doesn't apply to any of these
# five tables, and only one of them (hcai_projects, via
# open_hcai_projects_by_facility_id) has its own internal sort at all.
# Recency (most recent change or first-seen, descending) is used instead:
# it is the one ordering every SourceRowSeen row already carries for free,
# requires no new scoring judgment, and matches how a diff naturally reads
# (most recent activity first). Flagged here as a disclosed interpretation,
# not silently assumed.

def _hcai_lookup(session: Session, natural_key: str):
    return session.get(HcaiProject, natural_key)


def _hcai_describe(row) -> tuple[str, str]:
    stage = row.stage
    why = f"{row.facility_name} ({row.county})"
    if row.is_mechanical:
        why += " — mechanical scope"
    return stage, why


def _ab869_lookup(session: Session, natural_key: str):
    # perm_id is NOT Ab869Plan's primary key (that's the autoincrement
    # `id`) -- session.get() would look up the wrong column entirely, so
    # this has to be a real WHERE query, same as ab802/opsc/scaqmd below.
    return session.exec(select(Ab869Plan).where(Ab869Plan.perm_id == natural_key)).first()


def _ab869_describe(row) -> tuple[str, str]:
    status = row.plan_status or "status not parsed"
    who = row.financially_responsible_party or row.owner_name or f"perm {row.perm_id}"
    why = who + (", delay requested" if row.delay_requested else "")
    return status, why


def _ab802_lookup(session: Session, natural_key: str):
    pmid, _, year = natural_key.partition(":")
    return session.exec(
        select(Ab802Building).where(Ab802Building.portfolio_manager_property_id == pmid,
                                    Ab802Building.year_ending == int(year))
    ).first()


def _ab802_describe(row) -> tuple[str, str]:
    status = "air permit matched" if row.air_permit_facility_id else "no air permit match"
    why = row.property_name or f"{row.portfolio_manager_property_id} ({row.year_ending})"
    return status, why


def _opsc_lookup(session: Session, natural_key: str):
    return session.exec(select(OpscProject).where(OpscProject.application_number == natural_key)).first()


def _opsc_describe(row) -> tuple[str, str]:
    status = row.status or "status not stated"
    why = row.school_name or row.district or row.application_number
    return status, why


def _scaqmd_lookup(session: Session, natural_key: str):
    facility_id, _, source = natural_key.partition(":")
    return session.exec(
        select(ScaqmdFacility).where(ScaqmdFacility.facility_id == facility_id,
                                     ScaqmdFacility.source == source)
    ).first()


def _scaqmd_describe(row) -> tuple[str, str]:
    return "new facility registration", (row.facility_name or row.facility_id)


# source -> (label, lookup fn, describe fn) -- keys match
# app.pipeline.diffs.SOURCE_ROW_FETCHERS exactly.
_BRIEF_SOURCES = {
    "hcai_projects": ("HCAI project", _hcai_lookup, _hcai_describe),
    "ab869_plans": ("AB 869 plan", _ab869_lookup, _ab869_describe),
    "ab802_buildings": ("AB 802 filing", _ab802_lookup, _ab802_describe),
    "opsc_projects": ("OPSC application", _opsc_lookup, _opsc_describe),
    "scaqmd_facilities": ("SCAQMD/CARB facility", _scaqmd_lookup, _scaqmd_describe),
}


def new_or_changed_since_yesterday(session: Session, limit: int = 10) -> list[dict]:
    """Top `limit` rows across all five Pipeline B diff tables that are
    either new (first_seen_at) or genuinely changed (changed_at) in the
    last 24 hours, most recent first. Each dict: source (label), key
    (natural_key), event ("new" or "changed"), stage_or_status, why (one
    line). A row whose linked source-table record has since been deleted
    (lookup returns None -- possible if a full-replace table dropped it
    same-day) is skipped rather than rendered with invented fields."""
    cutoff = utcnow() - timedelta(hours=24)
    seen_rows = session.exec(
        select(SourceRowSeen).where(
            SourceRowSeen.source.in_(_BRIEF_SOURCES),
            (SourceRowSeen.first_seen_at >= cutoff) | (SourceRowSeen.changed_at >= cutoff),
        )
    ).all()

    def _event_time(r: SourceRowSeen):
        return r.changed_at or r.first_seen_at

    seen_rows.sort(key=_event_time, reverse=True)

    out: list[dict] = []
    for r in seen_rows:
        if len(out) >= limit:
            break
        label, lookup, describe = _BRIEF_SOURCES[r.source]
        row = lookup(session, r.natural_key)
        if row is None:
            continue
        stage_or_status, why = describe(row)
        event = "changed" if (r.changed_at and r.changed_at >= cutoff) else "new"
        out.append({"source": label, "key": r.natural_key, "event": event,
                   "stage_or_status": stage_or_status, "why": why, "at": _event_time(r)})
    return out


def _render_new_or_changed(rows: list[dict]) -> str:
    if not rows:
        return "NEW OR CHANGED SINCE YESTERDAY\n  Nothing new or changed in the last 24 hours."
    lines = ["NEW OR CHANGED SINCE YESTERDAY"]
    for r in rows:
        lines.append(f"  [{r['event']}] {r['source']}: {r['why']} — {r['stage_or_status']}")
    return "\n".join(lines)


# ---- manual sources past re-pull date ---------------------------------------
# docs/DAILY-BRIEF-DESIGN.md §1a's own manual-source list, narrowed to the
# five the ask named. Cadence comes from config.yaml's sources.<key>.stale_hours
# (see that file for each source's own citation of where its number comes
# from) -- "last pull" is the most recent timestamp this app itself recorded
# for that table, never the source file's own internal "as of" date (that
# tells you how current the DATA is, not when a human last ran the import).

_MANUAL_RECADENCE_SOURCES = [
    # (display name, config key, model, timestamp column)
    ("HCAI report", "hcai_projects", HcaiProject, "imported_at"),
    ("AB 869", "ab869_compliance_plans", Ab869Plan, "imported_at"),
    ("IEPR", "iepr", None, "imported_at"),  # IeprForwardLoad, imported lazily below (avoid a hard import cycle)
    ("DCA BPELSG file", "bpelsg_mechanical_roster", None, "imported_at"),  # BpelsgEngineer, same reason
    ("AHJ register", "ahj_a2l_guidance", None, "checked_at"),  # AhjA2lGuidance, same reason
]


def manual_sources_past_recadence(session: Session, cfg: Config) -> list[dict]:
    """Every one of the five sources whose actual days-since-last-pull
    exceeds its own registered config.yaml stale_hours -- a source with no
    rows at all (never pulled) counts as past due, not skipped silently.
    A source still inside its own cadence is NOT returned; this list is
    "what needs attention," not a status board of all five (see
    manual_recadence_status below for that)."""
    return [row for row in manual_recadence_status(session, cfg) if row["is_stale"]]


def manual_recadence_status(session: Session, cfg: Config) -> list[dict]:
    """All five sources, stale or not -- the full accounting used by
    manual_sources_past_recadence (brief section) and available directly
    for anything that wants the complete picture, not just the overdue
    subset."""
    from app.models import AhjA2lGuidance, BpelsgEngineer, IeprForwardLoad

    model_by_key = {"iepr": IeprForwardLoad, "bpelsg_mechanical_roster": BpelsgEngineer,
                    "ahj_a2l_guidance": AhjA2lGuidance}
    now = utcnow()
    out = []
    for display_name, config_key, model, ts_col in _MANUAL_RECADENCE_SOURCES:
        model = model or model_by_key[config_key]
        stale_hours = cfg.get(f"sources.{config_key}.stale_hours")
        last_pull = session.exec(select(func.max(getattr(model, ts_col)))).one()
        if last_pull is None:
            out.append({"name": display_name, "last_pull": None, "days_since": None,
                       "cadence_days": stale_hours / 24 if stale_hours else None, "is_stale": True})
            continue
        hours_since = (now - last_pull).total_seconds() / 3600
        is_stale = stale_hours is not None and hours_since > stale_hours
        out.append({"name": display_name, "last_pull": last_pull,
                   "days_since": round(hours_since / 24, 1),
                   "cadence_days": round(stale_hours / 24, 1) if stale_hours else None,
                   "is_stale": is_stale})
    return out


def _render_manual_recadence(rows: list[dict]) -> str | None:
    if not rows:
        return None
    lines = ["MANUAL SOURCES PAST RE-PULL DATE"]
    for r in rows:
        if r["last_pull"] is None:
            lines.append(f"  {r['name']}: never pulled")
            continue
        lines.append(f"  {r['name']}: {r['days_since']:.1f} days since last pull "
                     f"(cadence {r['cadence_days']:.0f} days) — last pulled "
                     f"{r['last_pull']:%Y-%m-%d}")
    return "\n".join(lines)


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
    field_intel_rows = _new_field_intel(session)
    new_or_changed = new_or_changed_since_yesterday(session)
    manual_stale = manual_sources_past_recadence(session, cfg)

    # A quiet day with nothing to call and nothing due is a genuinely empty
    # digest — everything else always has SOMETHING to say (even "nothing
    # changed"), so this is the one case worth skipping the send entirely.
    # Deliberately NOT extended to new_or_changed/manual_stale: an empty
    # source table reads as "never pulled" (see manual_recadence_status),
    # which is always true in a fresh test database and would make this
    # skip-check nearly impossible to hit in isolation -- these two
    # sections are additive to a digest already going out, never a reason
    # to send one otherwise skipped as quiet.
    if not calls and not change_lines and not overdue_lines and not one_thing and not field_intel_rows:
        return None

    sections = [_render_calls(calls), _render_changes(change_lines)]
    overdue_section = _render_overdue(overdue_lines)
    if overdue_section:
        sections.append(overdue_section)
    if one_thing:
        sections.append(f"ONE THING\n  {one_thing}")

    plain_body = f"DMG Scout — {utcnow():%a %b %d}\n\n" + "\n\n".join(sections) + "\n"
    body, narration_stats = narrate_or_fallback(cfg, calls, change_lines, overdue_lines, one_thing, plain_body)

    field_intel_section = _render_field_intel(field_intel_rows)
    if field_intel_section:
        body = body.rstrip("\n") + "\n\n" + field_intel_section + "\n"

    # Both appended AFTER narration, never handed to it -- same reasoning as
    # field_intel above (zero LLM calls, plain f-strings only). Rendered
    # unconditionally so an empty new/changed section still says so
    # explicitly on a genuinely quiet night, matching this digest's own
    # "says so plainly when nothing did" rule for §2.
    body = body.rstrip("\n") + "\n\n" + _render_new_or_changed(new_or_changed) + "\n"
    manual_section = _render_manual_recadence(manual_stale)
    if manual_section:
        body = body.rstrip("\n") + "\n\n" + manual_section + "\n"

    stats = {
        "calls": len(calls), "changes": len(change_lines), "overdue": len(overdue_lines),
        "field_intel": len(field_intel_rows), "new_or_changed": len(new_or_changed),
        "manual_stale": len(manual_stale),
        **narration_stats,
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
    from app.field_intel import field_intel_activity
    new_or_changed = new_or_changed_since_yesterday(session)
    manual_stale = manual_sources_past_recadence(session, cfg)
    return {"calls": calls, "changes": changes, "overdue": overdue, "one_thing": one_thing,
           "field_intel_activity": field_intel_activity(session),
           "new_or_changed": new_or_changed, "manual_stale": manual_stale}


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
