"""The eleven MCP tools. Imported by app/mcp_server.py, which owns the `mcp`
FastMCP instance these register against — import order matters (mcp must
exist before this module's decorators run, and must not have called
http_app() yet), so app/mcp_server.py imports this module, not the other way
around.

Every tool opens its own app.db.session_scope() rather than depending on a
shared session — matches how CLI commands and background pipeline stages do
it elsewhere in this codebase, and keeps each call's DB work in its own
transaction regardless of how FastMCP schedules concurrent tool calls.

Text, not JSON: every return value is a string meant to be read, not parsed —
"a project should read like a briefing, not a row." Every tonnage or dollar
figure keeps its basis/confidence flag inline, exactly as the dashboard shows
it; nothing here restates a number without the caveat that came with it. A
list caps at 20 rows and says how many were left out rather than silently
truncating.

log_outreach is the only tool that writes to Postgres, and it only ever
inserts an Outreach row — the same table and shape the dashboard's own
outreach form writes to. Nothing here touches a pipeline table (Signal,
Project's own pipeline-owned fields, ProjectSignal, ...). pre_call_brief
caches its output too, but to a local JSON file, never a database row — see
app.precall's module docstring for why.
"""
from __future__ import annotations

from app.mcp_server import mcp


def _project_row(p, age=None) -> str:
    """One-line summary shared by search_projects and who_to_call."""
    if p.tons_estimate_low:
        tons = f"{p.tons_estimate_low:,.0f}-{p.tons_estimate_high:,.0f} tons"
        if p.estimate_low_confidence:
            tons += " [LOW CONFIDENCE]"
    else:
        tons = "size unknown"
    stage_note = p.stage.value
    if age and age.unverified():
        stage_note += f" (UNVERIFIED, {age.label} old)"
    loc = f"{p.county or '?'} Co, {p.state or '?'}"
    return (f"  #{p.id} {p.name} — {p.developer or 'developer unknown'} — {loc} — "
           f"{stage_note} — {p.window.value} — score {p.score:.2f} — {tons}")


@mcp.tool
def board_summary() -> str:
    """Snapshot of the whole board: how many active projects by category and by
    window, total estimated pipeline equipment value, how many have a real
    reachable contact, and when the data was last touched. Good first call to
    orient a conversation, or to answer a broad "what's going on" question."""
    from collections import Counter

    from sqlmodel import select

    from app.db import session_scope
    from app.ladder import build_ladders, contact_status
    from app.models import ACTIVE_STATUSES, Project

    with session_scope() as session:
        projects = session.exec(select(Project).where(Project.status.in_(ACTIVE_STATUSES))).all()
        if not projects:
            return "No active projects on the board."

        by_category = Counter(p.category.value for p in projects)
        by_window = Counter(p.window.value for p in projects)
        value_low = sum(p.equipment_value_low or 0 for p in projects)
        value_high = sum(p.equipment_value_high or 0 for p in projects)
        n_valued = sum(1 for p in projects if p.equipment_value_low is not None)

        ladders = build_ladders(session, projects)
        callable_n = sum(
            1 for p in projects
            if contact_status(session, p, ladder=ladders[p.id])["status"] == "contactable"
        )
        last_touched = max((p.updated_at for p in projects), default=None)

    lines = [f"DMG Scout board — {len(projects)} active projects"
            + (f", data as of {last_touched:%Y-%m-%d %H:%M UTC}" if last_touched else ""), ""]
    lines.append("By category: " + ", ".join(
        f"{k} {v}" for k, v in sorted(by_category.items(), key=lambda kv: -kv[1])))
    lines.append("By window: " + ", ".join(
        f"{k} {v}" for k, v in sorted(by_window.items(), key=lambda kv: -kv[1])))
    lines.append("")
    if n_valued:
        lines.append(f"Estimated pipeline equipment value: ${value_low:,.0f}-${value_high:,.0f} "
                     f"across {n_valued} of {len(projects)} projects with a known facility type. "
                     f"The rest have tonnage but no dollar estimate — no facility type stated, "
                     f"so no equipment category to price against.")
    else:
        lines.append("No projects have an estimated equipment value yet.")
    lines.append(f"{callable_n} of {len(projects)} have at least one reachable contact "
                f"(a phone or email) — see who_to_call for the ranked list.")
    return "\n".join(lines)


@mcp.tool
def search_projects(
    county: str | None = None,
    state: str | None = None,
    stage: str | None = None,
    window: str | None = None,
    category: str | None = None,
    min_score: float | None = None,
    min_tons: float | None = None,
) -> str:
    """Filtered, ranked shortlist of active projects — every filter is
    optional, combine as many as you like. Highest priority score first,
    capped at 20 rows (says how many more matched if it had to cut). Each row
    carries the project's ID — follow up with get_project(project_id) for the
    full picture.

    stage: concept | entitlement | design | permitting | procurement |
    construction | operating
    window: PRE_BOD | IN_BOD | POST_BOD | OPERATING
    category: data_center | industrial | esco | other
    """
    from sqlmodel import select

    from app.db import session_scope
    from app.models import ACTIVE_STATUSES, Category, Project, Stage, Window
    from app.normalize import normalize_county
    from app.staleness import stage_ages

    with session_scope() as session:
        q = select(Project).where(Project.status.in_(ACTIVE_STATUSES))
        if county:
            q = q.where(Project.county == normalize_county(county))
        if state:
            q = q.where(Project.state == state.strip().upper())
        if stage:
            try:
                q = q.where(Project.stage == Stage(stage))
            except ValueError:
                return f"Unknown stage {stage!r}. Valid: {', '.join(s.value for s in Stage)}"
        if window:
            try:
                q = q.where(Project.window == Window(window))
            except ValueError:
                return f"Unknown window {window!r}. Valid: {', '.join(w.value for w in Window)}"
        if category:
            try:
                q = q.where(Project.category == Category(category))
            except ValueError:
                return f"Unknown category {category!r}. Valid: {', '.join(c.value for c in Category)}"
        if min_score is not None:
            q = q.where(Project.score >= min_score)

        projects = session.exec(q.order_by(Project.score.desc())).all()
        if min_tons is not None:
            projects = [p for p in projects if (p.tons_estimate_high or 0) >= min_tons]
        if not projects:
            return "No projects matched those filters."

        ages = stage_ages(session, projects)
        total = len(projects)
        shown = projects[:20]
        lines = [f"{total} project(s) matched" + (f", showing top {len(shown)}:" if total > 20 else ":")]
        for p in shown:
            lines.append(_project_row(p, ages.get(p.id)))
        if total > 20:
            lines.append(f"... {total - 20} more not shown — narrow the filters to see them.")
        return "\n".join(lines)


@mcp.tool
def get_project(project_id: int) -> str:
    """Full briefing on one project: developer, location, stage with how old
    that evidence is, tonnage with its basis and confidence, every signal in
    the timeline with its source URL, and the contact ladder ranked by
    reachability. Use search_projects or who_to_call first to find the
    project_id."""
    from app.brief import build_brief
    from app.db import session_scope
    from app.ladder import build_ladder
    from app.staleness import stage_ages

    with session_scope() as session:
        try:
            b = build_brief(session, project_id)
        except ValueError:
            return f"No project #{project_id}."
        p = b["project"]
        ladder = build_ladder(session, p)
        age = stage_ages(session, [p]).get(p.id)

        lines = [f"#{p.id} {p.name} ({p.status})"]
        lines.append(f"{p.developer or 'developer unknown'} — {p.county or '?'} Co, {p.state or '?'}"
                     + ("" if p.in_territory else " [OUT OF TERRITORY]"))
        lines.append("")

        stage_line = f"Stage: {p.stage.value}"
        if age and age.days is not None:
            if age.unverified():
                stage_line += f" — UNVERIFIED, last evidenced {age.label} ago"
                if not age.dated_from_event:
                    stage_line += " (dated from when we recorded it, not a filing date)"
            else:
                stage_line += f" — confirmed {age.label} ago"
        else:
            stage_line += " — no dated evidence on file"
        lines.append(stage_line)
        dtb = ""
        if p.days_to_estimated_bid is not None:
            dtb = f", ~{p.days_to_estimated_bid} days to estimated bid"
            if p.days_to_estimated_bid_low is not None:
                dtb += f" (95% CI {p.days_to_estimated_bid_low}-{p.days_to_estimated_bid_high})"
        lines.append(f"Window: {p.window.value}" + dtb)
        lines.append(f"Priority score: {p.score:.2f}")
        lines.append("")

        if p.tons_estimate_low:
            conf = " — LOW CONFIDENCE" if p.estimate_low_confidence else ""
            lines.append(f"Tonnage: {p.tons_estimate_low:,.0f}-{p.tons_estimate_high:,.0f}{conf}")
            lines.append(f"Basis: {p.estimate_basis}")
        else:
            lines.append("Tonnage: not yet estimated.")
        if p.equipment_value_low:
            lines.append(f"Estimated equipment value: ${p.equipment_value_low:,.0f}-"
                         f"${p.equipment_value_high:,.0f} ({p.equipment_value_basis})")
        lines.append("")

        lines.append(f"Engineer of record: {b['engineer_of_record'] or 'not yet identified'}")
        lines.append(f"General contractor: {b['gc'] or 'not yet identified'}")
        lines.append("")

        lines.append("Contact ladder (best first):")
        if ladder:
            for r in ladder[:8]:
                reach = ", ".join(x for x in (r.get("phone"), r.get("email")) if x) or "no contact info"
                who = r["name"] + (f" ({r['title']})" if r.get("title") else "")
                src = f" [{r['source_url']}]" if r.get("source_url") else ""
                lines.append(f"  rung {r['rung']} ({r['rung_label']}): {who} — {reach}{src}")
            if len(ladder) > 8:
                lines.append(f"  ... {len(ladder) - 8} more rungs not shown.")
        else:
            lines.append("  NO CONTACT AT ANY RUNG — this project is reading, not a call.")
        lines.append("")

        lines.append(f"Signal timeline ({len(b['timeline'])} signal(s)):")
        for t in b["timeline"][:15]:
            url = f" — {t['url']}" if t["url"] else ""
            lines.append(f"  {t['date']:%Y-%m-%d} [{t['type']}] {t['summary']}{url}")
        if len(b["timeline"]) > 15:
            lines.append(f"  ... {len(b['timeline']) - 15} more not shown.")

        if p.notes:
            lines += ["", f"Notes: {p.notes}"]
        if p.next_action:
            lines.append(f"Next action: {p.next_action}")
        if b["outreach"]:
            lines += ["", "Outreach log:"]
            for o in b["outreach"][:10]:
                na = f" → next: {o.next_action}" if o.next_action else ""
                lines.append(f"  {o.date:%Y-%m-%d} ({o.channel}) {o.notes}{na}")

        return "\n".join(lines)


@mcp.tool
def who_to_call(n: int = 5, county: str | None = None, state: str | None = None) -> str:
    """The top N active projects with a real, reachable human attached,
    ranked by priority score — use this for "who should I call this week".
    Each entry names the person, their role/rung, phone and email, and one
    line on why the project matters. Projects with no callable contact are
    skipped entirely; use search_projects if you want to see those too.
    Also lists in-territory OPSC school funding rows (county-filtered same
    as the project list; state filter other than CA/blank suppresses them,
    since OPSC is California-only) — these have no named human contact, so
    the call target is the district itself, not a person."""
    from sqlmodel import select

    from app.db import session_scope
    from app.ladder import build_ladders, contact_status
    from app.models import ACTIVE_STATUSES, Project
    from app.normalize import normalize_county

    from app.call_target import (
        CALL_TARGET_LABELS, engineer_of_record_by_project, gc_by_project,
        nearby_contractor_by_project, project_call_target,
    )
    from app.config import load_config

    with session_scope() as session:
        cfg = load_config()
        q = select(Project).where(Project.status.in_(ACTIVE_STATUSES))
        if county:
            q = q.where(Project.county == normalize_county(county))
        if state:
            q = q.where(Project.state == state.strip().upper())
        projects = session.exec(q.order_by(Project.score.desc())).all()

        picked = []
        if projects:
            ladders = build_ladders(session, projects)
            for p in projects:
                cs = contact_status(session, p, ladder=ladders[p.id])
                if cs["status"] != "contactable":
                    continue
                picked.append((p, cs["best_reachable"]))
                if len(picked) >= n:
                    break

        if not projects:
            lines = ["No active projects matched."]
        elif not picked:
            lines = ["No project in scope has a reachable contact right now."]
        else:
            picked_projects = [p for p, _ in picked]
            eor_map = engineer_of_record_by_project(session, [p.id for p in picked_projects])
            gc_map = gc_by_project(session, [p.id for p in picked_projects])
            nearby_map = nearby_contractor_by_project(session, cfg, picked_projects)

            lines = [f"Top {len(picked)} to call:"]
            for p, contact in picked:
                reach = ", ".join(x for x in (contact.get("phone"), contact.get("email")) if x)
                who = contact["name"] + (f" ({contact['title']})" if contact.get("title") else "")
                tons = (f"{p.tons_estimate_low:,.0f}-{p.tons_estimate_high:,.0f} tons"
                       if p.tons_estimate_low else "size unknown")
                ct = project_call_target(cfg, p, engineer_of_record=eor_map.get(p.id),
                                         gc=gc_map.get(p.id), nearby_contractor=nearby_map.get(p.id))
                lines.append(
                    f"\n#{p.id} {p.name} ({p.county or '?'} Co, {p.state or '?'}) — "
                    f"score {p.score:.2f}, {p.window.value}, {tons}\n"
                    f"  Call: {who} — {reach} — {contact['rung_label']}"
                    + (f" [{contact['source_url']}]" if contact.get("source_url") else "")
                    + f"\n  Call target: {CALL_TARGET_LABELS[ct.target]}"
                    + (f" — {ct.who_label}" if ct.who_label else "")
                    + f" ({ct.rule}: {ct.reason})"
                )

        from app.pipeline.opsc import schools_board
        school_rows = [] if (state and state.strip().upper() != "CA") else schools_board(session, cfg, county=county)
        if school_rows:
            lines.append(f"\nOPSC school funding (no named contact — call target is the district itself):")
            for d in school_rows[:5]:
                r = d["row"]
                ct = d["call_target"]
                amt = (f"${r.state_share_of_funding:,.0f}" if r.state_share_of_funding is not None
                      else "amount unknown")
                lines.append(
                    f"\n  #{r.id} {r.district or '(no district)'} — {r.school_name or '(no school)'} "
                    f"({r.county or '?'} Co) — {r.program or '?'}, {r.status or '?'}, {amt}\n"
                    f"    Call target: {CALL_TARGET_LABELS[ct.target]}"
                    + (f" — {ct.who_label}" if ct.who_label else "")
                    + f" ({ct.rule}: {ct.reason})"
                )
            if len(school_rows) > 5:
                lines.append(f"\n  ... {len(school_rows) - 5} more in-territory school row(s) not shown; "
                            f"see /board?view=schools.")

        return "\n".join(lines)


@mcp.tool
def get_account(account_id: int | None = None, name: str | None = None) -> str:
    """Account brief: coverage (what they buy), ranked gaps with estimated
    dollar values, service-life replacement windows with dates, and any live
    Scout project naming them. Pass either account_id or name (a substring
    match — if more than one account matches, none is guessed; you get the
    candidate list back instead)."""
    from sqlmodel import select

    from app.accounts import build_account_brief
    from app.config import load_config
    from app.db import session_scope
    from app.models import Account
    from app.normalize import normalize_name

    with session_scope() as session:
        if account_id is None and not name:
            return "Give either account_id or name."
        if account_id is None:
            norm = normalize_name(name)
            candidates = (session.exec(
                select(Account).where(Account.name_norm.contains(norm))).all() if norm else [])
            if not candidates:
                return f"No account matching {name!r}."
            if len(candidates) > 1:
                lines = [f"{len(candidates)} accounts match {name!r} — say which one:"]
                for a in candidates[:15]:
                    lines.append(f"  #{a.id} {a.name} ({a.account_type})")
                if len(candidates) > 15:
                    lines.append(f"  ... {len(candidates) - 15} more not shown.")
                return "\n".join(lines)
            account_id = candidates[0].id

        try:
            brief = build_account_brief(session, load_config(), account_id)
        except ValueError:
            return f"No account #{account_id}."
        a = brief.account

        lines = [f"#{a.id} {a.name} — {a.account_type}, {a.ownership_type}"
                + (f" — rep: {a.assigned_rep}" if a.assigned_rep else "")]
        if a.county or a.state:
            lines.append(f"{a.county or '?'} Co, {a.state or '?'}")
        lines.append("")

        counts = brief.coverage["counts"]
        lines.append(f"Coverage: {counts.get('bought', 0)} bought, "
                     f"{counts.get('quoted_not_won', 0)} quoted-not-won, "
                     f"{counts.get('never_quoted', 0)} never quoted, "
                     f"{counts.get('unknown', 0)} unknown.")
        lines.append("")

        if brief.gaps:
            lines.append("Top gaps (ranked by relevance x estimated dollar value):")
            for g in brief.gaps[:10]:
                lines.append(f"  {g.line.name} ({g.line.firm}) — {g.coverage_status}, "
                            f"~${g.value_low:,.0f}-${g.value_high:,.0f}, relevance {g.relevance:.2f}")
            if len(brief.gaps) > 10:
                lines.append(f"  ... {len(brief.gaps) - 10} more not shown.")
        else:
            lines.append("No gaps above the relevance floor.")
        lines.append("")

        if brief.replacement_windows:
            lines.append("Replacement windows (most urgent first):")
            for w in brief.replacement_windows[:10]:
                lines.append(f"  {w.line.name}: installed {w.install_year}, {w.status.upper()} "
                            f"({w.basis}), window {w.window_start_year}-{w.window_end_year}")
        else:
            lines.append("No known replacement windows (no install year on file, or nothing due).")

        if brief.live_projects:
            lines += ["", "Live Scout projects naming this account:"]
            for lp in brief.live_projects:
                p = lp["project"]
                lines.append(f"  #{p.id} {p.name} ({lp['role']}) — score {p.score:.2f}")

        return "\n".join(lines)


@mcp.tool
def get_account_page(account_id: int | None = None, name: str | None = None) -> str:
    """The pre-meeting page, for the phone: CSLB license detail (status,
    classifications, expiration, bond, workers comp, Local 250 -- ambiguous
    matches show every tied candidate, never a guess), the 13-role
    whitespace view, overdue retrofit buildings near the license's
    geocoded address (the reason to take the meeting), any active Scout
    project naming them, and outreach history. Different from get_account
    (the dollar-ranked-gaps printable brief) -- this is what to pull up
    walking into a call. Pass either account_id or name (a substring match
    — if more than one account matches, none is guessed; you get the
    candidate list back instead)."""
    from sqlmodel import select

    from app.accounts import build_account_page
    from app.config import load_config
    from app.db import session_scope
    from app.models import Account
    from app.normalize import normalize_name

    with session_scope() as session:
        if account_id is None and not name:
            return "Give either account_id or name."
        if account_id is None:
            norm = normalize_name(name)
            candidates = (session.exec(
                select(Account).where(Account.name_norm.contains(norm))).all() if norm else [])
            if not candidates:
                return f"No account matching {name!r}."
            if len(candidates) > 1:
                lines = [f"{len(candidates)} accounts match {name!r} — say which one:"]
                for a in candidates[:15]:
                    lines.append(f"  #{a.id} {a.name} ({a.account_type})")
                if len(candidates) > 15:
                    lines.append(f"  ... {len(candidates) - 15} more not shown.")
                return "\n".join(lines)
            account_id = candidates[0].id

        try:
            page = build_account_page(session, load_config(), account_id)
        except ValueError:
            return f"No account #{account_id}."
        a = page.account

        lines = [f"#{a.id} {a.name} — {a.account_type}"
                + (f" — rep: {a.assigned_rep}" if a.assigned_rep else "")]
        if a.county or a.state:
            lines.append(f"{a.county or '?'} Co, {a.state or '?'}")
        lines.append("")

        lines.append("Who they are:")
        cslb, contractor = page.cslb_match, page.cslb_match["contractor"]
        if cslb["ambiguous"]:
            lines.append(f"  {len(cslb['candidates'])} CSLB licenses tied on this name — none applied:")
            for c in cslb["candidates"]:
                lines.append(f"    #{c.license_no} {c.business_name} ({c.city or 'city?'}), "
                            f"{c.primary_status or 'status unknown'}")
        elif contractor is None:
            scope = f" in {a.county} County" if page.cslb_county_scoped else ""
            lines.append(f"  No CSLB license matched this account's name{scope}.")
        else:
            lines.append(f"  CSLB #{contractor.license_no} {contractor.business_name} — "
                        f"{contractor.primary_status or 'status unknown'}, "
                        f"{contractor.classifications or 'no classifications'}, "
                        f"exp {contractor.expiration_date.date() if contractor.expiration_date else 'unknown'}")
            bonded = "bonded" if contractor.bond_number and not contractor.bond_cancellation_date else "not bonded"
            lines.append(f"  {bonded}"
                        + (f" (${contractor.bond_amount:,.0f})" if contractor.bond_amount else "")
                        + f" — workers comp: {contractor.workers_comp_coverage_type or 'unknown'}"
                        + f" — Local 250: {'yes' if contractor.ua_local_250_signatory else 'no'}")
        lines.append("")

        cov = page.role_coverage
        lines.append(f"What we've sold them: {cov['bought_roles']} of {cov['total_roles']} building roles bought.")
        lines.append("")

        lines.append("What I could hand them:")
        if contractor is None:
            lines.append("  No CSLB match, so no geocoded address to search from.")
        elif contractor.latitude is None:
            lines.append(f"  License #{contractor.license_no} has not been geocoded yet.")
        elif not page.overdue_buildings:
            lines.append(f"  0 overdue retrofit buildings within {page.overdue_radius_miles:.0f}mi — a real zero.")
        else:
            lines.append(f"  {len(page.overdue_buildings)} overdue retrofit building(s) within "
                        f"{page.overdue_radius_miles:.0f}mi — this is the reason to take the meeting. Nearest:")
            for row in page.overdue_buildings[:5]:
                b = row["building"]
                lines.append(f"    {row['distance_miles']}mi — {b.address or 'APN ' + b.apn} "
                            f"({b.county} Co) — {b.service_life_years_past:+.0f}yr overdue"
                            if b.service_life_years_past is not None else
                            f"    {row['distance_miles']}mi — {b.address or 'APN ' + b.apn} ({b.county} Co)")
        lines.append("")

        firm = page.firm_match["firm"]
        lines.append("Where they already show up:")
        if firm is None:
            lines.append("  Not on Scout's own firm roster under this name — the common case.")
        elif not page.firm_match["active_projects"]:
            lines.append(f"  On Scout's firm roster as {firm.name} ({firm.firm_type}), no active project right now.")
        else:
            for p, role, stage in page.firm_match["active_projects"]:
                lines.append(f"  #{p.id} {p.name} ({role}, {stage.value}) — score {p.score:.2f}")
        lines.append("")

        lines.append("What we've said to each other:")
        if page.outreach:
            for o in page.outreach[:5]:
                lines.append(f"  {o.date:%Y-%m-%d} {o.channel}: {o.notes}"
                            + (f" — next: {o.next_action}" if o.next_action else ""))
        else:
            lines.append("  No outreach logged for this account yet. Use log_outreach(account_id=...) to add one.")

        return "\n".join(lines)


@mcp.tool
def search_firms(query: str) -> str:
    """Look up a firm by name (substring match): its type, every active Scout
    project it appears on and in what role, and — if it's also a sales
    account — a pointer to get_account for coverage and outreach history."""
    from sqlmodel import select

    from app.db import session_scope
    from app.models import ACTIVE_STATUSES, Account, Firm, Project, ProjectFirm

    with session_scope() as session:
        firms = session.exec(select(Firm).where(Firm.name.ilike(f"%{query}%"))).all()
        if not firms:
            return f"No firm matching {query!r}."
        if len(firms) > 8:
            lines = [f"{len(firms)} firms match {query!r} — narrow it down:"]
            for f in firms[:15]:
                lines.append(f"  {f.name} ({f.firm_type})")
            if len(firms) > 15:
                lines.append(f"  ... {len(firms) - 15} more not shown.")
            return "\n".join(lines)

        out = []
        for f in firms:
            lines = [f"{f.name} — {f.firm_type}"]
            links = session.exec(
                select(ProjectFirm, Project).where(
                    ProjectFirm.firm_id == f.id, Project.id == ProjectFirm.project_id,
                    Project.status.in_(ACTIVE_STATUSES))).all()
            if links:
                lines.append("Active projects:")
                for pf, p in links:
                    lines.append(f"  #{p.id} {p.name} ({pf.role}) — score {p.score:.2f}")
            else:
                lines.append("No active Scout projects.")

            account = session.exec(select(Account).where(Account.firm_id == f.id)).first()
            if account:
                lines.append(f"Also a sales account — use get_account(account_id={account.id}) "
                            f"for coverage and gaps.")
            out.append("\n".join(lines))
        return "\n\n".join(out)


@mcp.tool
def log_outreach(project_id: int | None = None, account_id: int | None = None, notes: str = "",
                 channel: str = "call", next_action: str | None = None,
                 next_action_date: str | None = None) -> str:
    """Record that you talked to someone — the one write tool. Give exactly
    one of project_id (outreach about a specific live Scout project) or
    account_id (outreach about a contractor/GC account generally — most
    accounts have no live project to attach this to at all, see
    get_account_page). channel: call | email | meeting | text | other.
    next_action_date is an ISO date (YYYY-MM-DD) if you have one. Writes to
    the same Outreach log the dashboard's own outreach forms write to;
    nothing here touches the pipeline's tables."""
    from datetime import datetime

    from app.db import session_scope
    from app.models import Account, Project
    from app.outreach import log_outreach as _log_outreach

    if (project_id is None) == (account_id is None):
        return "Give exactly one of project_id or account_id, not both and not neither."

    parsed_date = None
    if next_action_date:
        try:
            parsed_date = datetime.fromisoformat(next_action_date)
        except ValueError:
            return f"Could not parse next_action_date {next_action_date!r} — use YYYY-MM-DD."

    with session_scope() as session:
        if project_id is not None:
            entity = session.get(Project, project_id)
            if entity is None:
                return f"No project #{project_id}."
            label = f"#{entity.id} {entity.name}"
        else:
            entity = session.get(Account, account_id)
            if entity is None:
                return f"No account #{account_id}."
            label = f"#{entity.id} {entity.name}"

        _log_outreach(session, project_id=project_id, account_id=account_id, channel=channel,
                     notes=notes, next_action=next_action, next_action_date=parsed_date)
        confirmation = f"Logged: {channel} on {label} — {notes}"
        if next_action:
            confirmation += f" — next: {next_action}"
            if parsed_date:
                confirmation += f" by {parsed_date:%Y-%m-%d}"
        return confirmation


@mcp.tool
def source_health() -> str:
    """Is the pipeline actually running? Last successful run per source
    (flagged if stale beyond 36 hours by default, or a source's own
    sources.<name>.stale_hours override for a non-daily cadence), plus
    today's and this month's LLM spend against the daily budget. Use this
    if the board looks stale, a number looks off, or projects seem to have
    stopped updating."""
    from app.ops import doctor
    from app.spend import budget_status

    checks = doctor()
    source_checks = [(n, ok, d) for n, ok, d in checks if n.startswith("source:")]
    other_checks = [(n, ok, d) for n, ok, d in checks if not n.startswith("source:")]

    lines = ["Source health:"]
    for name, ok, detail in source_checks:
        lines.append(f"  {name[len('source:'):]}: {'OK' if ok else 'STALE/FAILED'} — {detail}")

    if other_checks:
        lines += ["", "Other checks:"]
        for name, ok, detail in other_checks:
            lines.append(f"  {name}: {'OK' if ok else 'FAIL'} — {detail}")

    st = budget_status()
    flag = " — EXHAUSTED" if st["exhausted"] else " — at warning threshold" if st["warn"] else ""
    lines += ["", f"LLM spend: ${st['today_usd']:.2f} today of ${st['daily_budget_usd']:.2f} "
             f"daily budget (${st['month_usd']:.2f} this month){flag}"]

    from app.precall import precall_cost_report
    pc = precall_cost_report()
    lines += ["", f"Pre-call briefs: {pc['n_briefs']} cached, ${pc['total_cost_usd']:.4f} total "
             f"(this instance's local disk only — see app.precall's module docstring for why "
             f"that cache isn't in Postgres, and doesn't survive a deploy)."]
    return "\n".join(lines)


@mcp.tool
def compare_lines(
    tonnage: float | None = None,
    building_type: str | None = None,
    latent_load_priority: bool = False,
    marine_or_corrosive: bool = False,
    water_available: bool | None = None,
    space_rigging_constrained: bool = False,
    redundancy_required: bool = False,
    buyer_type: str | None = None,
) -> str:
    """Head-to-head line comparison for an application, not the one-line-at-a-
    time view /lines gives. building_type is one of data_center, healthcare,
    industrial_warehouse, education, hospitality, labs, office, multifamily.
    buyer_type is owner_direct or spec_driven.

    A null capability field is never a vote against a line -- it lands in
    that candidate's capability_gaps instead, which is what to call the
    factory about. tonnage sizes what kind of equipment is relevant (cooling
    generation / heat rejection / air handling); no line carries a tonnage
    capacity field, so it never filters or scores candidates directly. Lead
    time is never surfaced, for any line."""
    from app.compare import compare_lines as _compare_lines
    from app.db import session_scope

    with session_scope() as session:
        candidates = _compare_lines(
            session, tonnage=tonnage, building_type=building_type,
            latent_load_priority=latent_load_priority, marine_or_corrosive=marine_or_corrosive,
            water_available=water_available, space_rigging_constrained=space_rigging_constrained,
            redundancy_required=redundancy_required, buyer_type=buyer_type,
        )

    if not candidates:
        return "No candidate lines in a tonnage-relevant role (cooling generation / heat rejection / air handling)."

    lines = [f"{len(candidates)} candidates:"]
    for c in candidates:
        lines.append(f"\n{c.line} ({c.building_role})")
        if c.why_it_fits:
            lines.append("  fits: " + "; ".join(c.why_it_fits))
        if c.trades_away:
            lines.append("  trades away: " + "; ".join(c.trades_away))
        for k, v in c.eligibility_flags.items():
            if v is not None:
                lines.append(f"  {k}: {v.value} (checked {v.checked})")
        if c.competitors is not None:
            lines.append(f"  competitors: {c.competitors.value} (checked {c.competitors.checked})")
        if c.known_limitations:
            lines.append(f"  known limitations: {c.known_limitations}")
        lines.append(f"  capability_gaps: {', '.join(c.capability_gaps) or 'none'}")
    lines.append("\nLead time: not tracked for any line, intentionally omitted.")
    return "\n".join(lines)


@mcp.tool
def get_selection_tool(line_name: str) -> str:
    """Which selection software to use for one line card line, by name
    (e.g. "AAON", "Marley", "TCF/Twin City Fan" -- exact ProductLine.name).
    unchecked means nobody has looked, not that no tool exists -- those are
    deliberately different findings, see app.models.SelectionTool."""
    from sqlmodel import select

    from app.accounts import SELECTION_TOOL_ACCESS_LABELS, SELECTION_TOOL_VERIFICATION_LABELS
    from app.db import session_scope
    from app.models import ProductLine, SelectionTool
    from app.normalize import normalize_name

    with session_scope() as session:
        line = session.exec(
            select(ProductLine).where(ProductLine.name_norm == normalize_name(line_name))).first()
        if line is None:
            return f"No line card entry named {line_name!r}."
        tool = session.exec(select(SelectionTool).where(SelectionTool.product_line_id == line.id)).first()

        if tool is None or tool.verification_status == "unchecked":
            return (f"{line.name} ({line.firm}): selection tool unchecked -- nobody has looked yet. "
                    f"Not the same as 'no tool exists'.")

        if tool.access_level == "none_exists":
            detail = f"{line.name} ({line.firm}): confirmed -- no selection tool exists."
            if tool.what_it_outputs:
                detail += f" {tool.what_it_outputs}"
            return detail

        status_label = SELECTION_TOOL_VERIFICATION_LABELS.get(tool.verification_status, tool.verification_status)
        access = SELECTION_TOOL_ACCESS_LABELS.get(tool.access_level, tool.access_level or "access level varies")
        parts = [f"{line.name} ({line.firm}): {tool.tool_name} [{status_label}]"]
        if tool.vendor_url:
            parts.append(f"— {tool.vendor_url}")
        parts.append(f"\n  access: {access}")
        if tool.produces_submittal_docs is not None:
            parts.append(f"\n  produces submittal docs: {'yes' if tool.produces_submittal_docs else 'no'}")
        if tool.what_it_outputs:
            parts.append(f"\n  {tool.what_it_outputs}")
        if tool.verified_by:
            when = tool.verified_date.strftime("%Y-%m-%d") if tool.verified_date else "date unrecorded"
            parts.append(f"\n  verified by {tool.verified_by}, {when}")
        return " ".join(parts)


@mcp.tool
def pre_call_brief(entity_type: str, entity_id: int, force_refresh: bool = False) -> str:
    """Everything worth knowing before dialing a project or contractor, in one
    page: Scout's own data (the contact ladder, firms, prior outreach, line
    card fit, nearby regulatory triggers -- whichever apply to this entity)
    plus fresh web research on the company and recent news, every web-sourced
    fact cited inline with its URL and retrieval date. entity_type is
    "project", "contractor", or "opsc_project" (use search_projects/
    who_to_call or /contractors to find an id first; who_to_call's OPSC
    section shows opsc_project row ids -- these have no named contact, so
    the brief covers the district/school and web research on it instead).

    Cached per entity after the first call -- opening it again is free and
    instant. Pass force_refresh=True to regenerate (the underlying data may
    have moved since the cache was written). A cache miss costs real money
    (a web-search-enabled Claude call, typically a few cents) -- see
    source_health for the running total across every cached brief."""
    from app.db import session_scope
    from app.precall import PrecallUnavailable
    from app.precall import pre_call_brief as _pre_call_brief
    from app.spend import BudgetExceeded

    if entity_type not in ("project", "contractor", "opsc_project"):
        return f"entity_type must be 'project', 'contractor', or 'opsc_project', got {entity_type!r}."

    with session_scope() as session:
        try:
            entry = _pre_call_brief(session, entity_type, entity_id, force_refresh=force_refresh)
        except ValueError:
            return f"No {entity_type} #{entity_id}."
        except PrecallUnavailable as exc:
            return f"Pre-call brief unavailable: {exc}"
        except BudgetExceeded as exc:
            return f"Pre-call brief unavailable: {exc}"

    status = "cached" if entry["from_cache"] else f"generated just now, cost ${entry['cost_usd']:.4f}"
    header = f"Pre-call brief: {entry['name']} ({entity_type} #{entity_id}) — {status}"
    generated = f"Generated {entry['generated_at_display']}"
    return f"{header}\n{generated}\n\n{entry['text']}"


@mcp.tool
def log_field_intel(reported_by: str, reported_at: str, source_notes: str,
                    owner: str = "", location: str = "", size_scope: str = "",
                    stage: str = "unknown", expected_timing: str = "",
                    engineer_name: str = "", mech_contractor_name: str = "") -> str:
    """Record human-sourced project intelligence -- a GC, engineer, or owner
    naming a job in conversation, before it's any kind of public document.
    Separate from everything else this tool set writes: no triage, no
    extraction, no grounding, because there is no document behind it to
    check. Stays clearly marked unverified until a real filing confirms it
    (see get_field_intel / /intel).

    reported_by (who told you), reported_at (YYYY-MM-DD, when the
    conversation happened), and source_notes (what they said, their words)
    are the only required fields -- that's the source, the same way a URL
    is for everything else. Every other field: leave blank if they didn't
    address it, never guess a value to fill it in.

    engineer_name and mech_contractor_name are checked against Scout's own
    firm and account rosters by exact name match the moment you save --
    the confirmation reply tells you immediately if either name is already
    on Scout's board or your account roster, and what else they're linked
    to."""
    from datetime import datetime

    from app.db import session_scope
    from app.field_intel import create_field_intel

    try:
        reported_at_dt = datetime.strptime(reported_at, "%Y-%m-%d")
    except ValueError:
        return f"reported_at must be YYYY-MM-DD, got {reported_at!r}."

    with session_scope() as session:
        try:
            intel = create_field_intel(
                session, reported_by=reported_by, reported_at=reported_at_dt, source_notes=source_notes,
                owner=owner or None, location=location or None, size_scope=size_scope or None,
                stage=stage or "unknown", expected_timing=expected_timing or None,
                engineer_name=engineer_name or None, mech_contractor_name=mech_contractor_name or None,
            )
        except ValueError as exc:
            return str(exc)

        lines = [f"Logged field intel #{intel.id} — UNVERIFIED, human-sourced.",
                f"  From {intel.reported_by}, {intel.reported_at:%Y-%m-%d}: {intel.source_notes}"]
        if intel.owner or intel.location:
            lines.append(f"  {intel.owner or 'owner?'} — {intel.location or 'location?'}")
        if intel.engineer_name:
            if intel.engineer_firm_id or intel.engineer_account_id:
                lines.append(f"  Engineer {intel.engineer_name} — ALREADY on Scout's roster.")
            else:
                lines.append(f"  Engineer {intel.engineer_name} — not on Scout's roster yet.")
        if intel.mech_contractor_name:
            if intel.mech_contractor_firm_id or intel.mech_contractor_account_id:
                lines.append(f"  Mech sub {intel.mech_contractor_name} — ALREADY on Scout's roster "
                            f"— see get_account_page or /intel/{intel.id} for what else they're on.")
            else:
                lines.append(f"  Mech sub {intel.mech_contractor_name} — not on Scout's roster yet.")
        return "\n".join(lines)
