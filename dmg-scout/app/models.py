"""Database schema. Every extracted field traces back to a raw_document row with a URL."""
from __future__ import annotations

import enum
from datetime import datetime, timezone

from sqlalchemy import BigInteger, Column, Index, LargeBinary, Text, UniqueConstraint
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
    school_facility_funding = "school_facility_funding"


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
    # Who actually selects the mechanical equipment -- see Project.delivery_method's
    # docstring for the full explanation. Null unless the filing itself states the
    # project delivery method; never inferred from project type, agency, or stage.
    delivery_method: str | None = None
    stage: Stage = Field(default=Stage.unknown)
    filing_type: str | None = None
    summary_one_line: str = ""
    confidence: float = 0.0
    extraction_json: dict = Field(default_factory=dict, sa_column=Column(JSON, nullable=False, default=dict))
    named_people: list = Field(default_factory=list, sa_column=Column(JSON, nullable=False, default=list))
    named_firms: list = Field(default_factory=list, sa_column=Column(JSON, nullable=False, default=list))
    # Which version of app.grounding's guard last validated this row's numeric
    # and named-entity fields. NULL means never checked under a versioned
    # regime (every signal extracted before 2026-08-19). See
    # app.grounding.GROUNDING_VERSION and fix_corpus's own docstring: Blue
    # Owl and FAAC sat on live, board-visible fabricated values for weeks
    # because a RawDocument is only ever extracted once, so a guard shipped
    # after them never re-examined them -- this is what makes that
    # structurally impossible to repeat. `scout grounding --fix` (and the
    # `grounding` pipeline stage) query on this column directly, so a day
    # with no version bump is a cheap no-op query, not a full corpus scan.
    grounding_version: int | None = Field(default=None, index=True)


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
    # days_to_estimated_bid is the point figure (mean, for entitlement --
    # see scoring.days_to_bid_by_stage) used everywhere in scoring math
    # (app.pipeline.notify's reachability window, app.pipeline.scoring's
    # priority_score). low/high are the 95% CI bounds, populated only for a
    # stage with a real measured interval -- null for every stage still on
    # an invented placeholder, never a fake or zero-width range just to
    # fill the columns. See app.pipeline.scoring.days_to_estimated_bid_range.
    days_to_estimated_bid: int | None = None
    days_to_estimated_bid_low: int | None = None
    days_to_estimated_bid_high: int | None = None
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
    # Project delivery method, when a filing states it -- one of
    # design_bid_build, design_build, design_assist, cm_at_risk,
    # progressive_design_build, or null. Rolled up from linked signals in
    # app.pipeline.resolve._absorb, same first-stated-value-wins discipline as
    # water_source_stated above. NEVER inferred from project type, agency, or
    # stage -- public filings frequently don't state this at all, and a wrong
    # guess here sends a rep to the wrong door.
    #
    # This is the field that answers "who do I call": under design-bid-build
    # the engineer of record writes Division 23 and names a basis of design,
    # so the right call is to the MEP firm. Under design-build and
    # design-assist the mechanical contractor selects equipment -- often
    # before a specification even exists -- so the right call is to the
    # contractor. See app/delivery.py's DELIVERY_METHOD_NOTES for the
    # plain-language explanation shown on the board and project page.
    delivery_method: str | None = None
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
    """Logged contact with a human, about EITHER a project or an account --
    app.outreach.log_outreach (the one writer) enforces at least one of
    project_id/account_id is set; there is no unresolved-entity concept
    (see app/web/main.py:capture_confirm). Most rows so far are
    project-scoped (voice capture, Fathom sync, the project page's own
    log); account_id exists for the account detail page
    (app.web.main:/account/{id}), where a rep calls a contractor/GC
    account about their business generally, not about one specific job --
    and most accounts have no live Scout project to attach outreach to at
    all (see app.contractors.match_account_to_cslb)."""
    __tablename__ = "outreach"

    id: int | None = Field(default=None, primary_key=True)
    project_id: int | None = Field(default=None, foreign_key="projects.id", index=True)
    account_id: int | None = Field(default=None, foreign_key="accounts.id", index=True)
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


class SourceRowSeen(SQLModel, table=True):
    """The nightly diff's own memory: one row per (source, natural_key) ever
    observed in one of the five Pipeline B tables that have no integer id to
    hang DigestLog's existing new/stage_change/contactable machinery off of
    (hcai_projects, ab869_plans, ab802_buildings, opsc_projects,
    scaqmd_facilities -- see docs/DAILY-BRIEF-DESIGN.md section 1b and
    app/pipeline/diffs.py for the full design and the per-table fingerprint
    field lists, registered in app/assumptions.py).

    natural_key is always a string, even for a table whose real key is
    composite (Ab802Building's (portfolio_manager_property_id, year_ending),
    ScaqmdFacility's (facility_id, source)) -- see app.pipeline.diffs for
    the exact join format, kept in one place so a writer and reader can't
    drift apart, same discipline as source_run_name/run_name_source above.

    fingerprint is a deliberately narrow, per-table subset of fields (never
    every column -- imported_at alone would fire a false "changed" on every
    reload), so a real content change is what advances it, not a reload
    that reread the same row.

    A row disappearing from its source table is NOT deleted here --
    removed_at is set instead and the row stays. Silently dropping a row
    that vanished from a source file is exactly the kind of miss this
    system's invariant 6 ("no join hides its miss rate") exists to prevent;
    a removed row is itself a fact worth surfacing, not just an absence to
    stop tracking. A row that reappears later (removed_at cleared, fresh
    fingerprint stored) is still fully trackable going forward.

    The FIRST diff run ever against a given source is a BASELINE: every
    row present is seeded here with no new/changed/removed reported at all
    -- there is no prior snapshot to compare a from-scratch load against,
    so treating the whole table's existing history as "new today" would be
    wrong, not just noisy. See app.pipeline.diffs.diff_source.

    changed_at (added 2026-09-11, for the daily brief's "new or changed
    since yesterday" section): the timestamp of the most recent GENUINE
    fingerprint change, set ONLY on diff_source's changed branch -- never
    touched by the bulk "unchanged" update, which advances last_seen_at
    for every present row every night regardless of whether anything about
    it changed. That distinction matters: last_seen_at being recent means
    only "the row was present in last night's file," true of nearly every
    row nearly every night, so it cannot answer "did this change." NULL
    until the first real change is observed; never reset."""
    __tablename__ = "source_rows_seen"
    __table_args__ = (UniqueConstraint("source", "natural_key", name="uq_source_row_seen"),)

    id: int | None = Field(default=None, primary_key=True)
    source: str = Field(index=True)
    natural_key: str = Field(index=True)
    fingerprint: str = Field(default="", sa_column=Column(Text, nullable=False, default=""))
    first_seen_at: datetime = Field(default_factory=utcnow, index=True)
    last_seen_at: datetime = Field(default_factory=utcnow, index=True)
    changed_at: datetime | None = Field(default=None, index=True)
    removed_at: datetime | None = Field(default=None, index=True)


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
    firm_type: str = "unknown"  # mep | architect | civil | structural | mech_contractor | gc | developer | consultant | unknown
    aliases: list = Field(default_factory=list, sa_column=Column(JSON, nullable=False, default=list))
    added_from: str = "roster"  # roster | extraction | dashboard
    created_at: datetime = Field(default_factory=utcnow)


class ProjectFirm(SQLModel, table=True):
    __tablename__ = "project_firms"
    __table_args__ = (UniqueConstraint("project_id", "firm_id", "role", name="uq_project_firm_role"),)

    id: int | None = Field(default=None, primary_key=True)
    project_id: int = Field(foreign_key="projects.id", index=True)
    firm_id: int = Field(foreign_key="firms.id", index=True)
    role: str = "unknown"  # engineer_of_record | architect | mep_engineer | civil_engineer |
                           # structural_engineer | gc | mech_contractor | developer | consultant
    linked_at: datetime = Field(default_factory=utcnow)


class DeveloperDesignTeam(SQLModel, table=True):
    """A developer's recurring architect / MEP engineer / engineer of record,
    rolled up from ProjectFirm + Project.developer across every project that
    names them -- see app.developer_team.seed_from_project_firms and its
    module docstring for why this exists: a developer that has used the same
    design team on three prior projects is worth naming as a lead even on a
    fourth project where no document has named an engineer yet.

    NEVER this project's engineer of record -- a project's own ProjectFirm
    row (extracted from a document about THIS project) always wins; a row
    here is only ever shown labeled "usual team," same as FieldIntel's own
    discipline of never overwriting a documented fact with an inferred one.

    source='extracted' rows are a fully repeatable rollup
    (seed_from_project_firms deletes and rebuilds every one of them from
    ProjectFirm + Project.developer on each run) -- evidence_project_ids is
    exactly the project_id list that produced it, so a caller can always
    show its work. source='manual' rows are a rep's own knowledge entered on
    the developer page; reason and confirmed_by are required for those, same
    discipline as ManualCorrection, because there is no document behind a
    rep's memory either -- seed_from_project_firms never touches these.
    """
    __tablename__ = "developer_design_team"
    __table_args__ = (
        UniqueConstraint("developer_norm", "firm_id", "role", name="uq_developer_design_team"),
    )

    id: int | None = Field(default=None, primary_key=True)
    developer: str = Field(index=True)       # as first seen/entered, for display
    developer_norm: str = Field(index=True)  # normalize_name(developer) -- the real match key
    firm_id: int = Field(foreign_key="firms.id", index=True)
    role: str = Field(index=True)            # architect | mep_engineer | engineer_of_record
    evidence_project_ids: list = Field(default_factory=list, sa_column=Column(JSON, nullable=False, default=list))
    source: str = "extracted"                # extracted | manual
    reason: str | None = None                # required for source == 'manual'
    confirmed_by: str | None = None          # required for source == 'manual'
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class FieldIntel(SQLModel, table=True):
    """Human-sourced project intelligence: what a GC, engineer, or owner
    told a rep in conversation, before any of it exists as a public
    document -- a GC naming the engineer and mechanical sub on a job he's
    pursuing, weeks or months before a filing would ever surface it.

    Deliberately a separate table from Project, not a Project row with a
    provenance flag. Every existing Project invariant assumes a document
    chain this record never has: app.ops.doctor's project_evidence check
    requires >=1 linked Signal for any ACTIVE_STATUSES project;
    app.pipeline.resolve's auto-merge and app.duplicates' dedup both treat
    two Projects converging on the same name/location as the same
    real-world thing needing a merge; app.brief.build_brief's timeline is
    built exclusively from ProjectSignal->Signal->RawDocument links, so a
    signal-less Project would render as "nothing found," not "sourced from
    a conversation." None of those are safe to inherit silently, so this
    is its own table with its own page, its own board section, and its own
    (verbatim, never LLM-narrated -- see app.pipeline.notify) digest line.

    reported_by/reported_at/source_notes ARE the source -- required,
    because unlike everything else in this schema there is no document
    behind this to fall back on. Every other field is optional and never
    inferred: empty means the person didn't address it, not "unknown" or
    a guessed default. engineer_/mech_contractor_*_id are populated once,
    at creation, by an EXACT normalized-name match against the same
    Firm/Account rosters app.firms.match_firm and the account-roster CSLB/
    firm joins already use -- never fuzzy, never guessed, same discipline
    as every other join this session has built. confirmed_project_id is
    set ONLY by a human explicit action (app.field_intel.confirm_field_intel)
    when a later public filing turns out to be the same job -- never by an
    automated match, which is exactly the "silent merge" this table exists
    to avoid."""
    __tablename__ = "field_intel"

    id: int | None = Field(default=None, primary_key=True)

    # The source. Required -- this table's whole reason to exist.
    reported_by: str = Field(index=True)          # named person, e.g. "Dave Kim, ACME GC's PM"
    reported_at: datetime = Field(index=True)     # when the conversation happened, not when typed in
    source_notes: str = Field(sa_column=Column(Text, nullable=False))  # what they said, their words

    # The pipe. Every field optional; absence means unaddressed, never guessed.
    owner: str | None = None
    location: str | None = None
    size_scope: str | None = None
    stage: Stage = Field(default=Stage.unknown)   # same enum/vocabulary the board uses elsewhere
    expected_timing: str | None = None

    engineer_name: str | None = None
    engineer_firm_id: int | None = Field(default=None, foreign_key="firms.id")
    engineer_account_id: int | None = Field(default=None, foreign_key="accounts.id")

    mech_contractor_name: str | None = None
    mech_contractor_firm_id: int | None = Field(default=None, foreign_key="firms.id")
    mech_contractor_account_id: int | None = Field(default=None, foreign_key="accounts.id")

    # Confirmation -- see confirm_field_intel. Never set by anything but an
    # explicit human action naming the project it turned out to be.
    confirmed_project_id: int | None = Field(default=None, foreign_key="projects.id", index=True)
    confirmed_at: datetime | None = None
    confirmed_by: str | None = None

    status: str = Field(default="active", index=True)  # active | confirmed | stale
    created_at: datetime = Field(default_factory=utcnow, index=True)
    updated_at: datetime = Field(default_factory=utcnow)


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
    # Null only for a manually-entered correction -- see ManualCorrection
    # and app.pipeline.corrections.apply_manual_correction, its first
    # writer (2026-09-06): a stage override inserts one of these with
    # source='manual' so the ledger stays complete, same as a signal would.
    signal_id: int | None = Field(default=None, foreign_key="signals.id", index=True)
    source: str = "signal"  # signal | manual
    created_at: datetime = Field(default_factory=utcnow)


class ManualCorrection(SQLModel, table=True):
    """A human overriding stage/mw_it/mw_total/delivery_method directly --
    the ONLY path allowed to move these backward (stage), shrink them
    (mw_it/mw_total), or replace an already-stated one (delivery_method).
    See app.pipeline.corrections' module docstring for the full design
    (the RATCHET BUG diagnosis and RATCHET OVERRIDE proposal, both
    2026-09-06) and apply_manual_correction, the only writer.

    Writing a row here PINS the field: app.pipeline.resolve._absorb() and
    app.pipeline.size_score.run_size_score()'s best() both check the most
    recent correction for a (project_id, field) pair before applying their
    own forward-only/max-only rule, and silently discard any signal whose
    observed_at (or created_at fallback, same as StageObservation.from_event)
    is at or before corrected_at -- that evidence was already available (or
    contemporaneous) when the human decided, so it cannot un-decide it. A
    signal observed AFTER corrected_at that would still move the field is
    not applied automatically either -- see PinnedFieldConflict.

    old_value is always read from the live Project row inside the same
    transaction that writes this correction, never accepted from whatever
    form or caller supplied new_value -- a claimed old_value could be
    stale or simply wrong, and this table exists specifically to be a
    trustworthy audit record.

    field/new_value are both stored as plain strings (an enum's .value for
    stage, a plain number-string for mw_it/mw_total, the string itself for
    delivery_method) -- one shape covers all four correctable fields
    without three near-identical tables, since nothing here ever needs to
    query or aggregate across mixed value types, only display them.
    """
    __tablename__ = "manual_corrections"
    __table_args__ = (
        Index("ix_manual_corrections_project_field_at", "project_id", "field", "corrected_at"),
    )

    id: int | None = Field(default=None, primary_key=True)
    project_id: int = Field(foreign_key="projects.id", index=True)
    field: str = Field(index=True)  # stage | mw_it | mw_total | delivery_method
    old_value: str | None = None
    new_value: str
    reason: str = Field(sa_column=Column(Text, nullable=False))
    corrected_by: str
    corrected_at: datetime = Field(default_factory=utcnow, index=True)


class PinnedFieldConflict(SQLModel, table=True):
    """A signal observed AFTER a ManualCorrection's corrected_at that would
    still move the pinned field forward (stage/mw_it/mw_total) or propose a
    different value (delivery_method) -- surfaced for a human to confirm or
    reject, never applied automatically. The forward-only rule alone is no
    longer sufficient justification once a human has actively overridden a
    field; this is the fork in app.pipeline.resolve._absorb() that runs
    instead of writing straight to Project once a pin exists.

    Deduped on (project_id, field, signal_id) by the writer
    (app.pipeline.corrections.queue_pin_conflict), not a DB constraint --
    same one-decision-can't-double-insert discipline as
    StageObservation.uq_stage_observation_signal, but this row is deleted
    (not merely made unreachable) once resolved, so a unique constraint
    would only complain about a case the caller already checked for.
    """
    __tablename__ = "pinned_field_conflicts"

    id: int | None = Field(default=None, primary_key=True)
    project_id: int = Field(foreign_key="projects.id", index=True)
    field: str = Field(index=True)
    signal_id: int = Field(foreign_key="signals.id", index=True)
    correction_id: int = Field(foreign_key="manual_corrections.id", index=True)
    pinned_value: str
    candidate_value: str
    status: str = Field(default="pending", index=True)  # pending | confirmed | rejected
    created_at: datetime = Field(default_factory=utcnow)
    resolved_at: datetime | None = None
    resolved_by: str | None = None


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
    # NULL means unresearched, same discipline as every other *_verified
    # field on this model -- a default of True here would be exactly the
    # inference this app refuses to make everywhere else (see
    # CompetitorLine.covered_counties: empty means not researched, not
    # covers everywhere). True is only ever written alongside
    # existence_verified_basis explaining how the company was confirmed to
    # exist. False means exactly one thing downstream: excluded from
    # app.schedule_mapping's recommendation lists (our_lines_for_role) --
    # never named as "consider this line" until confirmed. NOT excluded from
    # basis-of-design/approved-equal MATCHING against a document's own text:
    # if a real drawing set names it, that is a fact about the document,
    # independent of whether Scout can independently confirm the company.
    # VU Flow Environmental is the one line researched and found false (no
    # resolving domain, no LinkedIn, no trade coverage -- see
    # country_of_manufacture_basis and competes_with_basis on this exact
    # line, researched 2026-08-09, and app.assumptions's "Line card:
    # existence-unverified lines" entry).
    existence_verified: bool | None = Field(default=None, index=True)
    existence_verified_basis: str | None = None
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

    # The ONE exception to this model's own "seeded from config.yaml,
    # edited there" convention stated in the class docstring above --
    # added 2026-09-04 by app.pipeline.line_pitch.discover_domain_via_web_search
    # for the lines whose own basis text names no plausible domain
    # (app.pipeline.line_pitch.candidate_domains). Closer in spirit to
    # CompetitorLine.source_url than to a seeded fact: a manufacturer's
    # domain doesn't change, so it is looked up via ONE web search, per
    # line, ever -- once official_domain is set here, no later run
    # searches again for that line (see candidate_domains, which tries
    # this field first). official_domain_source is 'web_search' when
    # found this way; official_domain_url is the fetched home page that
    # confirmed the line's own name actually appears on it (the
    # plausibility gate -- a web_search citation alone is never trusted).
    official_domain: str | None = None
    official_domain_source: str | None = None  # web_search | null
    official_domain_url: str | None = None

    created_at: datetime = Field(default_factory=utcnow)


class ProductLineBranch(SQLModel, table=True):
    """Which DMG/ToroAire location actually carries a given line -- the
    dimension ProductLine itself doesn't have. ProductLine's own fields
    (oshpd_osp, description, etc.) were all researched against DMG's SoCal
    card; nothing about the line row says whether that coverage holds at
    any other branch.

    The absence of a row for a given (product_line_id, branch) pair means
    unknown -- never inferred as either covered or not covered. Only write
    a row when a branch's coverage of a line is actually known. A branch
    with no line card supplied (Bay Area, Sacramento, Reno as of the
    2026-08-20 seed) gets ZERO rows -- every one of its 70 lines stays
    unknown, not backfilled from a sibling branch's card.

    status is one of:
      - 'confirmed_covered' -- the line is printed on that branch's own
        line card.
      - 'confirmed_not_covered' -- the branch has a line card on file and
        this line is NOT on it. A one-page line card is meant to be a
        complete listing of what the branch carries, so an omission is
        read as evidence of non-coverage, not merely unknown -- but this
        is an inference from an enumerated document, not a literal "we
        don't carry this" quote, and source_detail says so on every row.
      - 'reported_discussion' -- neither of the above; someone reported
        the branch is discussing adding the line, not that it does yet.
        Not used by the card-derived seed below; reserved for a future
        verbal-only report that isn't yet reflected on any card.

    verified is True for every row derived directly from reading a
    branch's own published line-card PDF (a first-party primary source,
    same standing as CompetitorLine's 'confirmed' status) -- source_pdf
    and source_pdf_revision_date name exactly which document and which
    dated revision of it produced the row, so a later, newer card
    supersedes it visibly rather than silently. verified=False is
    reserved for a row sourced only from someone's statement rather than
    a card -- source_detail carries who said it and when, in the same
    prose-citation style as every other *_basis field in this file, e.g.
    "Stated by Andy, 2026-08-19: ...". Either way, source_detail is
    required -- a row being unverified doesn't make it unknown, it's a
    known claim with a named source, which is a different thing from
    having no information at all."""
    __tablename__ = "product_line_branches"

    id: int | None = Field(default=None, primary_key=True)
    product_line_id: int = Field(foreign_key="product_lines.id", index=True)
    branch: str = Field(index=True)  # e.g. "DMG Hawaii" -- free text, not a canonical location enum
    status: str = Field(index=True)  # confirmed_covered | confirmed_not_covered | reported_discussion
    verified: bool = Field(default=False, index=True)
    source_pdf: str | None = None          # filename under docs/line-cards/, e.g. "Line Card DMG Hawaii 01-02-26.pdf"
    source_pdf_revision_date: str | None = None  # the date printed in the filename, e.g. "2026-01-02"
    source_detail: str = Field(sa_column=Column(Text, nullable=False))
    created_at: datetime = Field(default_factory=utcnow)


class RepFirm(SQLModel, table=True):
    """A COMPETING manufacturers' rep firm in DMG/ToroAire's territory --
    the other side of the line card. See app/competitors.py for the
    hand-researched dataset (each firm's own published line card only,
    never a directory or a guess) and its seed_competitor_lines function,
    which writes these rows and CompetitorLine below."""
    __tablename__ = "rep_firms"

    id: int | None = Field(default=None, primary_key=True)
    name: str = Field(index=True, unique=True)
    website: str | None = None
    territory_note: str | None = None


class CompetitorLine(SQLModel, table=True):
    """One manufacturer line -> role -> channel assignment, sourced ONLY
    from that manufacturer's or rep firm's own published line card (never a
    third-party directory, never guessed) -- see app/competitors.py's
    module docstring for the full research method and app/assumptions.py's
    "Competitor line card map" entry for the count of what got confirmed
    vs. not.

    channel is 'rep_firm' (rep_firm_id set) or 'factory_direct' (rep_firm_id
    null -- e.g. Trane, which sells direct in California rather than
    through any independent rep) -- a manufacturer competes against DMG's
    card either way, and which channel it is changes who a rep actually
    calls.

    building_role is null when the line's own published description doesn't
    map cleanly onto any of app.accounts.ROLE_ORDER's 13 roles (most
    accessories, tools, and components a rep firm also carries alongside
    its real equipment lines) -- left null rather than forced into the
    nearest-sounding role, and such rows simply don't appear on the
    per-role competitive surfacing.

    status is 'confirmed' only when a single, current, first-party source
    states the assignment with nothing else contradicting it. 'unconfirmed'
    covers two distinct cases, both recorded rather than resolved by
    picking a side: (1) a real conflict between sources -- e.g. Greenheck,
    where an older third-party directory listing is known to disagree with
    both Greenheck's own live rep locator and the rep firm's own site, which
    currently agree with each other; (2) the SAME manufacturer name also
    appears on DMG's own line card for the same role (Twin City Fan,
    Panasonic, Soler & Palau, Airzone) -- a rep firm's published card and
    DMG's own card both claiming the identical line is exactly the kind of
    thing this table exists to surface honestly, not paper over."""
    __tablename__ = "competitor_lines"

    id: int | None = Field(default=None, primary_key=True)
    manufacturer: str = Field(index=True)
    building_role: str | None = Field(default=None, index=True)  # ROLE_ORDER value, or null
    channel: str = Field(default="rep_firm", index=True)  # rep_firm | factory_direct
    rep_firm_id: int | None = Field(default=None, foreign_key="rep_firms.id", index=True)
    status: str = Field(default="confirmed", index=True)  # confirmed | unconfirmed
    conflict_note: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    source_url: str
    retrieved_at: datetime = Field(default_factory=utcnow)
    # Counties this row's source (source_url, as of retrieved_at) actually
    # names as covered -- e.g. Sigler SoCal Engineering's own locations page
    # names 5 of Scout's 7 territory counties by name and excludes Imperial
    # and Kern. Empty list means no county-level coverage was researched for
    # this row -- NOT "covers everywhere" -- callers that need a coverage
    # decision (app/schedule_mapping.py's resolve_displacement) must treat
    # an empty list as "no restriction to apply" rather than as evidence of
    # statewide coverage, since those are two different unknowns.
    covered_counties: list = Field(default_factory=list, sa_column=Column(JSON, nullable=False, default=list))


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
    # Never stated in the source roster -- NULL, not 0. See
    # app.importers.account_roster_csv, the only writer of this field.
    annual_revenue: float | None = None
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
    lead could be built from, because THIS PARTICULAR CHHS dataset ("Total
    Construction Cost of Healthcare Projects") is aggregated by county+status
    only. Correction, 2026-08-17: this docstring previously claimed HCAI's
    per-project detail (facility names, addresses) "lives behind HCAI's
    login-gated eServices portal — confirmed not publicly reachable" and can
    "never be more than a county-level trend layer" — that is TRUE of the
    plan-review/permit-stage workflow tracking specifically (still eServices-
    only, still not publicly reachable), but it is not true of HCAI's own
    seismic-rating data: "Seismic Ratings and Collapse Probabilities of
    California Hospitals" is a SEPARATE, genuinely per-building CHHS dataset
    with facility name, building name/number, and lat/lon — see
    HospitalBuilding below. The two datasets answer different questions
    (construction financials/status vs. seismic compliance) and only the
    latter turned out to be facility-identified.

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


class HospitalBuilding(SQLModel, table=True):
    """One row per seismically-separate California hospital building, from
    HCAI's public CHHS Open Data CSV "Seismic Ratings and Collapse
    Probabilities of California Hospitals" — see app/pipeline/hcai.py for
    the import, the SPC/NPC-to-deadline derivation, and the CHHS Terms of
    Use findings (assumptions register, group "Hospital seismic
    compliance").

    A DIFFERENT population from Project and from RetrofitBuilding, on
    purpose — this is a different sale (a hospital's facilities/capital
    team retrofitting or replacing an existing building under a statutory
    seismic deadline) to a different buyer, with a different regulatory
    driver (SB 1953, not a data-center lease or an assessor-derived
    replacement-candidate proxy). Never merged into the project board.

    spc_rating / npc_rating are HCAI's own raw codes, kept verbatim
    (including the trailing "s" HCAI uses for an unverified/self-reported
    rating, and "N/A"/"NYA" placeholders) — never coerced to a bare int,
    so a reader can see exactly what HCAI itself published. spc_deadline_year
    and npc_deadline_year are THIS APPLICATION'S derived interpretation of
    the base SB 1953 statutory schedule (SPC-1 -> 2020, SPC-2 -> 2030;
    "meets the 2030 standard" requires SPC >= 3 AND NPC = 5) — not an HCAI-
    published field, and per CHHS's own Terms of Use ("you may not claim the
    data is 'official government data'... must clearly indicate the data
    has been modified"), every place this renders must show it as Scout's
    own derived read, alongside the raw HCAI codes it was derived from, not
    in place of them. has_filed_extension is a coarse yes/no only (ANY
    non-blank value across HCAI's several extension-bill columns in the
    companion "Seismic Deadline Extensions Granted" dataset) — deliberately
    NOT a computed extended deadline date: those columns use inconsistent
    date formats across six different extension bills (SB 306, AB 523,
    SB 1661/AB 2557/AB 81, SB 499, SB 90, AB2190) each with different scope
    and history, and guessing at which one governs a given building's
    ACTUAL current deadline is exactly the false-precision this system
    abstains from elsewhere. A building flagged here needs a human to read
    HCAI's own extension record, not a number this system invented.
    """
    __tablename__ = "hospital_buildings"
    __table_args__ = (UniqueConstraint("perm_id", "building_nbr", name="uq_hospital_building"),)

    id: int | None = Field(default=None, primary_key=True)
    perm_id: str = Field(index=True)          # HCAI Facility Identification Number
    building_nbr: str = Field(index=True)     # HCAI's per-building number within a facility
    facility_name: str = Field(index=True)
    building_name: str | None = None
    building_status: str | None = None        # e.g. "OSHPD 1-In Service", "OSHPD 1-Under Construction"
    city: str | None = None
    county: str = Field(index=True)
    state: str = Field(default="CA", index=True)

    spc_rating: str | None = Field(default=None, index=True)   # HCAI's raw code: "1".."5", "1s".."5s", "4D", "N/A", "NYA"
    npc_rating: str | None = Field(default=None, index=True)   # HCAI's raw code: "1".."5", "3R", "4D-L1", "4D-L2", "N/A", "NYA"
    hazus_2010_pct: float | None = None       # 2010 HAZUS collapse-probability score, percent
    ab1882_notice: str | None = None          # HCAI's own plain-language risk note, verbatim when present

    latitude: float | None = None
    longitude: float | None = None

    # Derived -- see class docstring's "spc_rating / npc_rating" paragraph.
    spc_deadline_year: int | None = None
    npc_deadline_year: int | None = None
    meets_2030_standard: bool | None = None   # SPC in {3,3s,4,4s,4D,5,5s} AND NPC == "5"
    has_filed_extension: bool = Field(default=False, index=True)

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


class EbeweBenchmark(SQLModel, table=True):
    """One building's annual LA EBEWE benchmark filing — data.lacity.org
    Socrata dataset 9yda-i4ya. See app/pipeline/ebewe.py.

    ain_last3 is EBEWE's own documented field (Socrata calls the column
    "AIN" and its dataset description says it is "the last 3 digits of the
    Assessor Identification Number") — NOT a usable join key alone (three
    digits collide constantly across 60,000+ retrofit_buildings rows,
    confirmed 2026-08-16), but a free independent checksum against a
    text-address join's result — see
    app.pipeline.ebewe:ebewe_matches_by_normalized_address, which uses it
    to drop matches the checksum disagrees with rather than trust the
    address text alone.

    building_id is the LADBS Building ID (12 digits, distinct from AIN) --
    the key the A/RCx audit compliance cycle (LAMC Table 9708.2, see
    app.pipeline.regulatory:arcx_compliance_status) is actually keyed to.

    Keyed (building_id, program_year): EBEWE is an ANNUAL filing, so the
    same physical building recurs across years with different usage
    figures. Every year is stored (never overwritten) — the join in
    app.pipeline.ebewe always resolves to the MOST RECENT program_year per
    building_id, but older years stay on file rather than being discarded,
    the same discipline app/pipeline/permits.py applies to permit history.
    """
    __tablename__ = "ebewe_benchmarks"
    __table_args__ = (UniqueConstraint("building_id", "program_year", name="uq_ebewe_building_year"),)

    id: int | None = Field(default=None, primary_key=True)
    source: str = Field(default="la_ebewe_benchmarking", index=True)
    building_id: str = Field(index=True)
    program_year: int = Field(index=True)
    ain_last3: str | None = None  # see docstring — checksum only, never a join key alone
    building_address: str | None = None
    postal_code: str | None = None
    primary_property_type: str | None = None
    property_gfa: float | None = None
    year_built: int | None = None
    occupancy: float | None = None
    compliance_status: str | None = None
    organization: str | None = None
    number_of_buildings: int | None = None
    site_eui: float | None = None
    source_eui: float | None = None
    weather_normalized_site_eui: float | None = None
    weather_normalized_source_eui: float | None = None
    percent_diff_national_median_site_eui: float | None = None
    percent_diff_national_median_source_eui: float | None = None
    # ENERGY STAR 1-100 national percentile score, "Not Available" (-> null)
    # when usage was estimated rather than metered (see LADBS's own A/RCx
    # FAQ #4: ESPM will not assign a score to estimated usage) or when the
    # property type has no ENERGY STAR scoring model.
    energy_star_score: int | None = None
    energy_star_cert_years: str | None = None
    total_ghg_emissions: float | None = None
    indoor_water_use: float | None = None
    indoor_water_use_intensity: float | None = None
    outdoor_water_use: float | None = None
    total_water_use: float | None = None
    source_url: str
    imported_at: datetime = Field(default_factory=utcnow, index=True)


class OpscProject(SQLModel, table=True):
    """One row per Application_Number in OPSC's own "School Facility
    Program Funding" bulk CSV (data.ca.gov, CKAN package
    dd1eabf1-0b66-49d6-857d-8cef6ed93d45) -- see app/pipeline/opsc.py's
    module docstring for the fetch/compliance details.

    Full-replaced on every monthly load (the whole table, not per-year like
    Ab802Building -- OPSC's own file is one continuously-updated snapshot of
    every application ever filed, not an annual series): delete every row,
    reinsert fresh from the new CSV. Field names are 1:1 with the CSV's own
    header row, no renaming for style -- see app/pipeline/opsc.py:parse_rows.

    Numeric vs. text typing per field follows the CKAN datastore's OWN
    declared field type (checked live against the datastore_search API,
    2026-09-06/07), not a guess: Preliminary_Grant_Application,
    Environmental_Hardship_Application, Reduced_to_Costs_Incurred,
    Number_of_Elementary_School_Pupil_Grants_Requested,
    Grade_Level_of_Project, CSFA_Lease_Amount, CTEFP_Loan_Amount,
    Type_of_Joint_Use_Facility, Type_of_Joint_Use_Partner, Industry_Sector,
    Portables_Replaced, and Status are all declared type "text" by the
    datastore itself (several despite being funding-shaped columns) and are
    stored here as plain strings, verbatim, never coerced to a number the
    source itself doesn't claim; every other Application/Amount/Grants-
    Requested column is declared "numeric" and stored as a float.

    Deliberately NO address, latitude, or longitude field: OPSC's own file
    states none, and School_Name is not a safe geocoding input (a district
    can run more than one same-named site, e.g. "Lincoln Elementary" in
    unrelated cities) -- never inferred, never guessed at."""
    __tablename__ = "opsc_projects"

    id: int | None = Field(default=None, primary_key=True)
    county: str | None = Field(default=None, index=True)
    district: str | None = Field(default=None, index=True)
    school_name: str | None = None
    program: str | None = Field(default=None, index=True)
    application_number: str = Field(index=True, unique=True)
    applicant: str | None = None
    preliminary_grant_application: str | None = None
    full_grant_application: float | None = None
    site_and_design_application: float | None = None
    site_only_application: float | None = None
    design_only_application: float | None = None
    environmental_hardship_application: str | None = None
    reduced_to_costs_incurred: str | None = None
    number_of_elementary_school_pupil_grants_requested: str | None = None
    number_of_middle_school_pupil_grants_requested: float | None = None
    number_of_high_school_pupil_grants_requested: float | None = None
    number_of_non_severe_school_pupil_grants_requested: float | None = None
    number_of_severe_school_pupil_grants_requested: float | None = None
    grade_level_of_project: str | None = None
    state_share_of_funding: float | None = None
    site_acquisition: float | None = None
    financial_hardship: float | None = None
    csfa_lease_amount: str | None = None
    ctefp_loan_amount: str | None = None
    type_of_joint_use_facility: str | None = None
    type_of_joint_use_partner: str | None = None
    industry_sector: str | None = None
    portables_replaced: str | None = None
    last_sab_date: datetime | None = Field(default=None, index=True)
    status: str | None = Field(default=None, index=True)

    in_territory: bool = Field(default=False, index=True)
    source_url: str
    imported_at: datetime = Field(default_factory=utcnow, index=True)


class OpscWorkload(SQLModel, table=True):
    """One row per application on OPSC's own SAB Modernization or New
    Construction Workload List PDF (dgs.ca.gov/OPSC Workload-Lists) --
    applications currently in house, NOT yet funded, so they never appear
    in OpscProject at all. See app/pipeline/opsc.py's module docstring for
    the PDF-parsing approach and its own stated fallback (report the parse
    failure rate and stop, if the table doesn't parse cleanly).

    Full-replaced per `program` on every monthly load, same reasoning as
    OpscProject: this is a snapshot of what's in house RIGHT NOW, not a
    history to preserve row-by-row.

    label is always "application in house, not funded" -- OPSC's own PDF
    disclosure, verbatim, not this app's editorializing: see the source
    dgs.ca.gov page's own text, quoted in the module docstring."""
    __tablename__ = "opsc_workload"

    id: int | None = Field(default=None, primary_key=True)
    program: str = Field(index=True)  # "Modernization" | "New Construction"
    district: str | None = None
    school_name: str | None = None
    application_number: str | None = Field(default=None, index=True)
    label: str = "application in house, not funded"
    raw_row_text: str = Field(sa_column=Column(Text, nullable=False))
    source_url: str
    imported_at: datetime = Field(default_factory=utcnow, index=True)


class Ab802Building(SQLModel, table=True):
    """One building's annual AB 802 statewide benchmarking submission --
    energy.ca.gov's own yearly "Download submitted {year} benchmarking
    information" file. See app/pipeline/ab802.py.

    Keyed (portfolio_manager_property_id, year_ending): AB 802 is an ANNUAL
    filing, same shape as EbeweBenchmark's (building_id, program_year) --
    every year is stored, never overwritten, so year-over-year EUI change
    is queryable. A re-fetch of one year's file FULL-REPLACES only that
    year's rows (delete where year_ending == year, then reinsert) -- other
    years on file are untouched, same discipline permits.py applies to
    permit history.

    Field names are 1:1 with the published file's own header row (see
    app/pipeline/ab802.py:_parse_row for the exact header string each one
    came from) -- no renaming for style, so a column here is always
    traceable back to exactly what the file called it.

    Deliberately NO owner column -- see AB 802 statewide benchmarking's own
    assumptions-register entry: this file's `Address 1`/city/lat-long
    identify the BUILDING (self-reported to satisfy AB 802), never an
    owner of record, and this app's own verified finding elsewhere
    (RetrofitBuilding's docstring) is that no free, bulk-queryable LA
    County assessor owner-name source exists at all. Where a match to
    EbeweBenchmark exists, that dataset's own `organization` field is
    carried here as `benchmarking_filer` -- the entity that FILED the LA
    EBEWE benchmark, not a verified owner, and never labeled "owner" on
    the board for exactly that reason.

    assessor_match_method/assessor_match_distance_m/retrofit_apn: a loose
    join to RetrofitBuilding (LA County only -- the only county this app's
    assessor population covers), lat/long-first (RetrofitBuilding.latitude/
    longitude, itself rejoined from RetrofitGeocode) within 30m, falling
    back to app.pipeline.retrofit:normalize_address text matching -- see
    app/pipeline/ab802.py:match_to_retrofit for the same abstain-don't-
    guess ambiguity exclusions app.pipeline.ebewe already applies (a match
    on either side claimed by more than one row on the other is dropped,
    not guessed at). Null outside LA County: there is no assessor
    population to join against, by design, not a join failure.
    """
    __tablename__ = "ab802_buildings"
    __table_args__ = (
        UniqueConstraint("portfolio_manager_property_id", "year_ending", name="uq_ab802_property_year"),
    )

    id: int | None = Field(default=None, primary_key=True)
    portfolio_manager_property_id: str = Field(index=True)
    standard_id: str | None = None
    property_name: str | None = None
    address_1: str | None = None
    city: str | None = None
    state_province: str | None = None
    postal_code: str | None = None
    property_gfa_sqft: float | None = None
    primary_property_type: str | None = Field(default=None, index=True)
    all_property_use_types: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    weather_normalized_site_eui: float | None = None
    natural_gas_use_kbtu: float | None = None
    electricity_grid_purchase_kbtu: float | None = None
    electricity_onsite_renewable_kbtu: float | None = None
    fuel_oil_2_use_kbtu: float | None = None
    district_steam_use_kbtu: float | None = None
    diesel_use_kbtu: float | None = None
    propane_use_kbtu: float | None = None
    district_hot_water_use_kbtu: float | None = None
    district_chilled_water_use_kbtu: float | None = None
    year_built: int | None = Field(default=None, index=True)
    w_energy: str | None = None
    energy_star_score: int | None = None
    energy_star_certified: str | None = None
    energy_star_cert_years: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    county_from_geocoding: str | None = Field(default=None, index=True)
    report_generation_date: str | None = None
    used_estimated_energy_values: str | None = None
    alert_partial_year_data: str | None = None
    total_ghg_emissions_metric_tons: float | None = None
    ghg_emissions_intensity: float | None = None
    year_ending: int = Field(index=True)
    number_of_buildings: int | None = None

    # Derived, not from the file -- see app.pipeline.size_score.in_territory,
    # reused as-is against config.yaml's territory.CA county list.
    in_territory: bool = Field(default=False, index=True)

    # Loose join to RetrofitBuilding -- see class docstring. apn is a plain
    # string, not a foreign key: RetrofitBuilding is fully deleted and
    # reinserted on every retrofit rebuild, same reason OwnershipRecency/
    # RetrofitGeocode avoid a hard FK to it.
    assessor_match_method: str | None = Field(default=None, index=True)  # latlong | normalized_address
    assessor_match_distance_m: float | None = None
    retrofit_apn: str | None = Field(default=None, index=True)

    # Loose join to EbeweBenchmark, address-only (EbeweBenchmark carries no
    # lat/long) -- see class docstring for why organization is relabeled
    # benchmarking_filer rather than owner.
    ebewe_building_id: str | None = None
    benchmarking_filer: str | None = None

    # Loose join to ScaqmdFacility, normalized-address text only (neither
    # side has lat/long here) -- see app.pipeline.scaqmd's module docstring.
    # Recomputed fresh on every scaqmd_facility load, across all years on
    # file, not just the latest -- a real building's address doesn't change
    # year to year. Ambiguous on either side (two ScaqmdFacility rows or two
    # AB802 rows sharing one normalized address) is dropped, never guessed.
    air_permit_facility_id: str | None = Field(default=None, index=True)
    air_permit_match_method: str | None = None  # "normalized_address" or null

    source_url: str
    imported_at: datetime = Field(default_factory=utcnow, index=True)


class ScaqmdFacility(SQLModel, table=True):
    """One row per South Coast AQMD permitted facility -- FACILITY grain
    only, never equipment/permit grain. See app/pipeline/scaqmd.py's module
    docstring for the full access investigation: FIND (South Coast AQMD's
    own facility/equipment lookup) and Public Document Search (permits to
    operate, permit public notices) both live on hosts whose robots.txt
    disallows every user agent entirely -- PlanetBids-shaped, blocked
    regardless of method -- so neither was ever queried, not even once.
    This table exists ONLY because a real, un-blocked bulk alternative was
    found and verified: South Coast AQMD's own "Facilities Notified"
    Annual Emissions Reporting list, a plain XLSX linked directly from
    aqmd.gov/home/rules-compliance/compliance/annual-emission-reporting
    (not the blocked application host). Field names below are 1:1 with
    that file's own header row.

    Deliberately NO equipment, permit number, or capacity field: this
    source's own Notes sheet states plainly this is a facility-notification
    list, not a permit database, and none of those columns exist in it.
    An "air permit on file" flag naming this Facility ID is the honest
    ceiling of what this source can claim -- never "has a boiler," never
    "boiler capacity X."

    in_territory is True for every row, unconditionally, not computed
    against a county field -- there isn't one in the source file (only
    city/zip). This is not a guess: South Coast AQMD's own jurisdiction
    (LA, Orange, and the non-desert portions of Riverside and San
    Bernardino counties) is a strict subset of Scout's own 7-county DMG
    territory, so every facility in a SOUTH COAST AQMD file is, by
    construction, already in territory -- the source itself IS the
    territory filter, not a separate check this app performs.

    CARB's own Facility Search Tool (ww2.arb.ca.gov/facility-search-tool)
    was initially thought unreachable in Phase A (it renders as a
    JavaScript single-page application) -- corrected once it was confirmed
    the actual form lives in a plain HTML iframe
    (www.arb.ca.gov/app/emsinv/iframe/facinfo/facinfo.php), on a host whose
    own robots.txt is clean (Allow: /, 2s crawl-delay). CARB's own tooling
    still can't be driven headlessly from `app/pipeline` the way the AER
    XLSX is fetched -- its export is a hand-run Playwright pull (district
    "SC" = South Coast AQMD), saved as a static file under docs/carb/ and
    loaded from disk by app.pipeline.scaqmd.load_carb_facilities, same
    "hand-pulled, statically stored" precedent as app/pipeline/ab869.py's
    own PDF corpus (docs/hcai/ab869/raw/). See the assumptions register for
    the full access writeup and the Phase A correction.

    Two independent sources, kept as separate rows under (facility_id,
    source) rather than merged -- both ultimately trace back to South
    Coast AQMD's own permit system and, where a real facility appears in
    both, share the same facility_id (confirmed by inspection, not
    assumed), but their field coverage differs (AER carries the AB2588/
    CTR/Rule-317.1 flags; CARB's own export carries emissions tonnage by
    pollutant, not stored here -- out of scope for a facility-grain table
    with no equipment data either way) and their own full-replace cadences
    are independent, so merging them into one row per facility_id would
    make a partial re-load of one source silently clobber the other's
    fields.

    Full-replaced on every load, same discipline as Ab802Building's own
    year-partitioned table, except this source carries no year dimension
    of its own (it's a live notification list, not an annual series) --
    so the WHOLE table is replaced each run, same as OpscProject's
    continuously-updated snapshot, scoped to WHERE source = the one being
    reloaded (a CARB load must never delete the AER rows, or the reverse).
    """
    __tablename__ = "scaqmd_facilities"

    __table_args__ = (
        UniqueConstraint("facility_id", "source", name="uq_scaqmd_facility_id_source"),
    )

    id: int | None = Field(default=None, primary_key=True)
    facility_id: str = Field(index=True)
    # "aer_facilities_notified" (South Coast AQMD's own bulk XLSX) or
    # "carb" (CARB's Facility Search Tool, CEIDARS-backed) -- the SAME real
    # facility can appear once from each source, under the same facility_id
    # (both ultimately trace back to South Coast AQMD's own permit system),
    # so uniqueness is (facility_id, source), not facility_id alone. See
    # app.pipeline.scaqmd's module docstring for how each is loaded and how
    # "new vs already present" is reported across the two.
    source: str = Field(default="aer_facilities_notified", index=True)
    facility_name: str | None = None
    address: str | None = None
    city: str | None = Field(default=None, index=True)
    zip_code: str | None = None

    # Flags verbatim from the source file's own checkbox columns -- True
    # when checked, False when blank, never inferred. AER-only; always
    # False on a CARB-sourced row (CARB's own export carries emissions
    # tonnage by pollutant instead, not these flags -- not stored here,
    # out of scope for a facility-grain table with no equipment data).
    ab_2588: bool = Field(default=False)
    meets_ctr_threshold: bool = Field(default=False)  # "Criteria Pollutants >= 4tpy (100 tpy for CO)"
    core_ctr_facility: bool = Field(default=False)     # "'Core' CTR Facility (PTE>=250 tpy, ...)"
    ctr_phase_3: bool = Field(default=False)
    rule_317_1: bool = Field(default=False)

    in_territory: bool = Field(default=True, index=True)  # see class docstring -- always True here

    source_url: str
    imported_at: datetime = Field(default_factory=utcnow, index=True)


class BpelsgEngineer(SQLModel, table=True):
    """One row per in-territory Mechanical Engineer license from DCA's own
    free monthly "Board for Professional Engineers, Land Surveyors, and
    Geologists" licensee file -- see app/pipeline/bpelsg.py's module
    docstring for the access investigation and app/assumptions.py's
    "BPELSG mechanical engineer roster" entry for the file date, cadence,
    and column list this was verified against (2026-09-08).

    license_no is the primary key, not a separate autoincrement id --
    DCA's own License Number is already the stable, unique real-world
    identifier for this row, and load_bpelsg_engineers upserts on it
    (idempotent: re-running against the same or a newer month's file
    updates existing rows in place rather than duplicating them).

    Scope, deliberately narrow: License Type == "Mechanical Engineer"
    only, and County in Scout's own 7-county California territory
    (config.yaml's territories.california.counties) only -- every other
    license type and every out-of-territory row is dropped at load time,
    never stored. This is NOT a general BPELSG roster table.

    Deliberately NO firm/employer field: verified directly against the
    real file (2026-09-08) that its own Indiv/Org column is 'I' for
    every single one of its 119,766 rows, statewide, across every
    license type -- there is no organizational/firm-held license data in
    this file at all, so there is nothing to store under a firm column.
    name is a personal name only; the join this table supports is a
    normalized-person-name match, never a firm match (see
    app.pipeline.bpelsg.match_bpelsg_for_project).

    status/expiry are DCA's own License Status ("Active"/"Delinquent")
    and Expiration Date, verbatim -- a Delinquent license is still
    stored, not dropped, since "was licensed, lapsed" is itself a fact
    worth a rep knowing, never silently hidden.

    file_date is the source file's own Box-listed date (the file has no
    internal as-of date field of its own) -- recorded per row so a
    future reload against a newer month's file can be told apart from
    this one without a separate SourceRun lookup."""
    __tablename__ = "bpelsg_engineers"

    license_no: str = Field(primary_key=True)
    name: str
    license_type: str = Field(index=True)  # always "Mechanical Engineer" in this table, stored verbatim
    city: str | None = None
    county: str = Field(index=True)
    status: str = Field(index=True)  # "Active" | "Delinquent", DCA's own verbatim value
    expiry: datetime | None = None
    file_date: datetime = Field(index=True)
    imported_at: datetime = Field(default_factory=utcnow, index=True)


class HcaiProject(SQLModel, table=True):
    """One row per HCAI Facilities Development Division project record --
    the "Projects by County" report (report.hcai.ca.gov, division OSHPD,
    report id 38). See app/pipeline/hcai_projects.py's module docstring
    for the access investigation and app/assumptions.py's "HCAI Facilities
    Development Division project reports" entry for the Phase A findings
    this rests on (2026-09-08/09).

    record_no is the primary key -- DCA's... no, HCAI's own "Record"
    column (ProjNo in the raw export) is already a stable, globally
    unique identifier (confirmed: 45,132 of 45,132 rows had a distinct
    value in the Phase A pull), same "real-world key as PK" choice as
    BpelsgEngineer.license_no. load_hcai_projects upserts on it --
    idempotent, never duplicates a record across reloads.

    parent_no is set when a row is an AMENDMENT of an earlier record
    (4,974 of 45,132 in the Phase A pull) -- both the amendment and its
    parent are loaded as separate rows; nothing is collapsed at load
    time. See app.pipeline.hcai_projects' own docstring for the proposed
    (not decided) parent-collapsing question at display time.

    stage is THIS APPLICATION'S OWN collapse of HCAI's 23 raw Status
    values into five buckets (plan_review, pending_start, in_construction,
    closed, other) -- status_raw is kept verbatim alongside it, never
    replaced, so a reader can always see HCAI's own exact word. See
    app.pipeline.hcai_projects.STAGE_MAP for the full 23-value mapping,
    and this file's own assumptions-register entry for why each choice
    was made. Anything not in the map (a future new HCAI status this
    table has never seen) maps to "other," never dropped and never
    guessed into a more specific bucket.

    is_mechanical is a plain regex match on scope_text (see
    app.pipeline.hcai_projects.MECHANICAL_RE) -- no LLM, deliberately:
    every fact here is already an exact government value, same
    discipline as app.pipeline.opsc's direct-Signal-construction choice.
    A False here means "the keyword set didn't match," never "confirmed
    non-mechanical" -- scope_text is free text HCAI's own compliance
    officers typed, and plenty of real mechanical work has no keyword
    hit (a scope reading only "Emergency Generator Replacement" wouldn't
    match, for instance) or is a red herring (see this table's own
    assumptions-register precision-sample entry for the measured rate).

    report_date is the SOURCE FILE's own "as of" date (parsed from the
    report's own title line, e.g. "...as of 09/08/2026") -- the SAME
    value for every row loaded from one file, never utcnow() or per-row
    imported_at, so staleness is visible on the board without a separate
    SourceRun lookup and a stale file re-loaded later is told apart from
    a fresh one by this column, not by when the load happened to run."""
    __tablename__ = "hcai_projects"

    record_no: str = Field(primary_key=True)
    parent_no: str | None = Field(default=None, index=True)
    facility_id: str = Field(index=True)
    facility_name: str
    facility_address: str | None = None
    county: str = Field(index=True)
    scope_text: str = Field(sa_column=Column(Text, nullable=False, default=""))
    date_in: datetime | None = None
    cost_est: float | None = None
    pct_complete: float | None = None
    status_raw: str = Field(index=True)
    stage: str = Field(index=True)  # plan_review | pending_start | in_construction | closed | other
    is_mechanical: bool = Field(default=False, index=True)
    report_date: datetime = Field(index=True)
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

    # Actual EBEWE benchmark data (data.lacity.org 9yda-i4ya), joined at
    # build time by normalized address -- see
    # app.pipeline.ebewe:ebewe_matches_by_normalized_address. A DIRECT
    # MEASURED-PERFORMANCE claim, unlike ebewe_candidate above (a
    # sqft-threshold proxy for audit SCOPE, not measured performance) --
    # never conflate the two. ebewe_matched is false on the large majority
    # of rows by construction: measured coverage 2026-08-16 was ~9% of all
    # retrofit_buildings (join method + rate in app/assumptions.py) --
    # deliberately NOT a rank_buildings() term for exactly that reason (a
    # ~9%-populated column blended into a board-wide score is the false-
    # precision problem app/assumptions.py exists to flag). Used only as an
    # in-subset tie-breaker on the /retrofit?has_ebewe=true view -- see
    # app/web/main.py:retrofit_board.
    ebewe_matched: bool = Field(default=False, index=True)
    ebewe_building_id: str | None = None
    ebewe_program_year: int | None = None
    ebewe_energy_star_score: int | None = None
    ebewe_site_eui: float | None = None
    ebewe_weather_normalized_site_eui: float | None = None
    ebewe_property_type: str | None = None
    # LAMC Table 9708.2 A/RCx compliance cycle, keyed off ebewe_building_id
    # -- see app.pipeline.regulatory:arcx_compliance_status. Only ever set
    # when ebewe_matched (the cycle needs a real LADBS Building ID; a
    # sqft-proxy ebewe_candidate row without a match has none to key off).
    # A dated legal obligation, useful on its own regardless of the
    # benchmark-data join's coverage -- surfaced as its own flag, not
    # folded into ebewe_matched.
    ebewe_arcx_due_this_year: bool = Field(default=False, index=True)
    ebewe_arcx_next_compliance_date: datetime | None = None

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

    # Rejoined from OwnershipRecency at build time, same DELETE-and-reinsert
    # reason as service_calls_per_year and latitude/longitude above -- see
    # app/pipeline/ownership.py. last_sale_date is the Assessor's own
    # RecordingDate (a Prop 13 reassessment trigger, the strongest publicly
    # available proxy for "this parcel changed hands" -- NOT proof of an
    # arms-length sale, and carries no document type; see that module's
    # docstring for the full compliance finding and both limitations). Feeds
    # rank_buildings' recency_factor -- see that function's docstring.
    last_sale_date: datetime | None = None
    last_sale_source: str | None = None
    last_sale_checked_at: datetime | None = None

    # Portfolio-transaction grouping -- see app/portfolios.py:detect_portfolios.
    # Buildings sharing a recording date within a short window AND close
    # geographic proximity are likely one transaction (a buyer picking up
    # several adjacent aging parcels at once), not independent leads.
    # Computed fresh at the end of every rebuild (a pure derived fact from
    # last_sale_date/latitude/longitude already on THIS table -- no external
    # fetch, so no rebuild-survival table needed the way OwnershipRecency/
    # RetrofitGeocode are). Null/1 means standalone -- no group. group_id is
    # the smallest APN among the group's members, deterministic and stable
    # across rebuilds as long as membership doesn't change. combined_sqft
    # sums every member with a known sqft; members lists every OTHER building
    # in the group (apn/address/sqft/last_sale_date) for display -- this row
    # is not included in its own members list.
    portfolio_group_id: str | None = Field(default=None, index=True)
    portfolio_member_count: int | None = None
    portfolio_combined_sqft: float | None = None
    portfolio_members: list = Field(default_factory=list, sa_column=Column(JSON, nullable=False, default=list))

    # True: every member shares the same APN book/page prefix (first 7 of
    # the 10-digit APN, e.g. "3110007" of "3110007011") -- one physical
    # property recorded as multiple assessor parcels. False: the group
    # spans more than one book/page -- a candidate genuine multi-property
    # transaction. Spot-checked 2026-08-19 against 20 real groups (see
    # app/assumptions.py's "Portfolio-transaction detection" entry): the
    # prefix match is exactly the discriminator that finding turned up --
    # 17 of 20 sampled groups shared a prefix. Null only when standalone
    # (portfolio_group_id is null) -- never null for an actual group.
    portfolio_same_block: bool | None = None

    # Rejoined from RetrofitGeocode at build time, same pattern and same
    # reason as service_calls_per_year above -- this table's own DELETE-and-
    # reinsert rebuild would otherwise wipe it. Null until geocoded; see
    # RetrofitGeocode's docstring and app.contractors for the join this
    # feeds (nearest licensed mechanical contractors).
    latitude: float | None = None
    longitude: float | None = None

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


class PipelineRun(SQLModel, table=True):
    """One row per `scout pipeline` invocation -- see app/pipeline_health.py,
    which writes these and answers "how stale is the data" from them. status
    starts "running" and is meant to be updated to "success" or "failed" at
    the end -- but an external hard kill (OOM, confirmed real 2026-08-13:
    the cron container killed by Render's own OOM killer mid-fetch) never
    reaches that code, and a row stuck at "running" forever is
    indistinguishable from a genuinely live run by status alone.

    heartbeat_at is what actually resolves that: touched periodically
    during a run (see app.pipeline_health.heartbeat -- once per pipeline
    stage, and once per source within fetch specifically, since fetch is
    the stage actually observed running long enough for a coarser
    per-stage-only heartbeat to false-negative). app.pipeline_health.
    reap_stale_runs() reclassifies any "running" row whose heartbeat has
    gone quiet past HEARTBEAT_STALE_MINUTES as "failed" -- called from
    check_and_alert_staleness() and from the top of `scout pipeline` itself,
    so a zombie row self-heals without needing a human to notice it.
    """
    __tablename__ = "pipeline_run"

    id: int | None = Field(default=None, primary_key=True)
    started_at: datetime = Field(default_factory=utcnow, index=True)
    heartbeat_at: datetime | None = Field(default=None, index=True)
    finished_at: datetime | None = None
    status: str = Field(default="running", index=True)  # running | success | failed
    records_processed: int | None = None
    # resource.getrusage(RUSAGE_SELF).ru_maxrss at the end of `scout pipeline`,
    # normalized to bytes -- see app.pipeline_health.peak_rss_bytes(). A run
    # killed externally (OOM) never reaches this write, same caveat as
    # status/finished_at above; PipelineStageRun below is what still has
    # something to say about a run that never got here.
    peak_rss_bytes: int | None = Field(default=None, sa_column=Column(BigInteger, nullable=True))
    error: str | None = Field(default=None, sa_column=Column(Text, nullable=True))


class PipelineStageRun(SQLModel, table=True):
    """One row per pipeline stage per run, recorded right where stage
    success/failure already is (app.cli:pipeline's stage loop) -- see
    app.pipeline_health.record_stage_peak_memory. peak_rss_bytes is the
    process's cumulative high-water mark AS OF this stage finishing, not this
    stage's own share of it (ru_maxrss never decreases), so consecutive rows
    for a run bound where growth happened; the last row for a run that never
    got a finished pipeline_run is the last stage that completed, which
    points at the NEXT stage in run order as the one that OOMed."""
    __tablename__ = "pipeline_stage_run"

    id: int | None = Field(default=None, primary_key=True)
    pipeline_run_id: int = Field(foreign_key="pipeline_run.id", index=True)
    stage: str
    peak_rss_bytes: int = Field(sa_column=Column(BigInteger, nullable=False))
    recorded_at: datetime = Field(default_factory=utcnow)


class StalenessAlert(SQLModel, table=True):
    """One row per staleness-alarm email actually sent -- see
    app/pipeline_health.py:check_and_alert_staleness. Exists solely to rate-
    limit the alert to at most one per 24h: "has a row landed in the last
    24h" gates every send, checked before the row (and the email) go out."""
    __tablename__ = "staleness_alerts"

    id: int | None = Field(default=None, primary_key=True)
    sent_at: datetime = Field(default_factory=utcnow, index=True)


class SamSolicitationCheck(SQLModel, table=True):
    """One row per SAM.gov solicitation this project has ever looked at --
    see app/pipeline/sam_gov.py. Exists for two reasons neither SpecMention
    nor RawDocument can cover on its own:

    1. Idempotency without spending SAM.gov's rate-limited search quota
       twice on the same notice -- a solicitation with no Division 23
       section produces zero SpecMention rows (the explicit instruction:
       "skip design-build solicitations... rather than storing empty
       rows"), so without a row HERE, every run would re-fetch and re-parse
       every attachment of every solicitation that has ever come back
       empty, forever.
    2. An honest denominator. "How many solicitations yielded a usable
       Division 23 section" is meaningless without also counting the ones
       that didn't -- this table is the only place that count lives, since
       SpecMention only ever holds the hits.

    outcome is one of: spec_mentions_found | performance_spec_only |
    ungrounded_mentions_only | no_ufgs_23_series_found |
    design_build_skipped | fetch_failed.
    design_build_skipped is a title/description keyword match (see
    DESIGN_BUILD_KEYWORDS), checked before ever spending an attachment
    download on a notice that was never going to carry a spec book.
    performance_spec_only means real UFGS 23-series section text WAS found
    and read, but it specifies by performance/salient characteristics only
    (the FAR 11.104/11.105-encouraged norm for federal work) with no
    manufacturer named anywhere -- a real, informative result, distinct
    from finding nothing. ungrounded_mentions_only means the model named one
    or more manufacturers but NONE of them appear anywhere in the 23-series
    text it was given (see app.grounding.name_grounded) -- a fabrication
    catch, kept distinct from no_ufgs_23_series_found so a rejected
    hallucination is never silently read as "nothing was there at all."
    no_ufgs_23_series_found covers every other reason no mechanical spec
    section turned up -- genuinely no HVAC scope in this solicitation, an
    attachment that didn't parse, or a design-build notice the keyword check
    missed -- deliberately not split further, since Scout cannot always tell
    those apart from the outside.
    """
    __tablename__ = "sam_solicitation_checks"

    id: int | None = Field(default=None, primary_key=True)
    notice_id: str = Field(index=True, unique=True)
    solicitation_number: str | None = None
    title: str = Field(default="", sa_column=Column(Text, nullable=False, default=""))
    agency: str | None = None  # SAM.gov's fullParentPathName
    state: str | None = Field(default=None, index=True)
    naics_code: str | None = None
    posted_date: datetime | None = None
    outcome: str = Field(index=True)
    detail: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    checked_at: datetime = Field(default_factory=utcnow, index=True)


class SpecMention(SQLModel, table=True):
    """One row per (manufacturer, mention_type) named in a Division 23
    (HVAC) spec section of a SAM.gov solicitation's own attachment PDF --
    see app/pipeline/sam_gov.py. Deliberately NOT a Project or a Signal:
    this is competitive intelligence about who the DESIGN side (the
    specifying A/E firm) is naming as Basis of Design or acceptable
    "or equal" manufacturers, not a lead -- nothing here should ever join
    against the board pipeline or app.pipeline.resolve. See the module
    docstring on why this needed its own table rather than reusing
    RawDocument/Signal.

    mention_type is "basis_of_design" (the single named BOD manufacturer
    for a spec section) or "or_equal" (one entry from that section's
    acceptable-substitute list) -- never blended into one row, since a
    firm naming DMG's line as BOD is a materially different signal than
    naming it as one of four acceptable alternates.

    on_dmg_line_card is computed at write time by normalized-name match
    against app.accounts' line_card names (the same normalize_name()
    app.duplicates/app.firms already use) -- true means a rep can walk
    into this spec with an existing relationship instead of a cold call.
    """
    __tablename__ = "spec_mentions"

    id: int | None = Field(default=None, primary_key=True)
    notice_id: str = Field(index=True)
    solicitation_number: str | None = None
    title: str = Field(default="", sa_column=Column(Text, nullable=False, default=""))
    agency: str | None = None
    state: str | None = Field(default=None, index=True)
    source_url: str | None = None
    specifying_firm: str | None = Field(default=None, index=True)
    spec_section: str | None = None       # e.g. "23 74 00"
    spec_section_title: str | None = None  # e.g. "Packaged Outdoor HVAC Equipment"
    manufacturer_name: str = Field(index=True)
    mention_type: str = Field(index=True)  # basis_of_design | or_equal
    on_dmg_line_card: bool = Field(default=False, index=True)
    dmg_line_card_name: str | None = None  # the matched line_card name, if any
    extraction_basis: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    posted_date: datetime | None = None
    retrieved_at: datetime = Field(default_factory=utcnow, index=True)


class SamGovSearchCall(SQLModel, table=True):
    """One row per call to SAM.gov's search endpoint (api.sam.gov/
    opportunities/v2/search) -- see app.pipeline.sam_gov's rate-limit guard.

    Exists solely to answer "how many search calls has ANY invocation of
    this source made today" -- a personal API key's ~10/day cap is
    confirmed real (a live run hit a 429 on 2026-08-13 after 12 calls in
    one day), and SAM.gov's API exposes no way to ask it directly how much
    quota remains. A manual investigation and a hypothetical future
    scheduled run draw against this SAME log, not separate counters --
    otherwise a scheduled run's own budget check would have no way to know
    a human had already spent part of the day's real quota by hand.
    """
    __tablename__ = "sam_gov_search_calls"

    id: int | None = Field(default=None, primary_key=True)
    called_at: datetime = Field(default_factory=utcnow, index=True)


class Contractor(SQLModel, table=True):
    """One row per CSLB (California Contractors State License Board)
    license -- see app/pipeline/cslb.py. Every field here is copied
    verbatim from CSLB's own public "License Master" bulk file
    (cslb.ca.gov/onlineservices/dataportal/), the free, no-login,
    self-service statewide download CSLB itself publishes for this exact
    purpose (no robots.txt exists on any CSLB domain; their own Conditions
    of Use states no restriction on automated/bulk use; confirmed by
    directly downloading it, 2026-08-14 -- 244,100 real records). Scoped at
    import time to classifications C-20/C-38/B and business addresses in
    LA, Orange, Riverside, San Bernardino, Ventura, San Diego, and Imperial
    counties -- see app.pipeline.cslb.COUNTIES/CLASSIFICATIONS.

    Deliberately NOT augmented with any judgment about a contractor's
    size, quality, or relationship to DMG -- every field below is either a
    direct CSLB field or (latitude/longitude/geocoded_at/geocode_source)
    a disclosed DERIVED field for the geographic join, never a claim about
    the contractor itself. If CSLB doesn't state it, this table doesn't
    have an opinion about it.

    Mailing address only: CSLB's own public data has no separate physical/
    business-address field -- MailingAddress is the only address CSLB
    publishes for a license, so that is what business_address holds. Not a
    limitation of this import; a fact about CSLB's own public record.
    """
    __tablename__ = "contractors"

    id: int | None = Field(default=None, primary_key=True)
    license_no: str = Field(index=True, unique=True)
    business_name: str = Field(default="", sa_column=Column(Text, nullable=False, default=""))
    full_business_name: str | None = None
    business_type: str | None = None  # CSLB's own field: Sole Owner, Corporation, LLC, Partnership...

    # CSLB's own "MailingAddress" -- the only address CSLB publishes. See
    # class docstring.
    business_address: str | None = None
    city: str | None = Field(default=None, index=True)
    county: str | None = Field(default=None, index=True)
    state: str | None = None
    zip_code: str | None = Field(default=None, index=True)
    business_phone: str | None = None

    issue_date: datetime | None = Field(default=None, index=True)
    expiration_date: datetime | None = Field(default=None, index=True)
    primary_status: str | None = Field(default=None, index=True)  # e.g. CLEAR, Work Comp Susp
    secondary_status: str | None = None

    # CSLB's raw "Classifications(s)" string, verbatim (e.g. "C20", "B, C38")
    # -- never split/normalized into a separate table, since a license can
    # carry classifications outside C-20/C-38/B that this import doesn't
    # otherwise track and must not silently drop from the record.
    classifications: str | None = Field(default=None, index=True)

    # Workers' compensation status -- CSLB's own WorkersComp* fields.
    workers_comp_coverage_type: str | None = None
    workers_comp_insurance_company: str | None = None
    workers_comp_policy_number: str | None = None
    workers_comp_effective_date: datetime | None = None
    workers_comp_expiration_date: datetime | None = None

    # Contractor's Bond -- CSLB's own CB* fields (the standard bond every
    # active license carries; distinct from CSLB's separate LLC/Disciplinary
    # bond fields, which this import does not currently store).
    bond_company: str | None = None
    bond_number: str | None = None
    bond_effective_date: datetime | None = None
    bond_cancellation_date: datetime | None = None
    bond_amount: float | None = None

    # Derived, not CSLB-sourced -- see class docstring. Geocoded from
    # business_address via app.geocode (US Census Bureau's free public
    # geocoder) so contractors can be joined to nearby retrofit candidates
    # by real distance. Null until geocoded; a null here is "not yet
    # geocoded," never treated as "no address."
    latitude: float | None = None
    longitude: float | None = None
    geocoded_at: datetime | None = None
    geocode_source: str | None = None

    # Precomputed by app.contractors.match_contractors -- see that module.
    # A live per-request N-buildings x M-contractors join does not scale at
    # this row count (tens of thousands on each side), so this is a cached
    # count, refreshed by re-running the match, not computed on page load.
    #
    # nearby_replacement_candidates is raw proximity count -- kept for
    # context, but NOT what /contractors sorts on: measured 2026-08-15, the
    # top 10 by count alone spanned 1,430-1,456 (under 2%), because in a
    # dense area count mostly just measures neighborhood density, not which
    # contractor is actually worth calling first. nearby_urgency_score is
    # the real ranking field -- sum of each nearby building's
    # service_life_years_past (same 0-100yr-capped gradient
    # app.pipeline.retrofit:rank_buildings ranks buildings on, not a second
    # drifting definition of "urgent"), so a contractor near a smaller
    # cluster of SEVERELY overdue buildings outranks one near a much larger
    # cluster of merely-old ones. nearby_estimated_tons (sum of each
    # nearby building's estimated tonnage band midpoint) is a tie-breaker
    # only, same discipline rank_buildings itself uses for size: it must
    # never be allowed to buy back urgency, only order contractors who tie
    # on it.
    nearby_replacement_candidates: int | None = None
    nearby_urgency_score: float | None = None
    nearby_estimated_tons: float | None = None
    nearby_radius_miles: float | None = None
    nearby_computed_at: datetime | None = None

    # Precomputed by app.contractors.match_contractors_overdue -- the
    # owner-direct replacement-lead board's own count, and deliberately a
    # SEPARATE field from nearby_replacement_candidates above rather than a
    # second meaning for it: this is OVERDUE-only (service_life_status ==
    # 'overdue', not the broader replacement_candidate population, which
    # also carries a due/approaching/not_due tail), computed at
    # contractors.default_radius_miles (15mi, "realistically reachable" --
    # see that field's own docstring) -- deliberately NOT
    # ranking_radius_miles (3mi), the radius nearby_urgency_score below
    # uses for cross-contractor ranking. Ranking and counting are different
    # questions: which contractor is worth calling first (needs a tight
    # radius to discriminate at all in dense areas) vs. how many buildings
    # could realistically be handed to them (needs the wide dispatch
    # radius, or a real-but-sparse territory looks like zero opportunity
    # when it has plenty at 5-10mi). A first version used the tight radius
    # for both and measured 53% of mechanical contractors at zero -- see
    # /assumptions' "Overdue-count radius" entry for how much of that was
    # the radius rather than a genuine absence of nearby buildings.
    nearby_overdue_count: int | None = None
    nearby_overdue_radius_miles: float | None = None
    nearby_overdue_computed_at: datetime | None = None

    source_url: str = Field(default="https://www.cslb.ca.gov/onlineservices/dataportal/")
    retrieved_at: datetime = Field(default_factory=utcnow, index=True)
    last_update: datetime | None = None  # CSLB's own "LastUpdate" field on the license record itself

    # UA Local 250's own public signatory/service-agreement contractor list
    # (socalhvacr.info/contractors) -- see app/pipeline/local250.py for the
    # fetch/match. An ATTRIBUTE of the contractor, never a ranking term: does
    # NOT feed nearby_urgency_score or any sort order on /contractors, only a
    # filter and a badge. False means "not found on Local 250's current list
    # as of the last check" -- the same real-status-as-is discipline CSLB's
    # own primary_status gets, not a claim that no relationship exists at
    # all (a contractor could be signatory to a different local, or have an
    # individual project agreement Local 250's own public list doesn't
    # cover). ua_local_250_matched_name is the exact name string from Local
    # 250's list that matched, kept for audit -- so a bad match is visible
    # and correctable, not just a silent boolean.
    ua_local_250_signatory: bool = Field(default=False, index=True)
    ua_local_250_matched_name: str | None = None
    ua_local_250_checked_at: datetime | None = None


class RetrofitGeocode(SQLModel, table=True):
    """Geocoded coordinates for a retrofit building's APN -- lives in its
    own table for the exact reason ServiceFrequencyReport does (see that
    model's docstring): build_retrofit_buildings/find_replacement_candidates
    DELETE and reinsert their whole population every run, so anything
    stored directly on RetrofitBuilding would be silently wiped and would
    have to be re-geocoded (a real cost against a rate-limited-by-courtesy
    free federal API) on every single rebuild. This table survives that;
    app.pipeline.retrofit rejoins it onto RetrofitBuilding.latitude/
    longitude at build time, the same way service_calls_per_year is
    rejoined from ServiceFrequencyReport.

    Geocoded via app.geocode (US Census Bureau's free public batch
    geocoder), from RetrofitBuilding.address -- source_address is stored
    alongside the result so a later address correction is visibly a
    different input, not a silent mismatch.
    """
    __tablename__ = "retrofit_geocodes"

    id: int | None = Field(default=None, primary_key=True)
    apn: str = Field(index=True, unique=True)
    source_address: str = Field(default="", sa_column=Column(Text, nullable=False, default=""))
    latitude: float
    longitude: float
    geocode_source: str = "us_census_bureau"
    geocoded_at: datetime = Field(default_factory=utcnow, index=True)


class RetrofitGeocodeFailure(SQLModel, table=True):
    """An apn the Census batch geocoder was given and could NOT match --
    the durable negative-result counterpart to RetrofitGeocode above, and
    for the same reason: app.pipeline.retrofit.geocode_retrofit_buildings
    treats "no row in either table" as "never attempted" and "row in
    RetrofitGeocode" as "already matched, don't re-request" -- before this
    table existed, an unmatched apn had NO row anywhere, so it looked
    identical to a never-attempted one and got resubmitted to Census on
    every single future run, forever (confirmed real, 2026-08-15: the same
    1,896 permanently-unmatchable apns retried five times in one session
    with zero new matches, for five wasted batch calls against a free
    federal service run on courtesy).

    attempted_at is updated in place (not a new row) on a repeat attempt --
    see geocode_retrofit_buildings' retry_unmatched parameter -- so this
    table also answers "when did we last actually try this address", not
    just "did we ever try it once".
    """
    __tablename__ = "retrofit_geocode_failures"

    id: int | None = Field(default=None, primary_key=True)
    apn: str = Field(index=True, unique=True)
    source_address: str = Field(default="", sa_column=Column(Text, nullable=False, default=""))
    attempted_at: datetime = Field(default_factory=utcnow, index=True)


class OwnershipRecency(SQLModel, table=True):
    """Change-of-ownership evidence for a retrofit building's APN -- lives
    in its own table for the exact reason RetrofitGeocode does (see that
    model's docstring): build_retrofit_buildings/find_replacement_candidates
    DELETE and reinsert their whole population every run, so anything
    stored directly on RetrofitBuilding would be silently wiped on the next
    rebuild. This table survives that; app.pipeline.retrofit rejoins it
    onto RetrofitBuilding.last_sale_date at build time.

    See app/pipeline/ownership.py's module docstring for the full sourcing
    story: LA County Recorder's own deed index has no bulk/API access (a
    real, checked, documented dead end -- not built), so last_sale_date is
    the LA County ASSESSOR's own public RecordingDate field instead (a
    Prop 13 reassessment-trigger date, the strongest publicly available
    proxy for a change of ownership, but NOT proof of an arms-length sale
    and carrying no document type -- both disclosed limitations, not
    silently assumed away).
    """
    __tablename__ = "ownership_recency"

    id: int | None = Field(default=None, primary_key=True)
    apn: str = Field(index=True, unique=True)
    last_sale_date: datetime = Field(index=True)
    source: str = "la_county_assessor_recording_date"
    checked_at: datetime = Field(default_factory=utcnow, index=True)


class ReviewQueue(SQLModel, table=True):
    """A PROPOSAL from an automated agent, never a final record -- the human
    reviews proposed_payload and either confirms it (which routes through
    that entity type's own real writer -- for voice_capture, that is
    app.outreach.log_outreach, the same path the dashboard's own outreach
    form and the log_outreach MCP tool both use, so there is exactly one
    place an Outreach row is ever created) or rejects it (discarded, never
    written anywhere).

    Deliberately generic, not voice-capture-specific -- `agent` and
    `entity_type` are how a future agent (a different capture channel, a
    different entity kind) shares this same table and this same review UI
    instead of building its own queue and its own review page. See
    app/pipeline/voice_capture.py for the first (and so far only) writer.

    provenance carries where this came from and what it's based on -- for
    voice_capture: {source: "ios_shortcut", retrieved_at, audio_id,
    transcript} -- so a reviewer (or an audit six months from now) can see
    the original evidence next to the proposal, not just trust the
    extraction. trace_id correlates every row this session and its errors,
    even across future agents that log more than one row per run.
    """
    __tablename__ = "review_queue"

    id: int | None = Field(default=None, primary_key=True)
    agent: str = Field(index=True)
    trace_id: str = Field(index=True)
    entity_type: str = Field(index=True)
    proposed_payload: dict = Field(sa_column=Column(JSON, nullable=False))
    provenance: dict = Field(sa_column=Column(JSON, nullable=False))
    confidence: float | None = None
    status: str = Field(default="pending", index=True)  # pending | approved | rejected
    created_at: datetime = Field(default_factory=utcnow, index=True)
    decided_at: datetime | None = None


class CaptureAudio(SQLModel, table=True):
    """Raw bytes for one voice-capture upload, so the review card can play
    the original recording back next to the transcript and the proposed
    fields -- the transcript is not trusted as the only record of what was
    actually said. One row per ReviewQueue row (see
    ReviewQueue.provenance['audio_id']), not a shared media library.

    Stored in Postgres, not on Render's local disk: the web/cron
    containers' disks are ephemeral across deploys (confirmed by this
    project's own render.yaml -- no persistent volume is declared), and a
    voice note surviving to review time is the entire point. Fine at the
    volume one rep's sales calls produce; if capture volume or audio length
    grows enough to strain the basic-256mb Postgres plan, move this to
    object storage -- not done here on purpose, since nothing about this
    schema needs to change to do that later (this table becomes a pointer
    table instead of a blob table).
    """
    __tablename__ = "capture_audio"

    id: int | None = Field(default=None, primary_key=True)
    content_type: str = Field(default="audio/m4a")
    data: bytes = Field(sa_column=Column(LargeBinary, nullable=False))
    size_bytes: int = 0
    created_at: datetime = Field(default_factory=utcnow, index=True)


class ProjectDocument(SQLModel, table=True):
    """A PDF a rep attaches to a project -- a drawing set, a Division 23 spec
    section, or just the mechanical sheets, handed over by an engineer or a
    GC. See app/pipeline/schedule.py for how this becomes ScheduleEntry rows.

    Stored in Postgres, not on Render's local disk -- same reasoning as
    CaptureAudio above (the web/cron containers' disks are ephemeral across
    deploys). A drawing set runs larger than a voice note; MAX_UPLOAD_BYTES
    in app/pipeline/schedule.py caps it well under what the basic-256mb
    Postgres plan can hold at the volume one rep's project files produce. If
    that volume grows enough to strain it, move to object storage -- not
    done here on purpose, same as CaptureAudio's own note on this, since
    nothing about this schema needs to change to do that later.

    raw_text is extracted once at upload time (pdfplumber, cheap, no LLM) and
    kept so schedule extraction can be re-run against it without re-reading
    the PDF bytes -- and so a document with no text layer (a scanned, un-OCR'd
    drawing set) is visibly distinguishable at a glance from one Scout simply
    hasn't gotten to yet.
    """
    __tablename__ = "project_documents"

    id: int | None = Field(default=None, primary_key=True)
    project_id: int = Field(foreign_key="projects.id", index=True)
    filename: str
    content_type: str = Field(default="application/pdf")
    data: bytes = Field(sa_column=Column(LargeBinary, nullable=False))
    size_bytes: int = 0
    # drawing_set | spec_section | mechanical_sheets | other -- the rep's own
    # label at upload time, purely descriptive (nothing downstream branches
    # on it); free text rather than an enum so a document that's genuinely
    # both isn't forced into one bucket.
    doc_type: str = Field(default="other", index=True)
    page_count: int | None = None
    raw_text: str = Field(default="", sa_column=Column(Text, nullable=False, default=""))
    uploaded_at: datetime = Field(default_factory=utcnow, index=True)
    uploaded_by: str | None = None
    extracted_at: datetime | None = None
    # Set when pdf_to_text/schedule extraction itself failed (a corrupt file,
    # an LLM error) -- kept distinct from "extracted_at is set but 0 rows
    # found", which is a legitimate outcome for a document with no equipment
    # schedule in it at all (e.g. an architectural sheet set).
    extraction_error: str | None = None


class ScheduleEntry(SQLModel, table=True):
    """One equipment-schedule line item extracted from a ProjectDocument --
    one tag, its capacity/airflow, and which manufacturer(s) the document
    itself names for it. See app/pipeline/schedule.py.

    Same grounding discipline as Signal (app/grounding.py): every field here
    is checked against the source document's own text before being trusted,
    and source_quote is not optional -- a row with no verbatim excerpt
    backing it has nothing to ground it against and is unconditionally
    needs_review. A rejected/ungrounded field is set to null and the reason
    is recorded in extraction_json, exactly like Signal's rejected_numeric/
    rejected_names -- never silently dropped, never silently kept as if
    trusted.

    needs_review is the union of every grounding failure PLUS a low raw
    confidence score -- a human decides what "low" means for their own risk
    tolerance by reading review_reason, not by re-deriving it from
    extraction_json. Re-extraction (re-running against the same document)
    deletes and replaces every row for that project_document_id, same
    replace-not-append idiom as Signal's one-per-raw_document.
    """
    __tablename__ = "schedule_entries"

    id: int | None = Field(default=None, primary_key=True)
    project_document_id: int = Field(foreign_key="project_documents.id", index=True)
    # Denormalized from project_documents.project_id so a project's full
    # schedule (across every document attached to it) is one indexed query,
    # not a join through project_documents for every read.
    project_id: int = Field(foreign_key="projects.id", index=True)
    tag: str = Field(index=True)
    equipment_type: str | None = None
    # One of app.accounts.ROLE_ORDER's 13 values, or null -- the SAME
    # building-role taxonomy DMG's own line card (ProductLine.building_role)
    # is grouped by, so a tag maps onto "which of our lines could serve
    # this" directly (app/schedule_mapping.py) rather than through a second,
    # hand-maintained equipment_type -> role table that could drift from the
    # line card's own. A classification judgment, like triage's category --
    # not something grounding checks against the document text.
    role: str | None = Field(default=None, index=True)
    capacity_value: float | None = None
    capacity_unit: str | None = None
    # Only set when the document restates capacity_value's SAME headline
    # number a second time in BTU/H for this tag (see schemas.py's
    # capacity_btuh docstring) -- never a conversion Scout computed itself.
    capacity_btuh: float | None = None
    # True/False when both capacity_value (tons) and capacity_btuh are
    # grounded and checked against each other; null when there was nothing
    # to cross-check. Agreement is corroborating evidence, not a conflict --
    # see app/grounding.py's ground_schedule_entry.
    capacity_corroborated: bool | None = None
    airflow_cfm: float | None = None
    basis_of_design_manufacturer: str | None = None
    approved_equals: list = Field(default_factory=list, sa_column=Column(JSON, nullable=False, default=list))
    source_quote: str = Field(default="", sa_column=Column(Text, nullable=False, default=""))
    source_page: int | None = None
    confidence: float = 0.0
    needs_review: bool = Field(default=False, index=True)
    review_reason: str | None = None
    extraction_json: dict = Field(default_factory=dict, sa_column=Column(JSON, nullable=False, default=dict))
    created_at: datetime = Field(default_factory=utcnow)


class Ab869Plan(SQLModel, table=True):
    """One row per in-territory facility's AB 869 seismic compliance plan
    filing -- facility grain, matching what a compliance plan actually IS
    (one filing per facility, with per-building detail nested inside it;
    see Ab869Building/Ab869Milestone for that nested detail, deliberately
    NOT flattened onto this row). See app/pipeline/ab869.py's module
    docstring for the full access/parsing investigation this is built on.

    plan_status/plan_status_source: plan_status is sourced from the
    crosstab CSV's own "Application Status" column when present (158 of
    201 in-territory facilities carry one); for the 43 that don't,
    plan_status_source is "pdf_header" and the value comes from the PDF's
    own "Status: {text}" line instead -- both are real, disclosed sources,
    never inferred. plan_status_paragraph is PDF-only (nothing else in
    Scout has it) and is frequently null: the paragraph following the
    status line is genuinely garbled for many facilities (see
    app.pipeline.ab869's module docstring on the PDF's own text-layer
    defect) -- plan_status_paragraph_reason records why when null.

    delay_text/delay_requested: delay_text is the PDF's own "Facility's
    Request for Delay" section, PDF-only, also frequently null for the
    same reason. delay_requested is derived ONLY when delay_text itself
    was successfully extracted: True unless the text is HCAI's own fixed
    "HCAI has not received an application for delay from this facility"
    phrasing, which is a literal template match, not an inference -- null
    when delay_text itself is null, never guessed from anything else.

    owner_name/owner_type/manager_name/manager_type/
    financially_responsible_party: sourced from the CROSSTAB, not the PDF
    -- three of the crosstab's own column headers are the literal question
    text of three of these six PDF "ownership lines" ("Who manages the
    hospital? ", "What type of entity manages this hospital? ", "Who is
    financially responsible for the seismic upgrades?"), which is
    independent confirmation this is the right source, not a guess. The
    PDF's own rendering of these same six lines is confirmed garbled the
    same way as the table headers (see app.pipeline.ab869). other_
    financial_contact (the sixth line, "Other contact financially
    obligated for infrastructure improvements") has no crosstab equivalent
    and is always null with other_financial_contact_reason set -- never
    guessed from an adjacent crosstab column.

    ab869_letter_url: read via pdfplumber's own hyperlink annotations
    (unaffected by the text-layer defect) -- confirmed 0 of 199 real
    in-territory PDFs carry one at all, so this column is schema-ready but
    has never actually been populated against real data as of this
    migration; kept rather than dropped in case a facility with a real
    delay letter link appears in a future re-import.

    source_pdf_hash: sha256 of the exact PDF bytes this row was parsed
    from -- `scout import-ab869`'s own idempotency key alongside perm_id,
    so re-running the import after Andrew re-pulls a facility's PDF (a new
    hash) creates a fresh import rather than silently keeping stale data,
    while re-running against byte-identical files already on disk is a
    true no-op.
    """
    __tablename__ = "ab869_plans"
    __table_args__ = (UniqueConstraint("perm_id", name="uq_ab869_plan_perm_id"),)

    id: int | None = Field(default=None, primary_key=True)
    perm_id: str = Field(index=True)
    county: str | None = Field(default=None, index=True)  # denormalized from HospitalBuilding at import time

    plan_status: str | None = Field(default=None, index=True)
    plan_status_source: str | None = None  # "crosstab" | "pdf_header"
    plan_status_paragraph: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    plan_status_paragraph_reason: str | None = None

    delay_text: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    delay_text_reason: str | None = None
    delay_requested: bool | None = Field(default=None, index=True)
    ab869_letter_url: str | None = None

    owner_name: str | None = None
    owner_type: str | None = None
    manager_name: str | None = None
    manager_type: str | None = None
    financially_responsible_party: str | None = Field(default=None, index=True)
    other_financial_contact: str | None = None
    other_financial_contact_reason: str | None = None

    source_pdf_path: str
    source_pdf_hash: str = Field(index=True)
    crosstab_path: str | None = None
    imported_at: datetime = Field(default_factory=utcnow, index=True)


class Ab869Building(SQLModel, table=True):
    """One row per building named in a facility's Compliance Method table
    -- building grain. Deliberately does NOT duplicate building_name,
    spc_rating, or npc_rating from HospitalBuilding: those three columns
    are the single most severely garbled region of the PDF (a long-
    wrapping name column immediately adjacent to two narrow rating
    columns -- see app.pipeline.ab869's module docstring), and
    HospitalBuilding already carries them authoritatively for the same
    (perm_id, building_nbr) pair at a confirmed 99.5% match rate against
    this exact crosstab's own Building No. values. Join to
    HospitalBuilding at query time for those three fields rather than
    trusting a second, less reliable copy here.

    compliance_type is sourced from the crosstab CSV (clean, already
    validated), not the PDF. narrative/hcai_comment are PDF-only -- see
    app.pipeline.ab869 for the column-position extraction and garbling
    check; either can be null with its own _reason set when that specific
    cell's text could not be reliably reconstructed.
    """
    __tablename__ = "ab869_buildings"
    __table_args__ = (UniqueConstraint("perm_id", "building_nbr", name="uq_ab869_building"),)

    id: int | None = Field(default=None, primary_key=True)
    perm_id: str = Field(index=True)
    building_nbr: str = Field(index=True)  # HCAI's "BLD-xxxxx", same format as HospitalBuilding.building_nbr

    compliance_type: str | None = Field(default=None, index=True)
    narrative: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    narrative_reason: str | None = None
    hcai_comment: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    hcai_comment_reason: str | None = None
    # From the crosstab's own "Progress" column -- ANY of a building's
    # repeated crosstab rows carrying "Missed Milestone(s)" sets this. A
    # building-level flag, not tied to any one Ab869Milestone row, because
    # the crosstab's progress-bucket rows and the PDF's own per-milestone
    # rows are two different views of the same underlying schedule, not a
    # 1:1 join Scout can make reliably.
    has_missed_milestone: bool = Field(default=False, index=True)

    imported_at: datetime = Field(default_factory=utcnow, index=True)


class Ab869Milestone(SQLModel, table=True):
    """One row per milestone in a building's Milestone Description Table
    -- milestone grain (many per building, PIN 80 allows up to ten).
    PDF-only; nothing else in Scout has this.

    completion_date_text is the verbatim string the hospital reported
    (only ever accepted here after matching an MM/DD/YYYY-shaped pattern --
    see app.pipeline.ab869's _DATE_RE -- so it is a validated date string,
    not arbitrary text); completion_date is that SAME value parsed to a
    real date, kept separate rather than replacing the raw string, same
    "raw verbatim + derived" split this codebase already uses for
    HospitalBuilding.spc_rating/spc_deadline_year. completion_date is what
    the join's own date-sorted milestone report (Phase C.6.c) filters and
    sorts on; completion_date_text is what a human reads to confirm it.
    """
    __tablename__ = "ab869_milestones"

    id: int | None = Field(default=None, primary_key=True)
    perm_id: str = Field(index=True)
    building_nbr: str = Field(index=True)

    milestone_type: str | None = Field(default=None, index=True)
    milestone_type_reason: str | None = None
    description: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    description_reason: str | None = None
    completion_date_text: str | None = None
    completion_date: datetime | None = Field(default=None, index=True)
    completion_date_reason: str | None = None
    hcai_comment: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    hcai_comment_reason: str | None = None
    met_by_hcai: str | None = None
    met_by_hcai_reason: str | None = None

    imported_at: datetime = Field(default_factory=utcnow, index=True)


class LinePitch(SQLModel, table=True):
    """One row per ProductLine (never per branch -- a pitch is about the
    manufacturer's product, not which DMG location happens to stock it):
    the elevator-pitch training material a new rep uses to talk about a
    line. See app/pipeline/line_pitch.py's module docstring for the full
    generation method.

    SAME propose-never-assert discipline as CaptureAudio/voice capture:
    every row generation ever writes starts review_status='draft' and is
    shown with an UNVERIFIED marker until a human confirms it. A confirmed
    row is NEVER touched by a later regeneration pass -- see
    app.pipeline.line_pitch.generate_line_pitches, which skips any line
    whose existing row is already 'confirmed', so a rep's review is
    permanent until they explicitly re-open it.

    differentiators/engineer_questions are JSON lists, target length 1 as
    of the 2026-09-03 register rewrite (was 3) -- can be shorter (empty)
    if the one candidate failed grounding, never padded with an invented
    one.

    pitch_scope is 'full' (a real manufacturer page was fetched, the LLM
    wrote a complete pitch, every claim fragment-grounded against that
    page) or 'line_row_only' (NO page was fetched, for any reason -- no
    LLM call was made at all, and what_it_is/where_it_fits are a
    deterministic template built ONLY from this line's own building_role
    and confirmed branch coverage, with zero capability or product-
    category claims of any kind). elevator_pitch/typical_project_types/
    differentiators/engineer_questions and every LineCompetitor row are
    always empty for a line_row_only pitch -- there is nothing to
    positively claim without a source to check it against. The UI shows
    'insufficient source' for these rather than any capability-sounding
    text -- see app/pipeline/line_pitch.py's module docstring for why this
    replaced the earlier behavior (a 'fetch_failed' line still got a full
    LLM-written pitch that then had every claim dropped by grounding,
    leaving a vague, unlabeled fragment -- confirmed real, the Aldes row
    in this feature's own first production run).

    source_url/source_fetch_status record WHERE (if anywhere) the
    manufacturer's own product page came from for this generation pass --
    'fetched' (real page text was used for grounding), 'robots_disallowed'
    (a candidate domain was found but robots.txt refused it -- checked live,
    not assumed), 'no_domain_found' (nothing in this line's own researched
    basis fields named a plausible manufacturer domain -- no web search was
    performed to find one), or 'fetch_failed' (network/HTTP error). Any
    status other than 'fetched' implies pitch_scope='line_row_only'.
    """
    __tablename__ = "line_pitches"
    __table_args__ = (UniqueConstraint("product_line_id", name="uq_line_pitch_product_line"),)

    id: int | None = Field(default=None, primary_key=True)
    product_line_id: int = Field(foreign_key="product_lines.id", index=True)

    what_it_is: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    where_it_fits: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    typical_project_types: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    elevator_pitch: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    differentiators: list = Field(default_factory=list, sa_column=Column(JSON, nullable=False, default=list))
    engineer_questions: list = Field(default_factory=list, sa_column=Column(JSON, nullable=False, default=list))

    review_status: str = Field(default="draft", index=True)  # draft | confirmed | rejected
    reviewed_by: str | None = None
    reviewed_at: datetime | None = None

    source_url: str | None = None
    source_fetch_status: str | None = None  # fetched | robots_disallowed | no_domain_found | fetch_failed
    pitch_scope: str = Field(default="full", index=True)  # full | line_row_only
    grounded_claim_count: int = 0
    dropped_claim_count: int = 0

    model: str | None = None
    generated_at: datetime | None = None
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class LineCompetitor(SQLModel, table=True):
    """One row per (product_line_id, competitor_name) -- why a rep loses or
    wins against that specific competitor for that specific DMG line.
    competitor_name is constrained at generation time to manufacturers
    already present in app.competitors.COMPETITOR_LINES/CompetitorLine for
    the SAME building_role as this line -- never invented from general
    knowledge, see app.pipeline.line_pitch's module docstring. evidence_url
    is NOT LLM-generated: it is copied directly from the matching
    CompetitorLine.source_url, the real, already-verified citation for that
    competitor actually selling into that role -- the one fact in this row
    that doesn't need a separate grounding pass, because it was sourced by
    app/competitors.py's own research before this table ever existed.

    Same review_status discipline as LinePitch: every row starts 'draft'
    and a confirmed or rejected row is never silently regenerated.
    """
    __tablename__ = "line_competitors"
    __table_args__ = (UniqueConstraint("product_line_id", "competitor_name",
                                       name="uq_line_competitor"),)

    id: int | None = Field(default=None, primary_key=True)
    product_line_id: int = Field(foreign_key="product_lines.id", index=True)
    competitor_name: str = Field(index=True)

    why_we_lose: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    why_we_win: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    evidence_url: str | None = None

    review_status: str = Field(default="draft", index=True)  # draft | confirmed | rejected
    reviewed_by: str | None = None
    reviewed_at: datetime | None = None

    model: str | None = None
    generated_at: datetime | None = None
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class AhjA2lGuidance(SQLModel, table=True):
    """One row per plan-check authority (AHJ) in Scout's territory --
    whether it has published written guidance on A2L refrigerant systems
    (R-32, R-454B) and what it says. See app.pipeline.ahj_a2l's module
    docstring for the Phase A research this table loads verbatim
    (app/assumptions.py's "AHJ A2L register" group) and app/assumptions.py
    for the column-set citation.

    jurisdiction is the primary key -- a plain display name ("Los
    Angeles", "Los Angeles County", "HCAI/OSHPD", "State of California"),
    not a foreign key into any other table. Nothing else in this app names
    an AHJ as a first-class entity, so there is nothing to key against;
    app.geo._CITY_TO_COUNTY's own flat, collision-free city namespace is
    what makes a plain string PK safe here (no two in-territory cities
    share a name in this app's own data).

    status is one of four values, plain string (deliberately NOT a
    native Postgres enum -- see the "Fix: add school_facility_funding to
    Postgres's signaltype enum" incident this codebase already lived
    through; a small closed set of values on a standalone reference table
    doesn't need that migration risk):
      HIT         -- a real, on-point document was found and read; every
                     column below is populated from it, NULL where the
                     document itself doesn't state a field.
      NONE_FOUND  -- one targeted search was run and returned no
                     plausible on-point document. checked_at is set; every
                     document column is NULL.
      BLOCKED     -- the AHJ's own host is robots.txt-disallowed (named
                     AI-crawler block or an infrastructure-level "Access
                     Denied"); never fetched, per this app's own "never
                     alter the user-agent to evade a block" rule.
                     checked_at is set (the block itself was confirmed),
                     every document column is NULL.
      NOT_REACHED -- never searched at all this pass, for a REASON that
                     has nothing to do with the jurisdiction itself (here:
                     a session-wide WebSearch tool quota ran out, and one
                     research batch never started due to a subagent
                     fault) -- checked_at is NULL, which is what makes
                     these rows visibly different from NONE_FOUND rather
                     than silently indistinguishable from "checked, found
                     nothing."

    Every document column (ashrae_15_edition through express_permit_note)
    is NULL when the source document doesn't state that fact -- never
    inferred, never defaulted to a guess, even when every other row's
    pattern makes a guess tempting (see app/assumptions.py's own "Null
    over inference" framing, CHARTER.md invariant 12)."""
    __tablename__ = "ahj_a2l_guidance"

    jurisdiction: str = Field(primary_key=True)
    jurisdiction_type: str = Field(index=True)  # county | city | state_agency
    county: str | None = Field(default=None, index=True)
    status: str = Field(index=True)  # HIT | NONE_FOUND | BLOCKED | NOT_REACHED

    ashrae_15_edition: str | None = None
    ashrae_15_2_edition: str | None = None
    ashrae_34_edition: str | None = None
    addendum_a_shaft_alt: str | None = None  # YES | NO | INFORMATIONAL | NULL
    addenda_accepted: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    edvc_regardless_of_charge: str | None = None  # YES | NO | NULL
    a1_resubmittal_rule: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    express_permit_note: str | None = Field(default=None, sa_column=Column(Text, nullable=True))

    doc_title: str | None = None
    doc_number: str | None = None
    doc_date: datetime | None = None
    source_url: str | None = None

    checked_at: datetime | None = Field(default=None, index=True)
    notes: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
