"""Database schema. Every extracted field traces back to a raw_document row with a URL."""
from __future__ import annotations

import enum
from datetime import datetime, timezone

from sqlalchemy import Column, Index, Text, UniqueConstraint
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

    `esco` is the odd one and the newest. It is not a new building at all: it is a
    public agency awarding an energy services performance contract on buildings it
    already owns. It earns a category rather than a keyword tag for the same
    reason industrial did — triage only keeps what it can name, so anything
    without a category of its own is classified `other`, and `other` means
    discarded. Detection that ends in `other` captures nothing.

    Two consequences that callers must handle rather than inherit:
      - `esco` never uses the data-centre electrical sizing path. See
        estimate_tons: a retrofit has no stated IT load, and borrowing data-centre
        watts-per-square-foot would overstate it by an order of magnitude.
      - `esco` is not one of the two construction boards. It is kept, counted and
        reachable, but it does not silently join a ranking of new-build
        opportunities it is not competing in.
    """
    data_center = "data_center"
    industrial = "industrial"
    esco = "esco"
    other = "other"

    @classmethod
    def boards(cls) -> tuple["Category", ...]:
        """The two new-construction boards, in board order."""
        return (cls.data_center, cls.industrial)


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
    # Extract intends one signal per document, but its lookup and its insert
    # straddle a Sonnet call, so two concurrent runs can both find nothing and
    # both insert. Intent in application code is not a guarantee. NULLs are
    # distinct in a Postgres unique index, so manual signals are unaffected.
    __table_args__ = (UniqueConstraint("raw_document_id", name="uq_signal_raw_document"),)

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
    # State Clearinghouse number. CEQAnet assigns one per PROJECT and reuses it
    # across every filing in the series (NOP, then DEIR, then NOD), so it is the
    # only exact project identifier any source gives us. Not extracted by the LLM
    # — copied from the adapter's document metadata, so it cannot be hallucinated.
    sch_number: str | None = Field(default=None, index=True)
    latitude: float | None = None
    longitude: float | None = None
    mw_it: float | None = None
    mw_total: float | None = None
    generator_count: int | None = None
    generator_hp_each: float | None = None
    generator_kw_each: float | None = None
    # Some filings (CEC power filings routinely) split the fleet into gensets
    # dedicated to data center critical/IT load and gensets backing house load
    # (office, cooling plant, everything else). Stated directly in MW, not
    # hp/kW, so this holds the number as written rather than forcing a
    # conversion the no-conversion extraction rule would then reject. See
    # app/pipeline/sizing.py's "gensets_critical" basis.
    generator_critical_count: int | None = None
    generator_critical_mw_each: float | None = None
    generator_house_count: int | None = None
    generator_house_mw_each: float | None = None
    building_sqft: float | None = None
    acres: float | None = None
    building_count: int | None = None
    cooling_type: str | None = None
    water_acre_feet_per_year: float | None = None
    # Water-source facts, separate from whether the project is publicly
    # contested over water -- see app/pipeline/waterrisk.py for why a single
    # "water contested" flag would erase the distinction that actually
    # decides an evaporative design's entitlement risk (potable vs
    # recycled/reclaimed supply). Null unless the document states it.
    water_source_stated: str | None = None
    water_reclaimed_identified: bool | None = None
    water_use_efficiency_stated: str | None = None
    water_opposition_stated: bool | None = None
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
    sch_number: str | None = Field(default=None, index=True)  # see Signal.sch_number
    mw_it: float | None = None
    mw_total: float | None = None
    tons_estimate_low: float | None = None
    tons_estimate_high: float | None = None
    estimate_basis: str | None = None  # which input drove the tonnage estimate
    estimate_low_confidence: bool = Field(default=False)
    # What the cooling is worth, which is not the same as how much there is: a
    # 5,000 ton fulfillment centre on packaged rooftops is a smaller opportunity
    # than a 600 ton cleanroom on custom AHUs. Null whenever tonnage or facility
    # type is unknown. See app/pipeline/sizing.estimate_equipment_value.
    equipment_value_low: float | None = None
    equipment_value_high: float | None = None
    equipment_value_basis: str | None = None
    stage: Stage = Field(default=Stage.unknown, index=True)
    window: Window = Field(default=Window.PRE_BOD)
    score: float = Field(default=0.0, index=True)
    # Phase 4: county-adjacency data center spillover multiplier applied to
    # `score` (see app/pipeline/spillover.py). spillover_mw is the raw
    # recent/queued DC MW its county + neighbors carried at last scoring run;
    # spillover_basis is the human-readable "why", shown on the board row.
    # Both null when the feature is disabled or the county resolved no MW.
    spillover_mw: float | None = None
    spillover_basis: str | None = None
    days_to_estimated_bid: int | None = None
    # Rolled up from linked signals in app.pipeline.resolve._absorb -- see
    # app/pipeline/waterrisk.py. water_risk_flag/basis are a CONFIDENCE
    # caveat, same tier as estimate_low_confidence below: never blended into
    # `score`, only ever shown as a reason to doubt this project proceeds as
    # filed. null/"noise" render nothing; "elevated" is the one worth a rep's
    # attention.
    water_source_stated: str | None = None
    water_reclaimed_identified: bool | None = None
    water_use_efficiency_stated: str | None = None
    water_opposition_stated: bool | None = None
    water_risk_flag: str | None = None       # null | noise | elevated
    water_risk_basis: str | None = None
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
    # manual | filing | apollo | lusha. A name typed into the dashboard, a name
    # extracted from a public CEQA/GOED filing, and a name pulled from a paid
    # contact database carry different weight -- this is what lets the board
    # show which is which rather than rendering all three the same way. See
    # app/enrichment.py and RUNG_LABELS in app/ladder.py.
    source: str = Field(default="manual", index=True)
    # confirmed | pending. "confirmed" means phone or email is populated and
    # reachable today. "pending" means the free Lusha/Apollo SEARCH layer
    # found a real named person and title at the firm, but no reveal was run
    # (search is free; reveal costs credits, or is plan-gated) -- a name is
    # still worth more than nothing, so it goes on the ladder as "one phone
    # call away" rather than not at all. import_enriched_contact() writes
    # "confirmed" and still refuses a contact with no phone/email;
    # import_pending_contact() is the separate, explicit write path for this
    # state -- see app/enrichment.py.
    reach_status: str = Field(default="confirmed", index=True)


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


class SavedSearch(SQLModel, table=True):
    """A standing question about the board, and whether to be told when it changes.

    Three shapes were asked for and they are genuinely different questions:
      - a THRESHOLD on the board          "anything over 10 MW in Storey County"
      - a NAME appearing anywhere         "anything naming Southland or ACCO"
      - a TRANSITION on a watched row     "any flagged project that changes stage"

    The first two are filters over the current board and can be answered by a
    query. The third is not: "changed" is a statement about two points in time, so
    it needs the previous answer stored to compare against. `last_seen` carries
    that, which is why it lives on the row rather than being recomputed.

    Criteria are a dict rather than columns because the set of askable questions
    will keep growing and a migration per question is a tax on asking them. The
    keys are validated in app/searches.py — an unknown key raises rather than
    silently matching everything, which is the failure mode a free-form filter
    invites.
    """
    __tablename__ = "saved_searches"

    id: int | None = Field(default=None, primary_key=True)
    name: str
    criteria: dict = Field(default_factory=dict, sa_column=Column(JSON, nullable=False,
                                                                  default=dict))
    alert: bool = True          # include in the daily digest
    alert_on_change: bool = False  # fire on stage transition, not on membership
    # Project ids the search matched when it was last evaluated, plus the stage
    # each was in. Only meaningful for alert_on_change; see app/searches.py.
    last_seen: dict = Field(default_factory=dict, sa_column=Column(JSON, nullable=False,
                                                                   default=dict))
    last_run_at: datetime | None = None
    created_at: datetime = Field(default_factory=utcnow)


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


# `source_runs.source` is ONE namespace with three writers and three conventions:
# run_fetch records a bare source name ("ceqanet"), run_backfill records
# "<source>:backfill", and stage_run records a pipeline stage ("resolve",
# "extract"). Reading the table therefore requires knowing the convention, and
# `scout doctor` did not — it matched the bare name only, so seven sources whose
# only runs were backfills all reported "no successful run recorded" against a
# table holding nine successful ones.
#
# The naming lives here, next to the column, precisely so a writer and a reader
# cannot drift apart again. Anything that writes a run name builds it with
# `source_run_name`; anything that reads one attributes it with
# `run_name_source`; and doctor checks that every name in the table is
# attributable, so the next invented variant fails loudly instead of quietly
# reopening this hole.
BACKFILL_RUN_MODE = "backfill"
STAGE_RUN_NAMES = frozenset({"resolve", "extract"})


def source_run_name(source: str, mode: str | None = None) -> str:
    """The `source_runs.source` value for a run of `source` in `mode`.

    `mode=None` is the scheduled fetch and keeps the bare name, because that is
    what is already in the table and renaming it would strand the history.
    """
    return f"{source}:{mode}" if mode else source


def run_name_source(run_name: str) -> str:
    """The source a run name belongs to: 'ceqanet:backfill' -> 'ceqanet'."""
    return run_name.split(":", 1)[0]


def run_name_mode(run_name: str) -> str | None:
    """The mode a run name carries: 'ceqanet:backfill' -> 'backfill', 'ceqanet' -> None."""
    _, sep, mode = run_name.partition(":")
    return mode if sep else None


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


class StageObservation(SQLModel, table=True):
    """Every stage a project has ever been observed at, kept forever — an
    append-only ledger, same shape as TokenSpend/HttpLog (insert only, never
    updated). `Project.stage` is still a single column, still set only by
    app.pipeline.resolve._absorb, and still moves forward only (see the
    comment there) — nothing about that changes. This table is what that
    single column always lacked: WHEN each claim was made and from WHAT, so
    staleness can be measured against evidence for the stage a project shows
    TODAY specifically, rather than against evidence of any kind (the gap
    app/staleness.py's original stage_ages() had — see its module docstring
    for the case that exposed it), and so the project page can show the
    progression instead of only the present.

    One row per signal that stated a stage — written in app.pipeline.resolve
    right after _absorb(), for EVERY signal.stage != unknown, whether or not
    it actually moved project.stage forward. A signal that reports an earlier
    stage than the project already shows is still a real observation, worth
    keeping for the history even though the forward-only rule means it cannot
    become the current value.
    """
    __tablename__ = "stage_observations"
    __table_args__ = (UniqueConstraint("signal_id", name="uq_stage_observation_signal"),)

    id: int | None = Field(default=None, primary_key=True)
    project_id: int = Field(foreign_key="projects.id", index=True)
    stage: Stage = Field(index=True)
    observed_at: datetime = Field(index=True)
    # True when observed_at is the signal's stated event date; False when it
    # fell back to when we saw the document — same distinction staleness.py's
    # StageAge has always carried, preserved here rather than lost.
    from_event: bool = True
    # Null only for a manually-entered correction (none exist yet, but the
    # column exists so one never has to be faked as a fake signal_id).
    signal_id: int | None = Field(default=None, foreign_key="signals.id", index=True)
    source: str = "signal"  # signal | manual
    created_at: datetime = Field(default_factory=utcnow)


class McpOAuthClient(SQLModel, table=True):
    """An OAuth client Claude (or any MCP client) registered via Dynamic Client
    Registration. DCR is left open — anyone can register a client — because the
    real security boundary is the password check at /authorize, not the
    registration step; see app/mcp_auth.py.

    `data` holds the full mcp.shared.auth.OAuthClientInformationFull payload as
    JSON rather than one column per field, so a new field the SDK adds never
    needs a migration here.
    """
    __tablename__ = "mcp_oauth_clients"

    client_id: str = Field(primary_key=True)
    data: dict = Field(sa_column=Column(JSON, nullable=False))
    created_at: datetime = Field(default_factory=utcnow)


class McpAuthCode(SQLModel, table=True):
    """A short-lived authorization code between /authorize and /token. DB-backed
    (not in-memory, unlike the SDK's demo provider) so a Render restart between
    the two calls doesn't orphan an in-flight login — see app/mcp_auth.py.
    """
    __tablename__ = "mcp_auth_codes"

    code: str = Field(primary_key=True)
    data: dict = Field(sa_column=Column(JSON, nullable=False))
    expires_at: float = Field(index=True)  # unix timestamp, matching mcp.server.auth.provider.AuthorizationCode
    created_at: datetime = Field(default_factory=utcnow)


class McpAccessToken(SQLModel, table=True):
    """A bearer token issued to an MCP client after a successful password login.
    DB-backed for the same reason as McpAuthCode — see app/mcp_auth.py for the
    lifetime (long, no refresh flow: single user, the password gate is the real
    boundary).
    """
    __tablename__ = "mcp_access_tokens"

    token: str = Field(primary_key=True)
    data: dict = Field(sa_column=Column(JSON, nullable=False))
    expires_at: float | None = Field(default=None, index=True)
    created_at: datetime = Field(default_factory=utcnow)


class ProductLine(SQLModel, table=True):
    """DMG/ToroAire line card, one row per manufacturer line. Seeded from
    config.yaml accounts.line_card (app/accounts.py:seed_product_lines) and
    edited THERE, not through the dashboard — same convention as the Firm
    roster (app/firms.py), for the same reason: a manufacturer's category and
    value tier is stable reference data, not something a rep types once and
    forgets to keep in sync across accounts."""
    __tablename__ = "product_lines"
    __table_args__ = (UniqueConstraint("name_norm", name="uq_product_line_norm"),)

    id: int | None = Field(default=None, primary_key=True)
    name: str = Field(index=True)
    name_norm: str = Field(index=True)
    firm: str = "DMG"  # DMG | ToroAire | both
    category: str = Field(index=True)  # see accounts.adjacency.categories in config.yaml
    subcategory: str = ""
    description: str = ""
    value_tier: int = 3  # 1 (highest $/unit) - 5 (lowest); see accounts.value_tier_dollars
    # One of replacement.service_life's 8 equipment keys, or null when this line
    # is not one of the archetypes that table has a service-life band for —
    # see the comment on accounts.line_card in config.yaml. Replacement windows
    # are simply not computed for a null line, rather than guessing.
    equipment_type: str | None = None
    # evaporative | adiabatic_hybrid | dry_air_cooled | closed_loop | null.
    # A MODEL-level fact, not a brand-level or equipment_type-level one — a
    # cooling tower line is not uniformly "evaporative" (Marley's own catalog
    # includes adiabatic/hybrid units that run dry most of the year), so this
    # is never inferred from equipment_type or from what a brand is generally
    # known for. Set only where someone has actually stated it, and
    # heat_rejection_mode_verified stays false until confirmed with the
    # factory — see config.yaml's line_card comment and
    # app/accounts.py:seed_product_lines.
    heat_rejection_mode: str | None = None
    heat_rejection_mode_verified: bool = False
    heat_rejection_mode_basis: str | None = None

    # Four more MODEL-level capability facts, same discipline as
    # heat_rejection_mode above: null until someone has actually stated it,
    # unverified until confirmed with the factory, never inferred from brand
    # reputation or from what the equipment_type/category "usually" implies.
    # Built for app.compare:compare_lines (scout compare-lines / the
    # compare_lines MCP tool) — see that module's docstring for the abstain
    # rule these fields exist to serve: a null or unverified value is a
    # capability_gap, never a vote against the line.
    latent_load_capability: str | None = None       # enhanced | standard | null
    latent_load_capability_verified: bool = False
    latent_load_capability_basis: str | None = None
    corrosion_resistance: str | None = None         # marine_grade | coated_standard | standard | null
    corrosion_resistance_verified: bool = False
    corrosion_resistance_basis: str | None = None
    redundancy_capable: bool | None = None           # can be configured N+1 / modular
    redundancy_capable_verified: bool = False
    redundancy_capable_basis: str | None = None
    rigging_constrained_capable: bool | None = None  # ships/installs in tight space or split shipment
    rigging_constrained_capable_verified: bool = False
    rigging_constrained_capable_basis: str | None = None

    # Primary organization for the line card view (app/web/main.py's
    # /lines): which role in a building this line fills — air handling,
    # cooling generation, heat rejection, etc. A rep thinks in these terms,
    # not in the 18 finer-grained categories accounts.adjacency scores gaps
    # on, so this is a SEPARATE field rather than a repurposing of category
    # above — category keeps driving gap/adjacency math untouched. Resolved
    # at seed time (see app/accounts.py:resolve_building_role) from category
    # via CATEGORY_TO_ROLE, with an explicit override for the handful of
    # lines whose actual product mix diverges from their category's default
    # (ROLE_OVERRIDE_BY_LINE) — correct there, not here, then re-run
    # `scout seed-lines`.
    building_role: str = Field(index=True, default="heating_specialty")

    # Which of the 8 markets (data_center, healthcare, industrial_warehouse,
    # education, hospitality, labs, office, multifamily) this line sells
    # into. Two tiers, distinguished by markets_served_source below because
    # they are NOT the same kind of claim: "researched" (config.yaml's
    # `markets:` key, one manufacturer-literature citation per line in
    # markets_served_basis) and "legacy_guess" (MARKETS_BY_LINE in
    # app/accounts.py, an unsourced first-pass table predating the research
    # pass, kept only as a fallback for lines nobody has researched yet).
    # Empty with a null source means genuinely unmapped, not "sells
    # nowhere" -- see markets_served_source. Distinct from PROJECT matching
    # below: Scout's own board only tracks data_center/industrial/esco new-
    # construction, so a line marked "healthcare" here will never surface a
    # matching Scout project -- that absence is a fact about what Scout
    # tracks, not a fact about the line.
    markets_served: list = Field(default_factory=list, sa_column=Column(JSON, nullable=False, default=list))
    # "researched" | "legacy_guess" | null (genuinely unmapped, no fallback
    # either). A guess and a sourced fact must never render the same way --
    # see app/accounts.py:seed_product_lines for how this is set and
    # lines.html / line_detail.html for how it's displayed. Set alongside
    # markets_served on every seed, never edited by hand.
    markets_served_source: str | None = None
    # Citation for markets_served -- present only when markets_served_source
    # is "researched" (manufacturer literature, URL + retrieval date); null
    # for "legacy_guess" rows, which have no citation to give.
    markets_served_basis: str | None = None

    # Everything below is UNFILLED (null) until confirmed by name and date —
    # the same discipline heat_rejection_mode above already applies. A
    # confidently wrong eligibility or lead-time claim in front of an
    # engineer is unrecoverable, so there is no inferred default for any of
    # these: brand reputation is not a source.
    competes_with: str | None = None          # named competitor line(s), free text
    competes_with_basis: str | None = None

    oshpd_osp: bool | None = None             # OSHPD/HCAI OSP pre-approval, CA healthcare
    oshpd_osp_basis: str | None = None
    ufc_4_010_06: bool | None = None          # UFC 4-010-06 antiterrorism standoff applicability
    ufc_4_010_06_basis: str | None = None
    ahri_certified: bool | None = None
    ahri_certified_basis: str | None = None
    country_of_manufacture: str | None = None
    country_of_manufacture_basis: str | None = None

    lead_time_weeks_low: int | None = None
    lead_time_weeks_high: int | None = None
    lead_time_basis: str | None = None        # sourced by name and date, required alongside any value

    limitations: str | None = None            # stated plainly, only where actually known — never inferred

    created_at: datetime = Field(default_factory=utcnow)


class Account(SQLModel, table=True):
    """A company DMG/ToroAire sells to or through — deliberately distinct from
    Firm, which is the roster extracted named_firms resolve against on the
    project-pipeline side. An Account carries sales-pipeline fields (assigned
    rep, order history, parent/child hierarchy) Firm has no use for, and Firm
    carries extraction provenance (added_from, aliases) an Account has no use
    for. `firm_id` is an optional bridge: set it (by hand, or auto-matched on
    create by normalized name) so an account brief can show live Scout
    projects naming this company without a second name-matching pass."""
    __tablename__ = "accounts"

    id: int | None = Field(default=None, primary_key=True)
    name: str = Field(index=True)
    name_norm: str = Field(index=True)
    parent_id: int | None = Field(default=None, foreign_key="accounts.id", index=True)
    # mechanical_contractor | service_contractor | gc | owner | developer | distributor | engineer
    account_type: str = Field(default="mechanical_contractor", index=True)
    address: str | None = None
    city: str | None = None
    county: str | None = Field(default=None, index=True)
    state: str | None = None
    assigned_rep: str | None = Field(default=None, index=True)
    first_order_date: datetime | None = None
    last_order_date: datetime | None = None
    notes: str = Field(default="", sa_column=Column(Text, nullable=False, default=""))
    # federal | state_municipal | private_commercial — matches
    # replacement.service_life.ownership keys, and drives which band this
    # account's replacement windows are computed from. Defaults to the longest
    # cycle for the same reason app/replacement.py's default does: guessing
    # short invents a live candidate years early, guessing long only misses one.
    ownership_type: str = "private_commercial"
    firm_id: int | None = Field(default=None, foreign_key="firms.id", index=True)
    status: str = Field(default="active", index=True)  # active | dormant | archived
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class AccountCoverage(SQLModel, table=True):
    """One account's relationship to one line-card line. A row is created for
    every product line the moment an account is created (see
    app/accounts.py:ensure_coverage_rows) so the account page always has
    something to click on, rather than an editor that only shows lines someone
    has already touched — the whole point is learning coverage by hand, a line
    at a time, from conversations."""
    __tablename__ = "account_coverage"
    __table_args__ = (UniqueConstraint("account_id", "product_line_id", name="uq_account_line"),)

    id: int | None = Field(default=None, primary_key=True)
    account_id: int = Field(foreign_key="accounts.id", index=True)
    product_line_id: int = Field(foreign_key="product_lines.id", index=True)
    status: str = Field(default="unknown", index=True)  # bought | quoted_not_won | never_quoted | unknown
    install_year: int | None = None
    dollar_value: float | None = None
    notes: str = Field(default="", sa_column=Column(Text, nullable=False, default=""))
    updated_at: datetime = Field(default_factory=utcnow)


class IeprForwardLoad(SQLModel, table=True):
    """One row from a utility's CEC IEPR large-load forecast filing (see
    app/pipeline/iepr.py) — a county-level FORWARD MW LAYER, not a project
    detector. The source spreadsheet is redacted to status/city/region/MW: no
    developer name, no address, nothing a lead could be built from. That is
    the point — this table feeds Phase 4's county-adjacency spillover score
    as an aggregate, and must never be joined against a Project or Signal as
    if a row here named one.

    Imported by hand, twice a year, from a downloaded CEC docket filing (see
    `scout import-iepr`) — deliberately NOT a scraper against TN-numbered
    filings pretending to be a live feed. Each import replaces every prior
    row for the same (utility, docket_tn): the filing is a point-in-time
    snapshot, not an appendable stream, and importing it a second time under
    the same docket_tn means "I re-downloaded the same filing," not "here are
    more rows."
    """
    __tablename__ = "iepr_forward_loads"

    id: int | None = Field(default=None, primary_key=True)
    utility: str = Field(index=True)          # e.g. "SCE"
    docket_tn: str = Field(index=True)        # CEC transaction number this row came from
    source_url: str                           # traces every row to a public filing
    status: str | None = None                 # raw status string from the filing
    cec_grouping: str | None = None           # raw grouping value; meaning undocumented upstream, stored as-is
    region: str | None = None                 # utility's internal planning region, not a county
    city: str | None = None                   # as filed — may be a typo, a placeholder ("Open"), or missing
    county: str | None = Field(default=None, index=True)  # derived from city; null when city can't be confidently mapped
    state: str = Field(default="CA", index=True)
    voltage: float | None = None
    requested_energization_year: int | None = None
    requested_peak_mw: float | None = None
    imported_at: datetime = Field(default_factory=utcnow, index=True)


class HcaiCountyActivity(SQLModel, table=True):
    """County-level AGGREGATE healthcare construction activity from HCAI's
    (formerly OSHPD) public CHHS Open Data CSV — see app/pipeline/hcai.py.
    Same shape as IeprForwardLoad: no facility names, no addresses, nothing a
    lead could be built from, because HCAI's only genuinely public data is
    aggregated by county+status. The per-project detail (facility names,
    plan-review stage before construction) lives behind HCAI's login-gated
    eServices portal — confirmed not publicly reachable — so this can never
    be more than a county-level trend layer.

    Imported by hand from a downloaded CHHS CSV, same reason as IEPR: the
    dataset's own download URL embeds a generation date that changes with
    every ~biweekly CHHS update, and the JSON API path that would let a
    fetcher discover the CURRENT filename is robots.txt-disallowed
    (data.chhs.ca.gov disallows /api/ and /datastore/*) — so there is no
    compliant way to auto-resolve "the latest file" and this is a snapshot
    import, not a live feed, on principle as much as mechanics.
    """
    __tablename__ = "hcai_county_activity"
    __table_args__ = (UniqueConstraint("county", "status", name="uq_hcai_county_activity"),)

    id: int | None = Field(default=None, primary_key=True)
    county: str = Field(index=True)
    state: str = Field(default="CA", index=True)
    status: str = Field(index=True)  # In Review | Pending Construction | In Construction | In Closure
    total_cost: float | None = None
    project_count: int | None = None
    snapshot_date: datetime = Field(index=True)
    source_url: str
    imported_at: datetime = Field(default_factory=utcnow, index=True)


class EquipmentPermit(SQLModel, table=True):
    """One mechanical (HVAC/refrigeration) permit from a city/county open-data
    feed — see app/pipeline/permits.py. This is NOT a project signal: it is
    the install-year evidence the regulatory engine (app/pipeline/regulatory.py)
    needs to infer refrigerant type and evaluate SB 1206 without ever seeing a
    nameplate. equipment_count/tons_each are mined from the free-text work
    description (e.g. "REPLACE (2) 6-ton HEAT PUMP PACKAGE UNIT"), not from a
    structured field the source doesn't provide.
    """
    __tablename__ = "equipment_permits"
    __table_args__ = (UniqueConstraint("source", "permit_nbr", name="uq_equipment_permit"),)

    id: int | None = Field(default=None, primary_key=True)
    source: str = Field(index=True)  # e.g. "la_city_mechanical"
    permit_nbr: str = Field(index=True)
    apn: str | None = Field(default=None, index=True)
    address: str | None = None
    county: str = Field(default="Los Angeles", index=True)
    state: str = Field(default="CA", index=True)
    permit_type: str | None = None       # e.g. "HVAC"
    permit_sub_type: str | None = None   # Commercial | Apartment | 1 or 2 Family Dwelling
    status_desc: str | None = None
    issue_date: datetime | None = Field(default=None, index=True)
    work_desc: str = Field(default="", sa_column=Column(Text, nullable=False, default=""))
    equipment_count: int | None = None
    tons_each: float | None = None
    inferred_refrigerant: str | None = Field(default=None, index=True)
    sb1206_trigger_status: str | None = Field(default=None, index=True)  # in_effect | upcoming | null
    sb1206_detail: str | None = None
    source_url: str
    imported_at: datetime = Field(default_factory=utcnow, index=True)


class AssessorCandidate(SQLModel, table=True):
    """One parcel from a county assessor roll, flagged as a CANDIDATE for a
    regulatory trigger by use code or building size — see
    app/pipeline/assessor.py. "Candidate" is the operative word: CARB's R3
    filer list is not public (confirmed — login-gated portal, no open
    dataset), so this can say "this parcel's use code matches the commercial-
    refrigeration segment," never "this facility reports to CARB." Likewise
    for EBEWE: a >20,000 sqft building is IN SCOPE for the audit cycle, not
    confirmed to have filed one.
    """
    __tablename__ = "assessor_candidates"
    __table_args__ = (UniqueConstraint("source", "ain", "trigger_key", name="uq_assessor_candidate"),)

    id: int | None = Field(default=None, primary_key=True)
    source: str = Field(index=True)  # e.g. "la_county_assessor"
    ain: str = Field(index=True)     # assessor identification number (parcel id)
    trigger_key: str = Field(index=True)  # matches a key in config.yaml's regulatory_triggers
    use_code: str | None = None
    use_desc: str | None = None
    address: str | None = None
    county: str = Field(default="Los Angeles", index=True)
    state: str = Field(default="CA", index=True)
    year_built: int | None = None
    sqft: float | None = None
    source_url: str
    imported_at: datetime = Field(default_factory=utcnow, index=True)


class RetrofitBuilding(SQLModel, table=True):
    """One BUILDING (not permit) — the retrofit board's unit of record. See
    app/pipeline/retrofit.py: this is a deduplication of EquipmentPermit
    (one building often carries several permits over the years) joined
    against assessor parcel characteristics (sqft, year built, use code).

    Deliberately NO owner name / owner mailing address field: verified live
    that no free, bulk-queryable source of that data exists for LA County —
    every parcel API and the Assessor's own portal API strip ownership
    fields entirely. `address` (situs/property address) is the only location
    identifier here; this is a call list built from public equipment and
    building records, not a mailing list.

    A separate population from Project — a building that pulled a mechanical
    permit is not a building filing new construction, and the two tables
    should never be joined as if a row in one implies a row in the other
    (measured: zero APN overlap between this table and Project as of the
    first build).
    """
    __tablename__ = "retrofit_buildings"

    id: int | None = Field(default=None, primary_key=True)
    apn: str = Field(index=True, unique=True)
    county: str = Field(default="Los Angeles", index=True)
    state: str = Field(default="CA", index=True)
    address: str | None = None

    # recently_active: has mechanical-permit evidence (2010-present) --
    # equipment_type/service_life_status below are permit-verified.
    # replacement_candidate: a commercial/industrial parcel built before the
    # permit window with NO permit on record at all -- absence is the
    # signal here (see app/pipeline/retrofit.py:find_replacement_candidates):
    # either the original equipment is still in place, or it was replaced
    # without a permit. No permit means no equipment TYPE evidence, so
    # equipment_type/mined_tons_each/sb1206 stay null for these rows on
    # principle. service_life_status IS computed for them, from
    # building_age_years (YearBuilt) as an install-year proxy under the
    # generic (equipment-unspecified) ownership-branched table -- see
    # app/replacement.py:generic_service_life. Its basis string is always
    # prefixed YEARBUILT-DERIVED so it can never be mistaken for the
    # permit-verified figure recently_active rows carry.
    population: str = Field(default="recently_active", index=True)
    building_age_years: float | None = None

    # From the assessor parcel join (never owner fields — see docstring)
    use_code: str | None = None
    use_desc: str | None = None
    sqft: float | None = None
    year_built: int | None = None

    # From the deduplicated permit history for this APN
    permit_count: int = 0
    latest_permit_nbr: str | None = None
    latest_install_year: int | None = None
    # packaged_rooftop | split_dx | air_cooled_chiller | water_cooled_chiller |
    # air_handling_unit | boiler | cooling_tower | vav_terminal | null (see
    # app/pipeline/retrofit.py:infer_equipment_type — never guessed past what
    # the work-description text actually supports)
    equipment_type: str | None = None
    mined_tons_each: float | None = None
    mined_equipment_count: int | None = None
    inferred_refrigerant: str | None = None

    # replacement_candidate only: a sqft-derived tonnage BAND (never a point
    # figure), industry rule-of-thumb sqft/ton by use code -- see
    # app/pipeline/retrofit.py:estimate_tonnage and config.yaml's
    # retrofit.candidate_sqft_per_ton. NOT permit-verified, must never be
    # confused with mined_tons_each above; null when use_desc has no
    # configured band rather than guessed.
    estimated_tons_low: float | None = None
    estimated_tons_high: float | None = None
    estimated_tons_basis: str | None = None

    # Which regulations fire on this building (evaluated at build time
    # against config.yaml's verified regulatory_triggers)
    sb1206_trigger_status: str | None = None  # in_effect | upcoming | null
    sb1206_detail: str | None = None
    carb_candidate: bool = Field(default=False, index=True)
    carb_use_code: str | None = None
    ebewe_candidate: bool = Field(default=False, index=True)

    # From app/replacement.py's ownership-branched service life table.
    # ownership is always "private_commercial" (the default, longest cycle)
    # because ownership type cannot be determined without owner data — see
    # replacement.py's own stated rule: better to miss a federal building
    # than invent a live one.
    service_life_status: str | None = Field(default=None, index=True)  # not_due | approaching | due | overdue
    service_life_basis: str | None = None
    equipment_age_years: float | None = None
    # age_years minus the service-life LOW threshold: negative before the
    # window, 0 right at "due", growing through due into overdue. status
    # alone is a 4-value tier that saturates hard on populations built to
    # skew old (measured: 95% "overdue" on replacement_candidate) -- this is
    # the gradient underneath it, persisted (not just a template
    # computation) so it's the SAME number rank_buildings ranks on and the
    # board displays, not two versions that can drift. See
    # app/pipeline/retrofit.py:rank_buildings.
    service_life_years_past: float | None = None

    # A number a rep can sort by, with every input traceable above — see
    # app/pipeline/retrofit.py:rank_buildings for the exact formula.
    rank_score: float | None = Field(default=None, index=True)

    permit_source_url: str | None = None
    assessor_source_url: str | None = None

    # Actual reported service frequency, manual entry only -- see
    # ServiceFrequencyReport below, this system's only source for it (there
    # is no scraper and there will not be one; this data lives inside
    # contractors' FSM systems and nowhere public). Joined onto this row at
    # build time from the most recent ServiceFrequencyReport for this apn --
    # see app/pipeline/retrofit.py:rank_buildings and build_retrofit_buildings
    # / find_replacement_candidates. NOT cleared by this table's own
    # rebuild-and-replace semantics: the join re-attaches it every time,
    # because ServiceFrequencyReport is the durable source of truth, this
    # column is a cache of it. Nullable, and null on nearly every row by
    # construction -- one contractor field report is not a program yet, see
    # app.assumptions's entry for this field and
    # app.pipeline.retrofit:service_calls_coverage for how thin the sample
    # currently is.
    service_calls_per_year: float | None = None
    service_calls_per_year_source: str | None = None
    service_calls_per_year_reported_at: datetime | None = None

    built_at: datetime = Field(default_factory=utcnow, index=True)


class ServiceFrequencyReport(SQLModel, table=True):
    """One manually-reported service-call figure for one building, `scout
    report-service-frequency` only -- there is no adapter for this and there
    will not be one. This data lives inside contractors' FSM systems and is
    not public; the field version of the assessor YearBuilt service-life
    proxy is a phone call, not a scrape.

    Survives RetrofitBuilding's own rebuild-and-replace semantics on
    purpose: build_retrofit_buildings/find_replacement_candidates DELETE and
    reinsert their population every run (see those functions' docstrings --
    it's a derived view over EquipmentPermit/AssessorCandidate, correctly
    rebuilt whole). A manually-entered fact must not get silently wiped by
    that, so it lives in its OWN table, keyed by apn, and gets rejoined onto
    the new RetrofitBuilding rows at the end of every rebuild. Multiple
    reports per apn are kept (history, not overwritten); the most recent by
    reported_at is what's currently joined in and ranked on.
    """
    __tablename__ = "service_frequency_reports"

    id: int | None = Field(default=None, primary_key=True)
    apn: str = Field(index=True)
    service_calls_per_year: float
    source: str  # who reported it -- a named contractor/company, not "a contractor"
    reported_at: datetime  # when THEY reported the figure, not when we recorded the row
    equipment_note: str | None = None  # which unit, if named -- the trigger case was one unit, not the building
    created_at: datetime = Field(default_factory=utcnow)


class SelectionTool(SQLModel, table=True):
    """Which selection software a rep actually uses to spec one line -- one
    row per ProductLine, 70 total. Deliberately NOT a per-product formula
    page: the role-based equations already cover the physics every line in
    a role shares, so this only answers "which tool" plus (elsewhere, not
    yet built) the handful of genuinely product-specific calculations.

    firm is NOT a column here on purpose -- ProductLine.firm (DMG/ToroAire/
    both) is already the single source of truth for who reps a line; join
    through product_line_id rather than duplicating it, so a firm
    correction on the line card can never leave this table quietly stale.

    verification_status is the load-bearing field:
      - "unchecked": nobody has looked. access_level/tool_name etc. are null.
      - "search_verified": found via web research (a vendor's own product
        page, a resources/downloads page) but not confirmed by the
        manufacturer directly and not a live-directory hit -- same
        discipline as ahri_certified's SEARCH_VERIFIED tier.
      - "confirmed": stated on the vendor's own page in a way that leaves no
        ambiguity (e.g. SPX's own CoolSpec page naming both Marley and
        Recold).
    A null access_level under "unchecked" must never be confused with
    access_level == "none_exists" (checked, and the tool genuinely doesn't
    exist) -- "not yet looked" is not "does not exist". See
    app.accounts.seed_selection_tools for the seed discipline (never infer
    a shared tool from a corporate/plant relationship -- Geoclima and
    Hecoclima share a physical plant, ClimateCraft and ClimaCool share a
    parent company, neither implies shared selection software).
    """
    __tablename__ = "selection_tools"
    __table_args__ = (UniqueConstraint("product_line_id", name="uq_selection_tool_line"),)

    id: int | None = Field(default=None, primary_key=True)
    product_line_id: int = Field(foreign_key="product_lines.id", index=True, unique=True)
    tool_name: str | None = None
    vendor_url: str | None = None
    # public_free | free_registration | rep_login | request_from_factory | none_exists | null
    access_level: str | None = None
    what_it_outputs: str | None = None
    produces_submittal_docs: bool | None = None
    verified_by: str | None = None
    verified_date: datetime | None = None
    verification_status: str = Field(default="unchecked", index=True)  # unchecked | search_verified | confirmed
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class AccessLog(SQLModel, table=True):
    """One row per dashboard page hit -- app.access_log's middleware, not a
    route handler, writes these (see that module for why: static assets,
    /healthz, and the MCP surface are excluded by path, everything else is
    logged regardless of auth outcome).

    username is read from the raw Authorization header, not from the auth()
    dependency's return value -- auth() only ever returns "andrew" (the one
    configured dashboard user) or raises 401 before this table is touched.
    Capturing the SUBMITTED username independent of whether it was accepted
    is what makes "a username that isn't andrew appeared" a real, detectable
    event (a failed/probing login attempt) instead of a structural
    impossibility. null means no Authorization header was sent at all --
    genuinely anonymous, not a failed login.
    """
    __tablename__ = "access_log"
    __table_args__ = (Index("ix_access_log_username_created_at", "username", "created_at"),)

    id: int | None = Field(default=None, primary_key=True)
    username: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    path: str = Field(index=True)
    method: str
    ip: str | None = None
    user_agent: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    created_at: datetime = Field(default_factory=utcnow)
