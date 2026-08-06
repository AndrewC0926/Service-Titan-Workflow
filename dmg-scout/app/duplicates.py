"""Standing duplicate check over the project table.

This exists because a resolver regression is invisible from the outside. When
CEQAnet's APN field changed punctuation between two filings of the same project,
`pair_similarity` scored the disagreement at its heaviest weight, four projects
with byte-identical names split into eight rows, and every downstream number —
board size, contactability rate, coverage — was quietly computed over inflated
counts. Nothing failed. The board just got longer.

So the check is not a diagnostic someone remembers to run. It runs after every
resolve and reports into source health, on the principle that a silently
fragmenting board should be as loud as a dead adapter.

Two rules, cheapest first:

  * shared SCH number — CEQAnet assigns one per project and reuses it across the
    filing series, so two active projects sharing one is a definitional duplicate
  * shared normalized name + county — catches sources with no SCH at all, and
    catches SCH parsing having broken upstream
"""
from __future__ import annotations

import re
from collections import defaultdict

from sqlmodel import Session, select

from app.models import ACTIVE_STATUSES, Project
from app.normalize import normalize_county, normalize_name

# Generic words that make unrelated rows collide ("El Camino Project" vs
# "El Camino"), stripped before comparing.
_FILLER = re.compile(r"\b(project|the|a|an)\b")


def name_key(name: str | None) -> str:
    if not name:
        return ""
    return re.sub(r"\s+", " ", _FILLER.sub("", normalize_name(name))).strip()


def find_duplicates(session: Session) -> dict:
    """Active project rows that are probably the same project.

    Returns groups under 'by_sch' and 'by_name_county'. A group is a list of
    project dicts, so callers can render it without re-querying.
    """
    projects = [p for p in session.exec(select(Project)).all()
                if p.status in ACTIVE_STATUSES]

    by_sch: dict[str, list[Project]] = defaultdict(list)
    by_name: dict[tuple[str, str], list[Project]] = defaultdict(list)
    for p in projects:
        if p.sch_number and p.sch_number.strip():
            by_sch[p.sch_number.strip()].append(p)
        key = name_key(p.name)
        # "Unnamed project (Storey)" rows are placeholders, not duplicates of one
        # another — grouping them would report permanent noise.
        if key and not (p.name or "").startswith("Unnamed"):
            by_name[(key, normalize_county(p.county) or "")].append(p)

    def render(group: list[Project]) -> list[dict]:
        return [{"id": p.id, "name": p.name, "county": p.county,
                 "category": p.category.value if p.category else None,
                 "score": p.score, "sch_number": p.sch_number,
                 "apn_parcel": p.apn_parcel} for p in sorted(group, key=lambda x: x.id)]

    sch_groups = {k: render(v) for k, v in by_sch.items() if len(v) > 1}
    # A pair already caught by SCH is not reported twice.
    flagged = {p["id"] for g in sch_groups.values() for p in g}
    name_groups = {f"{k[0]}|{k[1]}": render(v) for k, v in by_name.items()
                   if len(v) > 1 and not {p.id for p in v} <= flagged}

    dup_rows = len({p["id"] for g in sch_groups.values() for p in g}
                   | {p["id"] for g in name_groups.values() for p in g})
    return {
        "n_projects": len(projects),
        "by_sch": sch_groups,
        "by_name_county": name_groups,
        "n_groups": len(sch_groups) + len(name_groups),
        "n_duplicate_rows": dup_rows,
        # What the board size would be if every group collapsed to one row.
        "n_excess_rows": dup_rows - (len(sch_groups) + len(name_groups)) if dup_rows else 0,
    }


def duplicates_text(report: dict) -> str:
    lines = [(f"Duplicate projects — {report['n_projects']} active rows, "
              f"{report['n_groups']} suspect groups, "
              f"{report['n_excess_rows']} excess rows")]
    for label, groups in (("shared SCH number", report["by_sch"]),
                          ("shared name + county", report["by_name_county"])):
        if not groups:
            continue
        lines.append(f"\n  {label}: {len(groups)} groups")
        for key, group in list(groups.items())[:20]:
            lines.append(f"    [{len(group)}x] {group[0]['name'][:62]}")
            for p in group:
                lines.append(f"        #{p['id']:<5} score={p['score']:5.2f} "
                             f"county={p['county']} sch={p['sch_number']} "
                             f"apn={(p['apn_parcel'] or '')[:34]}")
    if not report["n_groups"]:
        lines.append("  none")
    return "\n".join(lines)
