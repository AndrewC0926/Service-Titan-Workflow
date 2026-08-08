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

from rapidfuzz import fuzz
from sqlmodel import Session, select

from app.models import (
    ACTIVE_STATUSES, Contact, Firm, Project, ProjectContact, ProjectFirm, ProjectSignal,
    RawDocument, Signal,
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


# Firm.firm_type defaults to this and it means "nobody has classified this firm".
# It is the ABSENCE of a type, not a type, and the difference is not cosmetic —
# see firm_type_index.
UNTYPED_FIRM = "unknown"


def firm_type_index(session: Session) -> dict[str, str]:
    """{normalized firm name or alias: firm_type}, in one query.

    This used to be a linear scan of the whole Firm table per PERSON, inside a
    loop over signals, inside a loop over projects. Rendering the board's contact
    column for 289 projects took minutes. Same answer, one query: firms are
    iterated in table order and each key keeps its first match, which is exactly
    what the old `return` on first hit did.

    Firms typed `unknown` are LEFT OUT, because "unknown" is a sentinel meaning
    the firm was never classified, and 56 of the 400 firms carry it. Returning
    the string made it truthy, and rung 7's fallback — "a person with a phone,
    named in a public filing, whose organisation we cannot classify" — is guarded
    by `not org_type`. So the one case the fallback exists to catch was the exact
    case it rejected. That is how the California Energy Commission project
    manager on the Vernon filing, with a direct line printed next to her name,
    produced a board row reading "No one".
    """
    index: dict[str, str] = {}
    for firm in session.exec(select(Firm)).all():
        if not firm.firm_type or firm.firm_type == UNTYPED_FIRM:
            continue
        index.setdefault(firm.name_norm, firm.firm_type)
        for alias in firm.aliases or []:
            index.setdefault(normalize_name(alias), firm.firm_type)
    return index


def _same_org(a_norm: str, b_norm: str | None) -> bool:
    """Is this the same company, allowing for how filings write a parent company?

    Exact equality on normalized names was the test, and it fails on the shape
    these fields actually take: the project's developer is recorded as "GIC Vernon
    LLC (Goodman)" while the filing names the person's employer as "GIC Vernon
    LLC". Those normalize to "gic vernon goodman" and "gic vernon", which are not
    equal, so the developer's own regional director did not register as working
    for the developer. token_set_ratio scores a subset at 100, which is the
    relationship a parenthetical parent actually has to the name it qualifies.
    """
    if not a_norm or not b_norm:
        return False
    if a_norm == b_norm:
        return True
    return fuzz.token_set_ratio(a_norm, b_norm) >= 90


def _classify_person(firm_types: dict[str, str], person: dict, doc_source: str,
                     developer_norm: str | None) -> int | None:
    title = person.get("title") or ""
    org = person.get("org") or ""
    org_norm = normalize_name(org) if org else ""
    org_type = firm_types.get(org_norm) if org_norm else None
    at_developer = _same_org(org_norm, developer_norm)

    if org_type == "mep" or (_MECH_ENG.search(title) and org_type != "gc"):
        return 1
    if org_type == "gc" and _PRECON.search(title):
        return 4
    if at_developer and _DEV_CM.search(title):
        return 5
    if org_type == "consultant" or _CONSULTANT.search(title):
        return 6
    # A named human AT THE DEVELOPER, whatever their title. The rung-5 test above
    # additionally requires a construction/MEP-manager title, which is the person
    # you most want — but requiring it meant everyone else at the developer fell
    # through every remaining rung and off the ladder. The developer's regional
    # director, with a mobile and an email on the filing, is not "nobody to call";
    # they are one question away from who is designing it, which is the whole
    # criterion in section 1 of the charter.
    if at_developer:
        return 5
    if _PLANNER.search(title) or (doc_source in ("ceqanet", "legistar", "goed")
                                  and person.get("phone") and not org_type):
        return 7
    return None


def build_ladders(session: Session, projects: list[Project]) -> dict[int, list[dict]]:
    """Ladders for many projects in a fixed number of queries.

    The per-project builder issued a query per signal, per document and per person
    on top of a full Firm scan for every person. That is fine for one project
    detail page and hopeless for a 289-row board, which is why the board never had
    a contact column despite a callable human being the entire point of the
    product. Everything is loaded up front here and the rung logic is unchanged.
    """
    if not projects:
        return {}
    ids = [p.id for p in projects]
    firm_types = firm_type_index(session)

    links = session.exec(
        select(ProjectSignal).where(ProjectSignal.project_id.in_(ids))).all()
    links_by_project: dict[int, list[ProjectSignal]] = {}
    for link in links:
        links_by_project.setdefault(link.project_id, []).append(link)

    signal_ids = [link.signal_id for link in links]
    signals = {s.id: s for s in session.exec(
        select(Signal).where(Signal.id.in_(signal_ids))).all()} if signal_ids else {}
    doc_ids = [s.raw_document_id for s in signals.values() if s.raw_document_id]
    docs = {d.id: d for d in session.exec(
        select(RawDocument).where(RawDocument.id.in_(doc_ids))).all()} if doc_ids else {}

    firms_by_project: dict[int, list[tuple[ProjectFirm, Firm]]] = {}
    for pf, firm in session.exec(
            select(ProjectFirm, Firm).where(ProjectFirm.project_id.in_(ids),
                                            Firm.id == ProjectFirm.firm_id)).all():
        firms_by_project.setdefault(pf.project_id, []).append((pf, firm))

    # Apollo/Lusha-enriched contacts (see app/enrichment.py), keyed per project
    # by the normalized firm name they were enriched for — a firm-level rung
    # upgrades to this real person instead of a bare company name when present.
    enriched_by_project: dict[int, dict[str, Contact]] = {}
    for pc, contact in session.exec(
            select(ProjectContact, Contact).where(
                ProjectContact.project_id.in_(ids), Contact.id == ProjectContact.contact_id,
                Contact.source.in_(("apollo", "lusha")))).all():
        if contact.company:
            enriched_by_project.setdefault(pc.project_id, {})[normalize_name(contact.company)] = contact

    return {p.id: _ladder_for(p, links_by_project.get(p.id, []), signals, docs,
                              firms_by_project.get(p.id, []), firm_types,
                              enriched_by_project.get(p.id, {}))
            for p in projects}


def build_ladder(session: Session, project: Project) -> list[dict]:
    """All rungs available for a project, best first. See _reachability_first."""
    return build_ladders(session, [project])[project.id]


def _ladder_for(project: Project, links: list[ProjectSignal],
                signals: dict[int, Signal], docs: dict[int, RawDocument],
                firm_rows: list[tuple[ProjectFirm, Firm]],
                firm_types: dict[str, str],
                enriched_by_firm: dict[str, Contact] | None = None) -> list[dict]:
    developer_norm = normalize_name(project.developer) if project.developer else None
    enriched_by_firm = enriched_by_firm or {}

    rungs: list[dict] = []
    seen: set[tuple] = set()

    def add(rung: int, name: str, title: str | None, company: str | None,
            source_url: str | None, kind: str,
            phone: str | None = None, email: str | None = None,
            contact_source: str = "extracted") -> None:
        # People and firms normalize differently: normalize_name is built for
        # companies and would reduce 'Di Wu' to 'wu', merging distinct people.
        norm = normalize_person_name(name) if kind == "person" else normalize_name(name)
        key = (rung, norm)
        if key in seen:
            return
        seen.add(key)
        rungs.append({"rung": rung, "rung_label": RUNG_LABELS[rung], "name": name,
                      "title": title, "company": company, "source_url": source_url,
                      "kind": kind,
                      "phone": (phone or "").strip() or None,
                      "email": (email or "").strip() or None,
                      # extracted = named in a public filing; firm = a bare
                      # company name, no individual; apollo/lusha = pulled
                      # from a paid contact database. Never renders the same
                      # on the board — see board.html/project.html.
                      "contact_source": contact_source})

    # People named in filings
    for link in links:
        s = signals.get(link.signal_id)
        if not s:
            continue
        doc = docs.get(s.raw_document_id) if s.raw_document_id else None
        url = doc.url if doc else None
        src = doc.source if doc else "manual"
        for person in s.named_people or []:
            name = (person.get("name") or "").strip()
            if not name:
                continue
            rung = _classify_person(firm_types, person, src, developer_norm)
            if rung:
                add(rung, name, person.get("title"), person.get("org"), url, "person",
                    phone=person.get("phone"), email=person.get("email"))
        # ATS postings = rung-5 evidence even without a named individual
        if s.signal_type.value == "job_posting" and url:
            company = (doc.meta or {}).get("company") if doc else None
            if company and developer_norm and normalize_name(company) == developer_norm:
                add(5, f"{company} (hiring in geo — see posting)", None, company, url, "evidence")

    # Firms linked to the project fill firm-level rungs — UNLESS an Apollo/
    # Lusha enrichment already put a real named person at that firm, in which
    # case the person replaces the bare company name at the same rung. See
    # app/enrichment.py.
    firm_rung = {"mep": 2, "mech_contractor": 3, "gc": 4, "consultant": 6}
    for pf, firm in firm_rows:
        rung = 2 if pf.role == "engineer_of_record" else firm_rung.get(firm.firm_type)
        if not rung:
            continue
        enriched = enriched_by_firm.get(firm.name_norm)
        if enriched:
            add(rung, enriched.name, enriched.title, firm.name, None, "person",
                phone=enriched.phone, email=enriched.email, contact_source=enriched.source)
        else:
            add(rung, firm.name, None, firm.name, None, "firm", contact_source="firm")
    # Bare developer company name is a LAST resort only — a named consultant or
    # planner from the filing is a better first call than a main line. It exists
    # so no project ever shows zero rungs when a developer is known.
    if not rungs and project.developer:
        add(5, f"{project.developer} (no individual named — company-level)",
            None, project.developer, None, "firm", contact_source="firm")

    rungs.sort(key=_reachability_first)
    return rungs


def reach_tier(rung: dict) -> int:
    """0 = callable today, 1 = a name to research, 2 = no individual at all."""
    if rung["kind"] != "person":
        return 2
    return 0 if (rung.get("phone") or rung.get("email")) else 1


def _reachability_first(rung: dict) -> tuple:
    """Rank a ladder: can I call them, THEN how close are they to the decision.

    Proximity to the mechanical specification decision used to come first, which
    is the right ordering for a research list and the wrong one for a call list.
    It meant the board headlined the person nearest the decision whether or not
    anybody could reach them, and a rep reading the top line got a name and no way
    to use it.

    So reachability is the primary key. A rung-7 lead agency planner with a direct
    line outranks a rung-1 mechanical engineer of record with nothing but a name,
    because one of those is a call this morning and the other is an afternoon of
    searching. Proximity still decides everything within a tier, so among people
    you can actually reach the closest to the decision is still first — no
    information is lost, it is just subordinated to being usable.

    Measured on the 289-project board, 2026-08-06:
      before  263 contactable rows, 39 of them (15%) headlining someone with no
              phone and no email while a callable person sat further down
      after   263 contactable rows, 0 such rows, by construction
      cost    27 rows now call someone further from the spec decision, by a mean
              of 0.11 rungs and never more than 2

    That last line is the one to keep an eye on. The trade is nearly free at this
    board's shape because the callable people are mostly rung 6 consultants and
    rung 7 planners anyway — the reorder does not move much because the board has
    little to move. It gets expensive only if rung 1-3 contacts start arriving
    without contact methods, which is what `proximity_given_up` watches for.
    """
    return (reach_tier(rung), rung["rung"], 0 if rung["kind"] == "person" else 1)


def best_contact(session: Session, project: Project) -> dict | None:
    ladder = build_ladder(session, project)
    return ladder[0] if ladder else None


def contact_status(session: Session, project: Project,
                   ladder: list[dict] | None = None) -> dict:
    """Is there a human on this row that a rep could actually call today?

    This is the success criterion as of 2026-08-05, and it deliberately replaces
    "did we find the mechanical engineer" — that target was measured unreachable
    three separate ways, because the owner contracts a prime and the prime picks
    its MEP sub privately. What matters instead is a name attached to a phone or
    an email, at ANY rung.

    Three states, and the middle one is the honest part: a name with no way to
    reach it is not a call, it is a research task. Counting it as coverage is how
    a board of 58 rows looked reachable when it was not.
    """
    ladder = build_ladder(session, project) if ladder is None else ladder
    people = [r for r in ladder if r["kind"] == "person"]
    reachable = [r for r in people if r.get("phone") or r.get("email")]
    # The ladder is now sorted reachability-first, so the callable contact IS the
    # headline: reachable[0] and ladder[0] are the same row whenever one exists.
    best_reachable = reachable[0] if reachable else None
    if best_reachable:
        status = "contactable"
    elif people:
        status = "name_only"
    else:
        status = "none"
    top = ladder[0] if ladder else None

    # What proximity we gave up to get a callable name. This replaces the old
    # top_rung_is_reachable as the interesting diagnostic: that flag now reads
    # True on every contactable row by construction, because the reorder made it
    # so. The live question is no longer "is the headline callable" but "how much
    # further from the specification decision did we have to reach to make it so",
    # which is what Phase B's ranking work should be judged on.
    closest_person_rung = min((r["rung"] for r in people), default=None)
    proximity_given_up = (
        best_reachable["rung"] - closest_person_rung
        if best_reachable and closest_person_rung is not None else 0
    )
    return {
        "status": status,
        "best_reachable": best_reachable,
        "n_people": len(people),
        "n_reachable": len(reachable),
        "top_rung_is_reachable": bool(top and top is best_reachable),
        # Rungs sacrificed for reachability: 0 means the closest person to the
        # decision is also callable, 3 means we are calling someone three rungs
        # further out because nobody nearer left a number.
        "proximity_given_up": proximity_given_up,
        "closest_person_rung": closest_person_rung,
    }


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
    headline_counts: Counter = Counter()
    contact_counts: Counter = Counter()
    contact_by_category: dict[str, Counter] = {}
    per_project = []
    ladders = build_ladders(session, projects)
    for p in projects:
        ladder = ladders[p.id]
        best = ladder[0] if ladder else None
        # TWO different rungs, and conflating them would have quietly rewritten a
        # gate metric. `closest_rung` is how near the board gets to the spec
        # decision; `best["rung"]` is the rung of the contact the board headlines.
        # Before the reachability reorder these were the same number, so `counts`
        # could be keyed on ladder[0]. They are not the same number any more: a
        # project holding a name-only mechanical EOR and a callable planner
        # headlines rung 7 and still reaches rung 1. Keying `counts` on the
        # headline would have dropped that project out of "mechanical EOR named"
        # and made the reorder look like a loss of coverage it did not cause.
        closest_rung = min((r["rung"] for r in ladder), default=None)
        counts[closest_rung] += 1
        by_category.setdefault(p.category.value, Counter())[closest_rung] += 1
        headline_counts[best["rung"] if best else None] += 1
        cs = contact_status(session, p, ladder=ladder)
        contact_counts[cs["status"]] += 1
        contact_by_category.setdefault(p.category.value, Counter())[cs["status"]] += 1
        reach = cs["best_reachable"]
        per_project.append({"project": p.name, "score": p.score, "window": p.window.value,
                            "category": p.category.value,
                            "best_rung": best["rung"] if best else None,
                            "closest_rung": closest_rung,
                            "best_name": best["name"] if best else None,
                            "best_kind": best["kind"] if best else None,
                            "contact_status": cs["status"],
                            "top_rung_is_reachable": cs["top_rung_is_reachable"],
                            "proximity_given_up": cs["proximity_given_up"],
                            "closest_person_rung": cs["closest_person_rung"],
                            "reachable_name": reach["name"] if reach else None,
                            "reachable_title": reach["title"] if reach else None,
                            "reachable_org": reach["company"] if reach else None,
                            "reachable_rung": reach["rung"] if reach else None,
                            "reachable_phone": reach["phone"] if reach else None,
                            "reachable_email": reach["email"] if reach else None})

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
            "headline_counts": dict(headline_counts),
            "by_category": {k: dict(v) for k, v in by_category.items()},
            "contact_counts": dict(contact_counts),
            "contact_by_category": {k: dict(v) for k, v in contact_by_category.items()},
            "per_project": per_project,
            "distinct_contacts": len(concentration),
            "distinct_people": sum(1 for k in concentration if k.startswith("person:")),
            "distinct_firms": sum(1 for k in concentration if k.startswith("firm:")),
            "person_rungs": sum(1 for r in per_project if r["best_kind"] == "person"),
            "firm_rungs": sum(1 for r in per_project if r["best_kind"] == "firm"),
            "top_contacts": [(k.split(":", 1)[1], n) for k, n in concentration.most_common(5)]}


CONTACT_LABELS = {
    "contactable": "name + phone or email  (a call)",
    "name_only":   "name, no contact method (research)",
    "none":        "no human named at all   (reading)",
}


def contactability_text(dist: dict) -> str:
    """The headline number: how many rows a rep can actually dial today.

    Printed above the rung table because the rung table answers a question we
    stopped asking — see contact_status().
    """
    cats = sorted(dist.get("contact_by_category", {}))
    n = max(dist["n_projects"], 1)
    lines = [f"Contactability — {dist['n_projects']} in-territory projects", ""]
    header = f"  {'':<38}" + "".join(f"{c[:12]:>13}" for c in cats) + f"{'TOTAL':>8}{'':>8}"
    lines += [header, "  " + "-" * (len(header) - 2)]
    for key in ("contactable", "name_only", "none"):
        cells = "".join(f"{dist['contact_by_category'][c].get(key, 0):>13}" for c in cats)
        total = dist["contact_counts"].get(key, 0)
        lines.append(f"  {CONTACT_LABELS[key]:<38}{cells}{total:>8}{total / n:>7.0%}")
    lines += ["  " + "-" * (len(header) - 2)]
    totals = "".join(f"{sum(dist['contact_by_category'][c].values()):>13}" for c in cats)
    lines.append(f"  {'projects':<38}{totals}{dist['n_projects']:>8}")
    mism = sum(1 for r in dist["per_project"]
               if r["contact_status"] == "contactable" and not r["top_rung_is_reachable"])
    if mism:
        lines += ["",
                  f"  on {mism} contactable rows the top-ranked rung is NOT the callable one —",
                  "  the board would show a firm while the phone number sits further down"]
    else:
        lines += ["", "  every contactable row headlines the callable person "
                      "(ladder is sorted reachability-first)"]

    # What the reordering costs in proximity to the specification decision. This
    # is the number that matters now: reachability is guaranteed, so the question
    # is how far out we had to reach for it.
    gaps = [r["proximity_given_up"] for r in dist["per_project"]
            if r["contact_status"] == "contactable"]
    if gaps:
        traded = sum(1 for g in gaps if g > 0)
        lines += [
            f"  on {traded} of {len(gaps)} contactable rows the callable person sits further "
            f"from the spec",
            f"  decision than the closest named person (mean {sum(gaps) / len(gaps):.1f} rungs "
            f"given up, max {max(gaps)})",
        ]
    return "\n".join(lines)


def distribution_text(dist: dict) -> str:
    """Proximity to the spec decision — the CLOSEST rung each project reaches.

    Deliberately not the rung of the headline contact. Since the reachability
    reorder those differ, and this table has to keep measuring the same thing it
    measured before the reorder or the gate numbers are not comparable.
    """
    cats = sorted(dist.get("by_category", {}))
    lines = [f"Contact ladder distribution — {dist['n_projects']} in-territory projects",
             "  (closest rung reached, regardless of whether that rung is callable)", ""]
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
