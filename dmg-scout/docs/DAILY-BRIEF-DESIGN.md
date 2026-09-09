# Daily brief — Phase A design

Proposal only. Nothing in this document is built. Written after reading
`docs/CHARTER.md` and `RUNBOOK.md` (no `STATUS.md` exists in this repo).
Three things are designed: a generalized nightly diff, an opportunity
lifecycle table, and a daily brief + weekly xlsx built from both. Each
section proposes options rather than asserting one, per Charter §5 ("stop
and ask" covers scope not named in a plan — this expands scope past the
existing `today_brief`, so it should be agreed, not just built).

**Scope guardrail up front:** Charter §7 rules out "a CRM (RepFabric and ROM
exist)" permanently. The opportunity table below is a thin stage + next-
action layer that feeds the brief, not a CRM rebuild — no pipeline
forecasting, no activity timeline UI, no multi-user permissions. If it grows
past "what stage, who owns it, what's next, when" it has crossed the line
Charter §7 draws. Flagged again in §2 where the risk is concretest.

---

## 1. Nightly run + diff

### 1a. What's schedulable today vs. manual, and why

Everything already running inside `scout pipeline` (Render cron
`dmg-scout-pipeline`, daily 13:00 UTC / 6am PT, `standard` plan):

| Source | Cadence in `scout pipeline` | Why |
|---|---|---|
| CEQAnet, EDGAR, CAEATFA, Legistar, CivicPlus, RSS, ATS (Greenhouse/Lever/Ashby) | Daily | Pipeline A — live, LLM-free fetch adapters, this is what the cron exists for |
| `la_ebewe_benchmarking`, `ua_local_250`, `la_county_ownership`, `match_contractors`, `match_contractors_overdue` | Weekly, Sundays only (`RETROFIT_WEEKLY_WEEKDAY`) | Confirmed against real `SourceRun` history — these sources' own publish rhythm is weekly at best, a daily re-fetch would just re-read the same file |

Manual today — a person runs a CLI command or a one-off Playwright script,
by hand, on the source's own irregular publish rhythm:

| Source | Real cadence (measured/stated) | Why manual |
|---|---|---|
| `hcai_seismic_ratings` | ~90 days (CHHS updates irregularly) | CHHS ToU ambiguous on automated access — never resolved, still manual pending that check ([[project_dmg_scout_hcai_hospitals]]) |
| `ab869_compliance_plans` | ~90 days (no real cadence to measure) | Tableau roster needs a rendered browser session — Playwright, not a fetcher |
| `hcai_projects` (HCAI Facilities Development Division) | Report generated on demand, hand-pulled | Same SSRS ReportViewer constraint as above; **has no `config.yaml` entry today** — see 1c |
| `scaqmd_facility` — CARB portion | Annual | CARB's Facility Search Tool export, one-off Playwright pull; the AER portion of this same table *is* a live fetcher (`fetch_scaqmd_facilities`) but isn't in the cron rotation either |
| `bpelsg_mechanical_roster` | Monthly | Box share-link file, hand-downloaded |
| `ab802_benchmarking` | Annual | `fetch_ab802_benchmarks` is a real automated fetcher — just never added to the cron's yearly rotation, unlike its weekly siblings |
| `opsc_school_facility` | Monthly (CKAN `accrualPeriodicity R/P1M`) | Bulk-CSV-only source; never added to the cron either — no technical blocker, purely "not yet wired in" |
| `competitor_lines` | No fixed rhythm | Hand-researched, not fetched at all |

Worth naming plainly: several of these (`ab802_benchmarking`, `opsc_school_facility`,
CARB) have no *technical* reason to stay manual — the fetcher already exists
and is LLM-free. They're manual because nobody has scheduled them, not
because the source resists automation. That's a separate, smaller proposal
from what follows (add three lines to `scout pipeline`'s yearly/monthly
rotation) and is out of scope for this brief design — noted here so it
isn't silently assumed away.

### 1b. Snapshot/diff: what counts as new or changed

The codebase already has exactly this mechanism, for one table only.
`DigestLog` (`kind`, `ref_id: int`, `fingerprint: str`) plus
`notify.py::_detect_changes` mark an item "already reported" once a
`(kind, ref_id, fingerprint)` triple has been seen; a project is "new" the
first time its id clears the score floor, "changed" when its stage's
fingerprint hasn't been seen for that id before. That's the right shape —
the problem is `ref_id` is typed `int`, an FK to `projects.id`. None of the
six target tables have an integer natural key:

| Table | Natural key | Type |
|---|---|---|
| `hcai_projects` | `record_no` | str |
| `ab869_plans` | `perm_id` | str |
| `ab802_buildings` | `(portfolio_manager_property_id, year_ending)` | str, str |
| `opsc_projects` | `application_number` | str |
| `scaqmd_facilities` | `(facility_id, source)` | str, str |
| `projects` / `signals` | `id` | int — already covered by `DigestLog` |

**Proposed fix:** don't overload `DigestLog` (its `kind` vocabulary —
`new_project` / `stage_change` / `contactable` — is already Project-specific
semantics, not a generic diff log). Add a new table instead:

```
SourceRowSeen(source: str, natural_key: str, fingerprint: str, first_seen_at, last_seen_at)
UniqueConstraint(source, natural_key)
```

`natural_key` is always a string — compound keys join with a fixed
separator (`f"{portfolio_manager_property_id}:{year_ending}"`, etc.). A row
is:
- **new** the first time `(source, natural_key)` has no `SourceRowSeen` row.
- **changed** when `fingerprint` differs from the stored one — `fingerprint`
  is *not* every column (that would fire on `imported_at` bumping every
  reload), it's a deliberately narrow, per-table subset of fields judged
  alert-worthy:

| Table | Fingerprint fields (propose, don't build) |
|---|---|
| `hcai_projects` | `stage`, `is_mechanical` |
| `ab869_plans` | `plan_status`, `delay_requested`, `missed_milestone_count`, `next_upcoming_date` |
| `ab802_buildings` | `air_permit_facility_id` (the only field that changes post-load via the AB869/802 join; the annual filing itself is a new row per year, not a "change" to an existing one) |
| `opsc_projects` | `status` |
| `scaqmd_facilities` | not fingerprinted for *change* — a facility's registration doesn't meaningfully change; only "new facility id" matters here |
| `projects` / `signals` | unchanged — keep `DigestLog`/`_detect_changes` exactly as-is, this whole section is additive |

One correctness note specific to `opsc_projects` and the AER half of
`scaqmd_facilities`: both are **full-replace** tables (delete all rows,
reinsert from the fresh file). That's fine for this diff *only if* the
diff keys off the natural key (`application_number`, `facility_id`), never
the DB `id` column — `id` is reassigned on every reload, the natural key is
stable across reloads. Worth stating explicitly since it's an easy trap.

### 1c. Where it runs, what it costs

**Proposed: a new stage appended to the existing `scout pipeline` cron run**,
not a second cron service. Reasoning:
- The diff is pure SQL (`SELECT` the current table, compare fingerprints,
  `UPSERT SourceRowSeen`) — no fetch, no LLM, sub-second to low-seconds even
  against the largest table here (`hcai_projects` at 45,132 rows).
- A second Render cron service is a recurring line-item cost plus a second
  dead man's switch to babysit (healthchecks.io ping, `scout doctor` check) —
  not worth it for work this cheap.
- It must run *after* whichever stage last touches each table, so a manual
  re-import earlier that day (someone runs `scout load-hcai-projects` at
  2pm) is picked up by the next morning's 6am run — the diff stage doesn't
  care whether the data changed because of the cron or because of a human
  running a CLI command earlier; it just compares current-DB-state against
  last-seen.

**Cost:** $0 marginal — runs inside the cron container Render already bills
for (`standard`, 2Gi, already sized for the fetch/extract stages, which are
far more memory-hungry than a handful of `SELECT`s). No LLM calls: this is
Pipeline B territory, and Charter §3's "must be deterministic SQL, do not
use the LLM where a join will do" applies directly — a diff is a comparison,
not a judgment call.

---

## 2. Opportunity lifecycle table

### 2a. Why not reuse `Project.status`

`Project.status` already has a coarse lifecycle
(`active`/`contacted`/`specified`/`bidding`/`won`/`lost`/`dead`/`archived`,
via `ACTIVE_STATUSES`/`OUTCOME_STATUSES`) but it lives only on `Project`
rows — Pipeline A's fetch→extract→resolve output. Most of what this brief
needs to track a stage on (an AB 869 hospital, an OPSC school, an HCAI
project, a BPELSG-verified engineer's project, an SCAQMD-permitted
facility) has **no** `Project` row at all; they're Pipeline B facility/permit
tables that never go through resolve. A stage-tracking table has to point
at either kind.

### 2b. Proposed model

```
Opportunity
  id: int, pk
  stage: enum (new, reviewed, contacted, engineer_known, contractor_known,
               quoted, won, lost, dead)
  assigned_rep: str | None        # NOT "owner" — see naming note below
  next_action: str | None
  next_action_date: datetime | None
  project_id: int | None, FK projects.id
  facility_type: str | None       # "hospital_building" | "ab869_plan" | "ab802_building"
                                   # | "hcai_project" | "opsc_project" | "scaqmd_facility"
  facility_id: str | None         # that table's natural key, as a string
  created_at, updated_at
  UniqueConstraint(project_id) where project_id is not null
  UniqueConstraint(facility_type, facility_id) where facility_type is not null
```

**Naming note:** the user's ask used "owner" for who's tracking the
opportunity. `owner` already means something else everywhere in this schema
(`Project.developer`, `Ab802Building`'s deliberate absence of an owner
column, `AB869Plan.owner_name`/`owner_type` — all real-world building
owners). Reusing "owner" for "which rep is on this" would collide with an
established meaning. Proposed `assigned_rep` instead; flagging for
confirmation since it's a rename off the literal ask, not a technical
constraint.

**Exactly one of `project_id` / `(facility_type, facility_id)` set:**
same pattern `Outreach` already uses for `project_id`/`account_id` — enforced
in one writer function (`app.opportunities.create_opportunity`), not a DB
constraint, matching this codebase's existing discipline (`apply_manual_correction`,
`confirm_field_intel` are the other precedents: mutation goes through one
named function, never ad hoc).

**Stage transitions are human-only, always.** Created at `stage=new`
automatically the first time a rep acts on a row (see 2d); every later
transition is a UI click, never pipeline-inferred. The one thing a nightly
job may do is *suggest* — e.g. "this HCAI project's stage flipped to
`in_construction`, consider marking the linked Opportunity `reviewed`" as a
brief line, never an automatic write. This mirrors the existing rule that
`apply_manual_correction` only ever comes through the UI, never called by
code — same principle, applied to stage.

### 2c. How `Outreach` / `FieldIntel` attach

Rather than a new FK on `Outreach`/`FieldIntel` (dual-write risk, and a
gap whenever someone logs outreach without remembering to also tag the
opportunity), propose **deriving** the link: given an `Outreach` row's
`project_id` or `account_id`, or a `FieldIntel` row's resolved firm/account
ids, look up the matching `Opportunity` via the same unique constraints
above. This only works cleanly if the 1:1 assumption holds (one open
Opportunity per project/facility, enforced by the unique constraints in
2b) — which is also why those constraints matter, not just for tidiness.
Flag as the leaner of two options; the alternative (an explicit
`opportunity_id` on both tables) is more explicit but is real schema churn
on two existing, working tables for a benefit (skipping a lookup) that
doesn't show up anywhere performance-sensitive.

### 2d. Watch list → pipeline view

**This is a real naming collision, not just phrasing.** `/watchlist` today
means one specific thing: out-of-territory projects
(`Project.in_territory == False`), deliberately excluded from the main
board and shown separately so they don't crowd in-territory rows. That's
unrelated to "opportunities I'm tracking through a sales stage." Two
options:

- **(a) New route, `/pipeline`, separate from `/watchlist`.** Lists every
  open `Opportunity` (non-terminal stage), filterable by stage/rep/due
  date, each row linking back to its `Project` or facility page. Leaves
  `/watchlist` exactly as it is today. Recommended — avoids quietly
  changing what reps already rely on `/watchlist` meaning, and keeps this
  addition legible as "a stage tracker," not a rename of an existing
  feature into something bigger.
- **(b) Repurpose `/watchlist` itself** into the stage-tracked view, since
  "things I'm watching" is a reasonable umbrella for both. Not recommended:
  changes the meaning of an existing, working page out from under anyone
  used to it, and conflates "out-of-territory, checked deliberately" with
  "in my sales pipeline," which aren't the same axis (an out-of-territory
  project could be `dead` or `engineer_known` just as easily as an
  in-territory one).

Proposing (a). Flagging (b) because the user's phrasing ("How Watch list
becomes the pipeline view") suggests they may have (b) in mind — worth
confirming before building either way.

---

## 3. Daily brief + weekly xlsx

### 3a. Brief contents, priority order, one page

`notify.py`'s existing `today_brief()` already has a four-section shape
(three calls today → what changed → overdue/due next actions → one thing
worth knowing) built specifically to fit a phone screen, plain text first,
with an optional cheap-model prose pass that was tried once and turned back
off. Proposed brief keeps that shape and folds the new sources in rather
than inventing a second, parallel brief:

1. **Overdue next actions** — generalized from `Outreach`'s
   `next_action`/`next_action_date` to also read `Opportunity.next_action*`,
   oldest first. This is the accountability section; it leads because a
   missed follow-up is the most actionable thing on the page.
2. **Three calls today** — unchanged, `Project`-only, exactly as it works
   now (score × reachability × window proximity).
3. **New/changed since yesterday** — the diff from §1, one line per source,
   capped (propose 5 lines/source, "+N more, see dashboard" past that) so
   a busy source (45K-row `hcai_projects`) can't crowd out everything else
   on a bad day.
4. **Cross-source highlights worth a human's attention** — e.g. "3
   facilities now have an open mechanical HCAI project," "2 AB 802
   buildings newly matched to an air permit" — a small, hand-picked set of
   the kind of fact that's easy to miss buried in §3's raw diff lines.
   Judgment call on which facts qualify; propose starting with just the two
   above (mechanical HCAI project opened, air-permit match newly found)
   since both already have working queries (`open_hcai_projects_by_facility_id`,
   `air_permit_facility_id`) and both are things a rep would actually want
   to hear about.
5. **One thing worth knowing** — unchanged (LLM budget, stale source, quiet
   county).

**Delivery: Resend, same as today** — `RESEND_API_KEY` (already set on both
Render services), sandbox sender `onboarding@resend.dev`, single recipient
`acrane988@gmail.com` (`digest.to` in `config.yaml`). **Flagging explicitly,
per the ask: this is a DMG-facing artifact riding entirely on Andrew's own
infrastructure** — his Resend account, his API key, his inbox. Nothing
about that changes with this proposal; noted because the brief is about to
carry meaningfully more (six more sources' worth of data) through the same
pipe.

### 3b. Weekly xlsx

**No new dependency** — `openpyxl` is already a hard dependency (used to
*read* AB802/SCAQMD/IEPR source files); writing one uses the same library.
Proposed home: `app/deliverables.py`, alongside the existing
`write_call_list`/`write_briefs`/`write_baseline` (same "generate to
`output/`" pattern).

Tabs, as asked:

| Tab | Source |
|---|---|
| Pipeline | Every open `Opportunity` row — stage, `assigned_rep`, `next_action(_date)`, linked project/facility |
| New this week | `SourceRowSeen` rows with `first_seen_at` in the last 7 days, across all six tables |
| Hospitals | `HospitalBuilding` + open `hcai_projects` per facility (same join `ab869_board_rows` already does) |
| Retrofit | `Ab802Building` replacement candidates (reuses `app.pipeline.retrofit`'s existing candidate logic) |
| Contractors | Roster + `nearby_urgency_score` (reuses `app.contractors`) |
| Source status | `SourceRun` health per source — same data `Source Health` already renders, just tabular |

Every tab is a read of tables/functions that already exist; nothing here
computes anything new.

**Delivery:** propose a new authenticated route,
`GET /export/weekly.xlsx` — same pattern as the existing
`/export/bpelsg-roster.csv`, human-pulled rather than emailed. Reasoning:
an xlsx attachment on the sandbox Resend sender adds size/deliverability
risk for no real benefit when the file is one click away on the dashboard
already behind the same HTTP basic auth. **Optional v2:** attach it to the
Sunday digest email instead of / in addition to the download route, if
Andrew would rather it just show up. Flagging as a choice, not deciding it.

**Cadence:** propose generating it as a stage appended to the *existing*
Sunday weekly run (same day as `la_ebewe_benchmarking`/`ua_local_250`/
`la_county_ownership`/`match_contractors`), not a new schedule.

### 3c. LLM calls, cost, and where a human decides — all three pieces

| Piece | LLM calls | Cost | Human decides |
|---|---|---|---|
| §1 nightly diff | None — pure SQL comparison | $0 marginal | Nothing — informational only, no writes a human needs to approve |
| §2 opportunity table | None | $0 | Every stage transition, every `next_action`, is a manual UI action — the pipeline may *suggest*, per 2b, never set |
| §3 brief | None required. Inherits the existing, currently-off `narrate_or_fallback` cheap-model prose pass as an *optional* toggle — same off-by-default posture it already has, not a new spend | $0 required; nonzero only if narration is deliberately re-enabled (same per-send cost as today) | Reading the brief and acting on it is 100% human; nothing in it auto-executes |
| §3 weekly xlsx | None | $0 | Pulled by a human via the export route; nothing auto-attached unless the v2 email-attachment option is chosen |

Consistent with Charter §3 ("Pipeline B must be deterministic SQL") and
§4 invariant 12 ("null over inference, always") — nothing here needs a
model, so nothing here uses one.

---

## Build order and rough hour estimate

Proposed order — each phase gated the way Charter §6 gates the rest of this
project (measure, report, stop before the next one), not built in one pass:

1. **Diff mechanism** (`SourceRowSeen` table + migration, per-table
   fingerprint functions, new cron stage). Foundational — §2 and §3 don't
   need it directly, but §3's "new/changed" section does, and it's the
   smallest, most self-contained piece to gate on real data first.
   **~3–4 hrs.**
2. **Opportunity table** (model, migration, `create_opportunity`/
   `set_stage` writer functions, unique constraints, tests). **~4–5 hrs.**
3. **`/pipeline` board** (list view, filter by stage/rep/due date, links
   back to project/facility detail pages). **~3 hrs.**
4. **Brief integration** — fold §1's diff and §2's overdue opportunities
   into `today_brief()`/`run_notify`, add the cross-source-highlights
   section. **~3–4 hrs.**
5. **Weekly xlsx** (`write_weekly_xlsx` in `deliverables.py`, 6 tabs,
   `/export/weekly.xlsx` route, wired into the Sunday cron stage).
   **~3 hrs.**
6. **Register + document** — `config.yaml` entries for anything newly
   scheduled, `assumptions.py` entries for the fingerprint field choices in
   §1b, RUNBOOK cadence table update, tests throughout (not batched at the
   end — each phase above includes its own). **~1–2 hrs**, spread across
   the above rather than a separate pass.

**Total: ~17–21 hours**, across the phases above. Nothing in this estimate
assumes the scope questions in §2d (route naming) or §3b (email vs.
download) are resolved a particular way — both are cheap to flip either
way before building, expensive to redo after.
