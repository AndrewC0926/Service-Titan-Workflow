"""Manual signal entry — first-class signals no scraper can see: an engineer
changing firms, a prequal invite from ACCO or Southland, a tip from a contact.
Used by both the CLI and the dashboard form."""
from __future__ import annotations

import hashlib

from sqlmodel import Session

from app.models import RawDocument, Signal, SignalType, Stage, TriageResult, utcnow


def add_manual_signal(
    session: Session,
    signal_type: str,
    summary: str,
    project_name: str | None = None,
    developer: str | None = None,
    county: str | None = None,
    state: str | None = None,
    mw_it: float | None = None,
    mw_total: float | None = None,
    stage: str = "unknown",
    person_name: str | None = None,
    person_org: str | None = None,
    url: str = "",
) -> Signal:
    st = SignalType(signal_type)
    text = (
        f"MANUAL ENTRY ({st.value})\n{summary}\n"
        f"Project: {project_name or '-'} | Developer: {developer or '-'} | "
        f"County: {county or '-'} {state or ''}"
    )
    uid = hashlib.sha256(f"{st.value}:{summary}:{project_name}".encode()).hexdigest()[:24]
    doc = RawDocument(
        source="manual", source_uid=uid, url=url or "manual://entry",
        title=f"Manual: {summary[:120]}", raw_text=text,
        content_hash=hashlib.sha256(text.encode()).hexdigest(),
        triage_result=TriageResult.relevant, triage_reason="manual entry",
        processed_at=utcnow(), meta={"default_signal_type": st.value},
    )
    session.add(doc)
    session.flush()
    signal = Signal(
        raw_document_id=doc.id, signal_type=st, project_name=project_name,
        developer_or_owner=developer, county=county, state=state,
        mw_it=mw_it, mw_total=mw_total, stage=Stage(stage),
        summary_one_line=summary[:300], confidence=1.0,
        named_people=([{"name": person_name, "org": person_org}] if person_name else []),
    )
    session.add(signal)
    session.commit()
    session.refresh(signal)
    return signal
