"""Outcome feedback loop: record what actually happened, then report which
signal types preceded conversions versus deaths. Scoring weights get tuned from
this data once there's enough of it — capture starts now."""
from __future__ import annotations

from collections import defaultdict

from sqlmodel import Session, select

from app.models import (
    OUTCOME_STATUSES, OutcomeEvent, Project, ProjectSignal, Signal, utcnow,
)

CONVERTED = {"specified", "won"}
DIED = {"lost", "dead"}


def record_outcome(session: Session, project_id: int, status: str, reason: str = "") -> OutcomeEvent:
    if status not in OUTCOME_STATUSES:
        raise ValueError(f"status must be one of {OUTCOME_STATUSES}")
    project = session.get(Project, project_id)
    if project is None:
        raise ValueError(f"no project {project_id}")
    event = OutcomeEvent(project_id=project_id, status=status, reason=reason)
    project.status = status
    project.updated_at = utcnow()
    session.add(event)
    session.add(project)
    session.commit()
    session.refresh(event)
    return event


def outcomes_report(session: Session) -> dict:
    """Signal-type counts for converted vs died projects, plus per-type conversion
    rate. Honest about sample size — this is noise until there are dozens of rows."""
    latest: dict[int, OutcomeEvent] = {}
    for ev in session.exec(select(OutcomeEvent).order_by(OutcomeEvent.created_at)).all():
        latest[ev.project_id] = ev  # last event wins

    converted_ids = {pid for pid, ev in latest.items() if ev.status in CONVERTED}
    died_ids = {pid for pid, ev in latest.items() if ev.status in DIED}

    def signal_types(project_ids: set[int]) -> dict[str, int]:
        counts: dict[str, int] = defaultdict(int)
        for pid in project_ids:
            links = session.exec(
                select(ProjectSignal).where(ProjectSignal.project_id == pid)).all()
            types = set()
            for link in links:
                s = session.get(Signal, link.signal_id)
                if s:
                    types.add(s.signal_type.value)
            for t in types:
                counts[t] += 1  # count each type once per project
        return dict(counts)

    conv_counts = signal_types(converted_ids)
    died_counts = signal_types(died_ids)
    all_types = sorted(set(conv_counts) | set(died_counts))
    rows = []
    for t in all_types:
        c, d = conv_counts.get(t, 0), died_counts.get(t, 0)
        rows.append({"signal_type": t, "converted": c, "died": d,
                     "conversion_rate": round(c / (c + d), 2) if c + d else None})
    return {
        "n_converted": len(converted_ids), "n_died": len(died_ids),
        "n_open": len(latest) - len(converted_ids) - len(died_ids),
        "rows": rows,
        "reliable": len(converted_ids) + len(died_ids) >= 20,
    }


def report_text(r: dict) -> str:
    lines = [f"Outcomes: {r['n_converted']} converted, {r['n_died']} died, {r['n_open']} in flight"]
    if not r["reliable"]:
        lines.append("(fewer than 20 closed outcomes — directional at best, do not retune scoring yet)")
    lines.append("")
    lines.append(f"{'signal type':24s} {'converted':>9s} {'died':>5s} {'conv rate':>9s}")
    for row in r["rows"]:
        rate = f"{row['conversion_rate']:.2f}" if row["conversion_rate"] is not None else "  —"
        lines.append(f"{row['signal_type']:24s} {row['converted']:>9d} {row['died']:>5d} {rate:>9s}")
    if not r["rows"]:
        lines.append("(no closed outcomes recorded yet)")
    return "\n".join(lines)
