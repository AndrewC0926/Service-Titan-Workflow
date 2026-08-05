"""Best-available-contact ladder (Step 8).

Most CEQA/GOED filings never name the MEP engineer of record — they name the
lead agency planner, the environmental consultant, sometimes the developer's
rep. If the tool only surfaced EORs, most of the board would show no one to
call. So every project gets a ladder, ranked by proximity to the mechanical
specification decision:

  1. mechanical engineer of record (named individual)
  2. MEP firm named without an individual (firm-level target, work the roster)
  3. design-build mechanical contractor on the project (they self-specify)
  4. GC precon lead (person, else firm-level)
  5. developer construction/MEP manager (person, else ATS-posting evidence)
  6. environmental / land-use consultant on the filing
  7. lead agency planner named in the filing (public, reachable, knows who is
     submitting drawings)

Every rung records where the name came from (source URL) so a call can open
with the filing, not a cold guess.
"""
from __future__ import annotations

import re
from collections import Counter

from sqlmodel import Session, select

from app.models import (
    ACTIVE_STATUSES, Firm, Project, ProjectFirm, ProjectSignal, RawDocument, Signal,
)
from app.normalize import normalize_name, normalize_person_name

RUNG_LABELS = {
    1: "mechanical EOR (named)",
    2: "MEP firm (firm-level)",
    3: "design-build mech contractor",
    4: "GC precon",
    5: "developer construction/MEP mgr",
    6: "environmental/land-use consultant",
    7: "lead agency planner",
}

_MECH_ENG = re.compile(r"\b(mechanical|mep)\b.*\b(engineer|principal|director)|\bP\.?E\.?\b", re.I)
_PRECON = re.compile(r"\bpre.?con(struction)?\b|\bestimat", re.I)
_DEV_CM = re.compile(r"\b(construction|mep|design|development)\s+(manager|director|lead)\b", re.I)
_CONSULTANT = re.compile(r"\b(environmental|planning|land.?use)\b.*\b(consultant|analyst|manager)\b"
                         r"|\bconsultant\b", re.I)
_PLANNER = re.compile(r"\bplann(er|ing)\b|\bcommunity development\b|\bclearinghouse\b", re.I)


def _firm_type_of_org(session: Session, org: str | None) -> str | None:
    if not org:
        return None
    norm = normalize_name(org)
    for firm in session.exec(select(Firm)).all():
        if firm.name_norm == norm or any(normalize_name(a) == norm for a in firm.aliases or []):
            return firm.firm_type
    return None


def _classify_person(session: Session, person: dict, doc_source: str,
                     developer_norm: str | None) -> int | None:
    title = person.get("title") or ""
    org = person.get("org") or ""
    org_type = _firm_type_of_org(session, org)

    if org_type == "mep" or (_MECH_ENG.search(title) and org_type != "gc"):
        return 1
    if org_type == "gc" and _PRECON.search(title):
        return 4
    if developer_norm and normalize_name(org) == developer_norm and _DEV_CM.search(title):
        return 5
    if org_type == "consultant" or _CONSULTANT.search(title):
        return 6
    if _PLANNER.search(title) or (doc_source in ("ceqanet", "legistar", "goed")
                                  and person.get("phone") and not org_type):
        return 7
    return None


def build_ladder(session: Session, project: Project) -> list[dict]:
    """All rungs available for a project, best (lowest rung) first."""
    links = session.exec(
        select(ProjectSignal).where(ProjectSignal.project_id == project.id)).all()
    developer_norm = normalize_name(project.developer) if project.developer else None

    rungs: list[dict] = []
    seen: set[tuple] = set()

    def add(rung: int, name: str, title: str | None, company: str | None,
            source_url: str | None, kind: str) -> None:
        # People and firms normalize differently: normalize_name is built for
        # companies and would reduce 'Di Wu' to 'wu', merging distinct people.
        norm = normalize_person_name(name) if kind == "person" else normalize_name(name)
        key = (rung, norm)
        if key in seen:
            return
        seen.add(key)
        rungs.append({"rung": rung, "rung_label": RUNG_LABELS[rung], "name": name,
                      "title": title, "company": company, "source_url": source_url,
                      "kind": kind})

    # People named in filings
    for link in links:
        s = session.get(Signal, link.signal_id)
        if not s:
            continue
        doc = session.get(RawDocument, s.raw_document_id) if s.raw_document_id else None
        url = doc.url if doc else None
        src = doc.source if doc else "manual"
        for person in s.named_people or []:
            name = (person.get("name") or "").strip()
            if not name:
                continue
            rung = _classify_person(session, person, src, developer_norm)
            if rung:
                add(rung, name, person.get("title"), person.get("org"), url, "person")
        # ATS postings = rung-5 evidence even without a named individual
        if s.signal_type.value == "job_posting" and url:
            company = (doc.meta or {}).get("company") if doc else None
            if company and developer_norm and normalize_name(company) == developer_norm:
                add(5, f"{company} (hiring in geo — see posting)", None, company, url, "evidence")

    # Firms linked to the project fill firm-level rungs
    firm_rows = session.exec(
        select(ProjectFirm, Firm).where(ProjectFirm.project_id == project.id,
                                        Firm.id == ProjectFirm.firm_id)).all()
    firm_rung = {"mep": 2, "mech_contractor": 3, "gc": 4, "consultant": 6}
    for pf, firm in firm_rows:
        rung = 2 if pf.role == "engineer_of_record" else firm_rung.get(firm.firm_type)
        if rung:
            add(rung, firm.name, None, firm.name, None, "firm")
    # Bare developer company name is a LAST resort only — a named consultant or
    # planner from the filing is a better first call than a main line. It exists
    # so no project ever shows zero rungs when a developer is known.
    if not rungs and project.developer:
        add(5, f"{project.developer} (no individual named — company-level)",
            None, project.developer, None, "firm")

    rungs.sort(key=lambda r: (r["rung"], 0 if r["kind"] == "person" else 1))
    return rungs


def best_contact(session: Session, project: Project) -> dict | None:
    ladder = build_ladder(session, project)
    return ladder[0] if ladder else None


def ladder_distribution(session: Session) -> dict:
    """The session's most important diagnostic: does this tool generate calls
    or just reading? Distribution of best-available rung across the board.

    Split by category, because the two boards are answered by different filings.
    A CEQA document names a lead agency planner and an environmental consultant; a
    GOED abatement application names an economic-development officer. Neither names
    a mechanical EOR, and a pooled number would hide which board is worse.
    """
    projects = session.exec(
        select(Project).where(Project.status.in_(ACTIVE_STATUSES),
                              Project.in_territory == True)).all()  # noqa: E712
    counts: Counter = Counter()
    by_category: dict[str, Counter] = {}
    per_project = []
    for p in projects:
        best = best_contact(session, p)
        rung = best["rung"] if best else None
        counts[rung] += 1
        by_category.setdefault(p.category.value, Counter())[rung] += 1
        per_project.append({"project": p.name, "score": p.score, "window": p.window.value,
                            "category": p.category.value, "best_rung": rung,
                            "best_name": best["name"] if best else None,
                            "best_kind": best["kind"] if best else None})

    # Rung counts alone flatter the board. 58 projects reaching "a contact" looked
    # like 58 calls; they resolve to a couple of dozen names, and the industrial
    # side concentrates on a handful of GOED economic-development officers who
    # appear on every abatement packet. Counted here, on the person normalizer, so
    # the concentration is part of the diagnostic instead of something computed by
    # hand afterwards — which is how it got read wrong the first time.
    def _identity(row: dict) -> str | None:
        if not row["best_name"]:
            return None
        if row["best_kind"] == "person":
            return "person:" + normalize_person_name(row["best_name"])
        return "firm:" + normalize_name(row["best_name"])

    ids = [i for i in (_identity(r) for r in per_project) if i]
    concentration = Counter(ids)
    return {"counts": dict(counts), "n_projects": len(projects),
            "by_category": {k: dict(v) for k, v in by_category.items()},
            "per_project": per_project,
            "distinct_contacts": len(concentration),
            "distinct_people": sum(1 for k in concentration if k.startswith("person:")),
            "distinct_firms": sum(1 for k in concentration if k.startswith("firm:")),
            "person_rungs": sum(1 for r in per_project if r["best_kind"] == "person"),
            "firm_rungs": sum(1 for r in per_project if r["best_kind"] == "firm"),
            "top_contacts": [(k.split(":", 1)[1], n) for k, n in concentration.most_common(5)]}


def distribution_text(dist: dict) -> str:
    cats = sorted(dist.get("by_category", {}))
    lines = [f"Contact ladder distribution — {dist['n_projects']} in-territory projects", ""]
    header = f"  {'rung':<40}" + "".join(f"{c[:12]:>13}" for c in cats) + f"{'TOTAL':>8}"
    lines += [header, "  " + "-" * (len(header) - 2)]
    for rung in range(1, 8):
        cells = "".join(f"{dist['by_category'][c].get(rung, 0):>13}" for c in cats)
        total = dist["counts"].get(rung, 0)
        lines.append(f"  {rung}. {RUNG_LABELS[rung]:<37}{cells}{total:>8}")
    cells = "".join(f"{dist['by_category'][c].get(None, 0):>13}" for c in cats)
    n_none = dist["counts"].get(None, 0)
    lines.append(f"  {'NO CONTACT AT ANY RUNG':<40}{cells}{n_none:>8}")
    lines += ["  " + "-" * (len(header) - 2)]
    totals = "".join(f"{sum(dist['by_category'][c].values()):>13}" for c in cats)
    lines.append(f"  {'projects':<40}{totals}{dist['n_projects']:>8}")

    n = max(dist["n_projects"], 1)
    reachable = dist["n_projects"] - n_none
    rung1 = dist["counts"].get(1, 0)
    near = sum(dist["counts"].get(r, 0) for r in (1, 2, 3, 4, 5))
    lines += ["", f"  reaches some rung:      {reachable}/{dist['n_projects']} ({reachable / n:.0%})"]
    lines.append(f"  mechanical EOR named:   {rung1}/{dist['n_projects']} ({rung1 / n:.0%})")
    lines.append(f"  rungs 1-5 (near the spec decision): {near}/{dist['n_projects']} ({near / n:.0%})")

    if "distinct_contacts" in dist:
        lines += ["",
                  f"  best contact is a named person on {dist['person_rungs']} projects, "
                  f"a firm on {dist['firm_rungs']}",
                  f"  distinct contacts behind those rows: {dist['distinct_contacts']} "
                  f"({dist['distinct_people']} people, {dist['distinct_firms']} firms)"]
        if dist["top_contacts"]:
            lines.append("  most repeated:")
            for name, count in dist["top_contacts"]:
                if count > 1:
                    lines.append(f"    {count:>3} projects  {name}")
        # The number that decides product-or-reading-list. Rung counts hide it.
        if dist["distinct_contacts"] and reachable:
            lines.append(f"  -> {reachable} reachable rows resolve to "
                         f"{dist['distinct_contacts']} contacts "
                         f"({reachable / dist['distinct_contacts']:.1f} rows each)")
    if n_none:
        lines.append(f"  {n_none} projects have no callable contact at all — those rows "
                     f"are reading, not calls")
    return "\n".join(lines)
