"""Database schema. Every extracted field traces back to a raw_document row with a URL."""
from __future__ import annotations

import enum
from datetime import datetime, timezone

from sqlalchemy import Column, Text, UniqueConstraint
from sqlalchemy.types import JSON
from sqlmodel import Field, SQLModel


def utcnow() -> datetime:
    """Current UTC time, as the one clock the whole codebase uses.

    Computed from the timezone-aware `datetime.now(timezone.utc)` — never the
    deprecated `datetime.utcnow()` — then stripped back to naive.

    Naive is deliberate, not an oversight. Every timestamp column here is
    TIMESTAMP WITHOUT TIME ZONE, and every date an adapter parses out of a
    source (`strptime` on a CEQAnet "Received" date, a Legistar
    MatterIntroDate, an AgendaCenter MMDDYYYY slug) is naive too. Returning an
    aware datetime would make `row_date >= since` raise TypeError across the
    adapters and would need a timestamptz migration to store. So the rule is:
    one clock, UTC, naive, and it lives here.
    """
    return datetime.now(timezone.utc).replace(tzinfo=None)


def from_unix(seconds: float) -> datetime:
    """Naive-UTC datetime from a Unix timestamp (replaces utcfromtimestamp)."""
    return datetime.fromtimestamp(seconds, timezone.utc).replace(tzinfo=None)


# Statuses still worth watching/scoring. Terminal: won | lost | dead | archived.
ACTIVE_STATUSES = ("active", "contacted", "specified", "bidding")
OUTCOME_STATUSES = ("contacted", "specified", "bidding", "won", "lost", "dead")


class SignalType(str, enum.Enum):
    utility_load_request = "utility_load_request"
    land_transfer = "land_transfer"
    abatement_application = "abatement_application"
    ceqa_nop = "ceqa_nop"
    planning_agenda = "planning_agenda"
    job_posting = "job_posting"
    air_permit_atc = "air_permit_atc"
    water_will_serve = "water_will_serve"
    ceqa_deir = "ceqa_deir"
    engineer_move = "engineer_move"
    faa_7460 = "faa_7460"
    abs_issuance = "abs_issuance"
    news_report = "news_report"
    prequal_invite = "prequal_invite"
    bid_invite = "bid_invite"
    manual_tip = "manual_tip"


class Stage(str, enum.Enum):
    concept = "concept"
    entitlement = "entitlement"
    design = "design"
    permitting = "permitting"
    procurement = "procurement"
    construction = "construction"
    operating = "operating"
    unknown = "unknown"


class Window(str, enum.Enum):
    PRE_BOD = "PRE_BOD"
    IN_BOD = "IN_BOD"
    POST_BOD = "POST_BOD"
    OPERATING = "OPERATING"


class TriageResult(str, enum.Enum):
    pending = "pending"
    relevant = "relevant"
    irrelevant = "irrelevant"
    error = "error"


class Category(str, enum.Enum):
    """What kind of building this is, which decides how it gets sized and which
    board it lands on. Two pipelines, one corpus: data centers are the long-cycle
    specialist work, industrial is new/expanding manufacturing, warehouse and
    distribution space that needs RTUs, AHUs, make-up air and VFDs.

    A plant that BUILDS servers is `industrial`, not `data_center` — it needs
    HVAC, it just isn't a computing facility. `other` means triage-negative.
    """
    data_center = "data_center"
    industrial = "industrial"
    other = "other"


class FacilityType(str, enum.Enum):
    """What the building does, at the resolution sizing needs.

    Industrial tonnage comes from floor area, and the sqft-per-ton figure varies
    50x across these — a 100,000 sqft cleanroom and a 100,000 sqft distribution
    warehouse are 700 tons apart. `unknown` is a real answer and gets the full
    span rather than a convenient middle.
    """
    distribution_fulfillment = "distribution_fulfillment"
    warehouse_conditioned = "warehouse_conditioned"
    light_manufacturing = "light_manufacturing"
    heavy_manufacturing = "heavy_manufacturing"
    cleanroom = "cleanroom"
    office_rnd = "office_rnd"
    data_center = "data_center"
    unknown = "unknown"


class RawDocument(SQLModel, table=True):
    __tablename__ = "raw_documents"
    __table_args__ = (UniqueConstraint("source", "source_uid", name="uq_source_uid"),)

    id: int | None = Field(default=None, primary_key=True)
    source: str = Field(index=True)
    source_uid: str = Field(index=True)  # source-native ID (SCH number, accession no, PDF path...)
    url: str
    title: str = ""
    fetched_at: datetime = Field(default_factory=utcnow)
    published_at: datetime | None = None
    content_hash: str = Field(index=True)  # sha256 of raw_text
    raw_text: str = Field(sa_column=Column(Text, nullable=False))
    meta: dict = Field(default_factory=dict, sa_column=Column(JSON, nullable=False, default=dict))
    triage_result: TriageResult = Field(default=TriageResult.pending, index=True)
    triage_reason: str = ""
    processed_at: datetime | None = None  # set when extraction completed


class Signal(SQLModel, table=True):
    __tablename__ = "signals"

    id: int | None = Field(default=None, primary_key=True)
    raw_document_id: int | None = Field(default=None, foreign_key="raw_documents.id", index=True)
    signal_type: SignalType = Field(index=True)
    category: Category = Field(default=Category.other, index=True)
    facility_type: FacilityType = Field(default=FacilityType.unknown, index=True)
    created_at: datetime = Field(default_factory=utcnow)
    event_date: datetime | None = None
    # Extracted fields (null = not stated in source; never inferred)
    project_name: str | None = None
    developer_or_owner: str | None = None
    jurisdiction: str | None = None
    county: str | None = Field(default=None, index=True)
    state: str | None = None
    street_address: str | None = None
    apn_parcel: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    mw_it: float | None = None
    mw_total: float | None = None
    generator_count: int | None = None
    generator_hp_each: float | None = None
    generator_kw_each: float | None = None
    building_sqft: float | None = None
    acres: float | None = None
    building_count: int | None = None
    cooling_type: str | None = None
    water_acre_feet_per_year: float | None = None
    stage: Stage = Field(default=Stage.unknown)
    filing_type: str | None = None
    summary_one_line: str = ""
    confidence: float = 0.0
    extraction_json: dict = Field(default_factory=dict, sa_column=Column(JSON, nullable=False, default=dict))
    named_people: list = Field(default_factory=list, sa_column=Column(JSON, nullable=False, default=list))
    named_firms: list = Field(default_factory=list, sa_column=Column(JSON, nullable=False, default=list))


class Project(SQLModel, table=True):
    __tablename__ = "projects"

    id: int | None = Field(default=None, primary_key=True)
    name: str = Field(index=True)
    category: Category = Field(default=Category.data_center, index=True)
    developer: str | None = None
    county: str | None = Field(default=None, index=True)
    state: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    apn_parcel: str | None = None
    mw_it: float | None = None
    mw_total: float | None = None
    tons_estimate_low: float | None = None
    tons_estimate_high: float | None = None
    estimate_basis: str | None = None  # which input drove the tonnage estimate
    estimate_low_confidence: bool = Field(default=False)
    stage: Stage = Field(default=Stage.unknown, index=True)
    window: Window = Field(default=Window.PRE_BOD)
    score: float = Field(default=0.0, index=True)
    days_to_estimated_bid: int | None = None
    in_territory: bool = Field(default=True, index=True)
    # active | contacted | specified | bidding | won | lost | dead | archived
    status: str = Field(default="active", index=True)
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
    last_signal_at: datetime | None = Field(default=None, index=True)
    notes: str = Field(default="", sa_column=Column(Text, nullable=False, default=""))
    next_action: str | None = None
    next_action_date: datetime | None = None


class ProjectSignal(SQLModel, table=True):
    __tablename__ = "project_signals"
    __table_args__ = (UniqueConstraint("project_id", "signal_id", name="uq_project_signal"),)

    id: int | None = Field(default=None, primary_key=True)
    project_id: int = Field(foreign_key="projects.id", index=True)
    signal_id: int = Field(foreign_key="signals.id", index=True)
    match_confidence: float = 1.0
    match_method: str = "direct"  # direct | blocking+fuzzy | llm_adjudicated | manual_merge
    linked_at: datetime = Field(default_factory=utcnow)


class MatchCandidate(SQLModel, table=True):
    """Uncertain signal->project matches awaiting human review."""
    __tablename__ = "match_candidates"

    id: int | None = Field(default=None, primary_key=True)
    signal_id: int = Field(foreign_key="signals.id", index=True)
    project_id: int = Field(foreign_key="projects.id", index=True)
    similarity: float = 0.0
    llm_verdict: str | None = None  # match | no_match | uncertain
    llm_reasoning: str | None = None
    status: str = Field(default="pending", index=True)  # pending | merged | rejected
    created_at: datetime = Field(default_factory=utcnow)
    resolved_at: datetime | None = None


class DeveloperAlias(SQLModel, table=True):
    __tablename__ = "developer_aliases"
    __table_args__ = (UniqueConstraint("alias_norm", name="uq_alias_norm"),)

    id: int | None = Field(default=None, primary_key=True)
    canonical: str
    alias: str
    alias_norm: str = Field(index=True)
    learned_from: str = "config"  # config | merge_confirmation
    created_at: datetime = Field(default_factory=utcnow)


class Contact(SQLModel, table=True):
    __tablename__ = "contacts"

    id: int | None = Field(default=None, primary_key=True)
    name: str = Field(index=True)
    title: str | None = None
    company: str | None = Field(default=None, index=True)
    company_type: str | None = None  # mep | gc | mech_contractor | developer | utility | ahj
    territory: str | None = None
    phone: str | None = None
    email: str | None = None
    created_at: datetime = Field(default_factory=utcnow)
    notes: str = ""


class ProjectContact(SQLModel, table=True):
    __tablename__ = "project_contacts"
    __table_args__ = (UniqueConstraint("project_id", "contact_id", "role", name="uq_project_contact_role"),)

    id: int | None = Field(default=None, primary_key=True)
    project_id: int = Field(foreign_key="projects.id", index=True)
    contact_id: int = Field(foreign_key="contacts.id", index=True)
    role: str = "unknown"  # engineer_of_record | gc | precon | developer | mech_contractor | other


class Outreach(SQLModel, table=True):
    __tablename__ = "outreach"

    id: int | None = Field(default=None, primary_key=True)
    project_id: int = Field(foreign_key="projects.id", index=True)
    contact_id: int | None = Field(default=None, foreign_key="contacts.id")
    date: datetime = Field(default_factory=utcnow)
    channel: str = "call"  # call | email | meeting | text | other
    notes: str = Field(default="", sa_column=Column(Text, nullable=False, default=""))
    next_action: str | None = None
    next_action_date: datetime | None = None


class SourceRun(SQLModel, table=True):
    """Per-source health. Silent scraper failure is the #1 way this system dies."""
    __tablename__ = "source_runs"

    id: int | None = Field(default=None, primary_key=True)
    source: str = Field(index=True)
    started_at: datetime = Field(default_factory=utcnow)
    finished_at: datetime | None = None
    ok: bool | None = None
    records_fetched: int = 0
    records_new: int = 0
    error: str | None = Field(default=None, sa_column=Column(Text, nullable=True))


class BackfillCheckpoint(SQLModel, table=True):
    """One row per completed backfill chunk so a crash resumes, not restarts."""
    __tablename__ = "backfill_checkpoints"
    __table_args__ = (UniqueConstraint("source", "chunk_key", name="uq_backfill_chunk"),)

    id: int | None = Field(default=None, primary_key=True)
    source: str = Field(index=True)
    chunk_key: str  # e.g. "2024-08-01:Riverside:NOP" or "2024-08-01:month:2025-03"
    records_fetched: int = 0
    records_new: int = 0
    completed_at: datetime = Field(default_factory=utcnow)


class TokenSpend(SQLModel, table=True):
    """Per-call LLM spend. The daily budget kill switch sums this table."""
    __tablename__ = "token_spend"

    id: int | None = Field(default=None, primary_key=True)
    ts: datetime = Field(default_factory=utcnow, index=True)
    day: str = Field(index=True)  # YYYY-MM-DD (UTC) for cheap daily sums
    stage: str  # triage | extract | adjudicate
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0


class HttpLog(SQLModel, table=True):
    """Request archive for every source fetch, for after-the-fact debugging."""
    __tablename__ = "http_log"

    id: int | None = Field(default=None, primary_key=True)
    source_run_id: int | None = Field(default=None, foreign_key="source_runs.id", index=True)
    ts: datetime = Field(default_factory=utcnow)
    url: str
    status: int | None = None
    ok: bool = False
    elapsed_ms: int | None = None
    response_bytes: int | None = None
    error: str | None = None


class Firm(SQLModel, table=True):
    """Company roster: MEP firms, mech contractors, GCs, developers. Extracted
    named_firms resolve against this instead of spawning duplicate rows."""
    __tablename__ = "firms"
    __table_args__ = (UniqueConstraint("name_norm", name="uq_firm_norm"),)

    id: int | None = Field(default=None, primary_key=True)
    name: str = Field(index=True)
    name_norm: str = Field(index=True)
    firm_type: str = "unknown"  # mep | mech_contractor | gc | developer | consultant | unknown
    aliases: list = Field(default_factory=list, sa_column=Column(JSON, nullable=False, default=list))
    added_from: str = "roster"  # roster | extraction | dashboard
    created_at: datetime = Field(default_factory=utcnow)


class ProjectFirm(SQLModel, table=True):
    __tablename__ = "project_firms"
    __table_args__ = (UniqueConstraint("project_id", "firm_id", "role", name="uq_project_firm_role"),)

    id: int | None = Field(default=None, primary_key=True)
    project_id: int = Field(foreign_key="projects.id", index=True)
    firm_id: int = Field(foreign_key="firms.id", index=True)
    role: str = "unknown"  # engineer_of_record | gc | mech_contractor | developer | consultant
    linked_at: datetime = Field(default_factory=utcnow)


class OutcomeEvent(SQLModel, table=True):
    """Outcome feedback: what actually happened. Future scoring weights get
    tuned from this, not from assumptions."""
    __tablename__ = "outcome_events"

    id: int | None = Field(default=None, primary_key=True)
    project_id: int = Field(foreign_key="projects.id", index=True)
    status: str  # contacted | specified | bidding | won | lost | dead
    reason: str = ""
    created_at: datetime = Field(default_factory=utcnow)


class FalsePositiveMark(SQLModel, table=True):
    """Board rows I marked as wrongly ranked. Feeds future scoring tuning."""
    __tablename__ = "false_positive_marks"

    id: int | None = Field(default=None, primary_key=True)
    project_id: int = Field(foreign_key="projects.id", index=True)
    reason: str = ""
    score_at_mark: float = 0.0
    window_at_mark: str = ""
    created_at: datetime = Field(default_factory=utcnow)


class DigestLog(SQLModel, table=True):
    """What the digest already reported, so we only ever send new/changed items."""
    __tablename__ = "digest_log"
    __table_args__ = (UniqueConstraint("kind", "ref_id", "fingerprint", name="uq_digest_item"),)

    id: int | None = Field(default=None, primary_key=True)
    kind: str  # new_project | stage_change | review_pending | source_failure
    ref_id: int
    fingerprint: str  # e.g. stage value, score bucket — dedupe key for "materially changed"
    sent_at: datetime = Field(default_factory=utcnow)
