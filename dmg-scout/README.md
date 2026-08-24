# DMG Scout

Early-signal data center project intelligence for an HVAC manufacturers' rep.
Finds projects 12–36 months before mechanical equipment bid — while the basis of
design is still fluid — and ranks them by **winnability**, not size.

```
FETCH → DEDUPE → TRIAGE → EXTRACT → RESOLVE → SIZE → SCORE → NOTIFY
```

- **Fetch/dedupe** — pluggable source adapters, SHA-256 content hashing, `(source, uid)` uniqueness. Idempotent.
- **Triage** — Haiku pass ("data center? named location?") kills ~90% of volume before paying for extraction.
- **Extract** — Sonnet structured extraction, strict JSON schema, nulls over guesses, full raw JSON stored.
- **Resolve** — blocking (county / 5km radius / APN / developer alias) → fuzzy scoring → LLM adjudication → human review queue. Every link records `match_method` + `match_confidence`.
- **Size** — MW / generator HP / sqft → estimated tons with visible `estimate_basis`. (224 MW ≈ 67–90k tons; unit-tested.)
- **Score** — `certainty × window × log(size) × recency`. A 500 MW project out to bid ranks below a 40 MW NOP. Unit-tested.
- **Notify** — daily digest, new and materially changed only, with one "call this person this week" pick.

## Stack

Python 3.12 · FastAPI · SQLModel · Alembic · Postgres · httpx + selectolax · pdfplumber ·
Anthropic API (Haiku triage, Sonnet extraction/adjudication) · Jinja2 + HTMX dashboard · Render deploy.

## Quick start

```bash
make install                  # venv + deps
cp .env.example .env          # fill in DATABASE_URL, ANTHROPIC_API_KEY, DASHBOARD_PASSWORD
make migrate                  # alembic upgrade head
.venv/bin/scout pipeline      # full run: fetch → … → notify (+ healthcheck ping)
make run                      # dashboard at :8000 (HTTP basic auth)
make test                     # 87 tests, no network needed
.venv/bin/scout verify-sources  # live smoke-test each adapter; catches URL drift
.venv/bin/scout doctor          # DB, API key, disk, source freshness, budget, dead man's switch
.venv/bin/scout backfill --source ceqanet --since 2024-08-01   # checkpointed history pull
.venv/bin/scout backfill --source ceqanet --since 2024-08-01 --estimate  # price LLM pass first
.venv/bin/scout golden collect && .venv/bin/scout golden review && .venv/bin/scout golden report
.venv/bin/scout seed-firms      # load MEP/GC/mech-contractor/developer roster
.venv/bin/scout brief 12        # one-page project brief (markdown)
.venv/bin/scout outcome 12 specified --reason "our chillers in BOD"
.venv/bin/scout outcomes        # signal types that convert vs die
.venv/bin/scout add-signal engineer_move "Jane Doe left Syska for kW MCE" --person "Jane Doe" --org "kW MCE"
```

Every stage is also individually runnable (`scout fetch --source ceqanet`, `scout triage`, …)
and idempotent — re-running never duplicates or corrupts.

## Sources

| Adapter | What | Signal | Status |
|---|---|---|---|
| `ceqanet` | CA environmental filings (CSV export + detail pages) | `ceqa_nop`, `ceqa_deir` | Tier 1, enabled |
| `caeatfa` | CAEATFA sales-tax exclusion (STE) board approvals — self-service awards workbook | `abatement_application` | Tier 1, enabled |
| `edgar` | SEC full-text search — data center ABS/CMBS deal documents + named-developer filings, fetched in full | `abs_issuance` | Tier 1, enabled |
| `goed` | NV GOED board packets (PDF) — abatements pre-construction | `abatement_application` | Tier 1, enabled |
| `legistar` | City/county agendas via Legistar Web API | `planning_agenda` | Tier 1, enabled |
| `civicplus` | CivicPlus AgendaCenter — **Storey County, NV** (Tahoe Reno Industrial Center) | `planning_agenda` | Tier 1, enabled |
| `rss` | Trade press + regional news + Google Alerts | `news_report` | Tier 1, enabled |
| `ats` | Greenhouse/Lever/Ashby/Workday public job boards (never LinkedIn) | `job_posting` | Tier 2, enabled |
| `primegov` | PrimeGov portal (City of Reno) — written and verified by hand, but **disabled**: `reno.primegov.com/robots.txt` is `Disallow: /` | `planning_agenda` | disabled |
| air permits, utility filings, FAA 7460, water districts | | | Tier 2, config-stubbed off |
| `manual` | CLI + dashboard form: engineer moves, prequal/bid invites, tips | first-class | enabled |

**Nevada is the point.** The 2024-08 backfill made this concrete: 25 months of
CEQAnet across seven Southern California counties produced 16 documents, while one
Nevada agency produced 75. Storey County is where Vantage, Tract, Google and Switch
build, it has no CEQA equivalent, and it publishes only through CivicPlus — so
`civicplus` exists to close that gap.

### Stub detection

A source can have rows, return 200s, and show a green last run while storing
nothing of value. `scout doc-stats` is the check that catches it — EDGAR sat at
**202 average characters across 3,016 rows** because it stored full-text-search
metadata and never followed the document URL it had recorded. Each adapter declares
a `min_doc_chars` floor; `verify-sources` reports FAIL, not OK, below it.

**Source verification (first live run 2026-08-04, then fixed).** The adapters
were originally written in a sandbox with no egress, against *guessed* endpoint
shapes. The first live `verify-sources` showed how badly that goes: CEQAnet's
CSV endpoint and every parameter name were invented (all 35 queries failed),
ten of twelve Legistar client slugs did not exist, and four of five ATS board
tokens were wrong. All are now confirmed against the live services — see the
comments in `config.yaml` for what each identifier is and how it was verified.

`scout verify-sources` reports three states, and **OK requires documents**:

| State | Meaning |
|---|---|
| `OK` | Returned at least one document |
| `WARN` | Every request succeeded but nothing survived filtering, **or** some configured sub-target (Legistar client, ATS board) is dead |
| `FAIL` | Source unusable, or a majority of its sub-targets failed. Exits 1. |

There is deliberately no "reachable" state: conflating reachable with working is
what let ten broken Legistar slugs report OK while returning zero documents. The
output separates *records scanned* (raw, pre-filter) from *matched* (post-filter)
so an empty result is attributable — 0 records means the endpoint moved, while 0
matched from many records just means the keyword filter is selective.

Run it after any config or adapter change; `--only <adapter>` scopes it.

## Importing the account roster

`scout import-accounts <path-to-csv>` loads a real account list in one shot.
Strict and atomic on purpose: if anything in the file is wrong, **nothing is
written** — you get a full list of every problem, in every row, in one pass,
so you fix the file once and re-run rather than debugging a half-loaded
roster. Safe to re-run any time: rows are matched on normalized account name
+ street address, so reloading the same file (or a corrected version of it)
updates existing accounts in place instead of duplicating them.

**Run `--dry-run` first, always.** `scout import-accounts <csv> --dry-run`
validates the file and prints exactly what would happen — insert / update /
skip per row, and for every row that would update an existing account, every
field that would change (old value → new value) — without writing anything.
This is the command to run standing in the office, before the real one.

**Columns** (exact header text, case/whitespace-insensitive):

| Column | Required | Notes |
|---|---|---|
| `Account Name` | yes | |
| `Street Address` | yes | Part of the matching key — see below. |
| `City` | yes | |
| `County` | yes (as a column) | Value may be blank — see "Missing values" below. |
| `Account Owner` | yes | The rep who owns this account. |
| `Account Type` | yes, no default | One of `mechanical_contractor`, `service_contractor`, `gc`, `owner`, `developer`, `distributor`, `engineer` (case/spacing-insensitive — `"General Contractor"` won't match; use the value itself, e.g. `gc`). Blank or unrecognized fails the whole file — this never silently becomes `mechanical_contractor`, so re-running with a corrected value overwrites whatever was there before, same as any other column. |
| `Last Order Date` | no | `YYYY-MM-DD`, `MM/DD/YYYY`, `MM/DD/YY`, or `YYYY/MM/DD`. |
| `Annual Revenue` | no | Plain number, `$` and `,` are stripped automatically. |
| `Product Lines Bought` | no | Semicolon-separated line names, e.g. `AAON; Marley; LG`. Must match a real line on the card (case/whitespace-insensitive) — a typo here fails the whole file, the same as any other bad cell, rather than silently dropping the line. |

Extra columns in the file are ignored. Missing a *required* column entirely
(not just a blank value — the column itself absent from the header row)
fails the whole file before any row is even read.

**Missing values:**

- **County left blank** on a row: derived from `City` when the city is one
  of the 201 confirmed territory cities in `app/geo.py`; left `NULL`
  otherwise. Never guessed from a zip code, never left as an empty string.
- **`Last Order Date` / `Annual Revenue` blank:** stored as `NULL`, never `0`
  and never today's date.
- **`Product Lines Bought` blank:** no coverage rows are marked bought for
  that account (existing coverage, if any, is untouched).
- **`Account Name` / `Street Address` / `City` / `Account Owner` blank:**
  this is a parse failure — the whole file is rejected, not just that row.

**Known address-matching limitation — numbered avenues.** The street-address
half of the matching key truncates at the first recognized suffix word (see
`app.pipeline.retrofit.normalize_address`'s own docstring — it's shared with
retrofit permit matching, not something this importer changes). "83-100 Ave
45" and "83-100 Ave 47" — both real, plausible Coachella Valley addresses —
normalize to the identical key. If two rows in the *same file* share an
account name and collide this way, the importer catches it and fails the
whole file, naming both rows (never silently keeps one and drops the
other). It does **not** currently check a new row against an *already-
imported* account from a prior run for this same ambiguity — `--dry-run`'s
per-row diff is your check for that case: read the address on every
row it proposes to UPDATE before running for real.

**The one command:**

```
scout import-accounts path/to/roster.csv
```

Prints `N inserted, N updated, N unchanged`, plus how many counties were
derived from city vs. left unresolved. On failure, prints every row/column/
reason and exits non-zero — nothing was written.

To spot-check what Scout's existing data actually resolves against the
accounts you just loaded, run `scout account-join-report`. Raw joins, not a
ranking — see `app.accounts` for all four:

- **By address / by owner name** (`accounts_matching_projects_by_address`/
  `_by_owner_name`): is this account itself a live project's site or its
  developer of record. Real, but mostly answers a question about *owner*/
  *developer*-type accounts, not contractors — expect close to nothing on a
  contractor/GC-heavy roster, and that's a real limit of what's connected in
  Scout's schema today, not a bug in the import.
- **By firm** (`accounts_matching_firm`) — the one that actually fits a
  contractor/GC roster: matches the account name against Scout's own firm
  roster (the same data `search_firms` reads) by normalized name, and reports
  every active project that firm is tied to, with role and stage. This is
  the join that answers "which of my accounts are on live projects right
  now." **Its ceiling depends on how many contractors/GCs are on the firm
  roster at all** — check `scout firm-type-counts` (or query
  `firms.firm_type`) before expecting much; a roster that's mostly
  `developer`/`consultant` firms will make this join look empty regardless
  of how good your account list is.
- **Role coverage** (`account_role_coverage`): "buys from us in N of 13
  roles," from `Product Lines Bought` on import.

A synthetic 40-row example in the exact expected format, with realistic
messiness (mixed case, "Inc" vs "Inc.", missing counties, blank optional
cells, one branch office sharing a name with another at a different
address), is checked in at `tests/fixtures/account_roster_sample.csv` —
run it against a local database to see the whole thing work before you're
standing in front of the real export. `tests/fixtures/account_roster_sample_firms.json`
adds synthetic firm/project data for three of those 40 accounts so the firm
join has something real to find when you try it locally — without it,
`account-join-report` against the sample roster alone will show zero
matches on every join, which is a fact about the fixture having no project
data next to it, not a finding about join quality.

## Deploy (Render)

`render.yaml` defines web + daily cron (6am PT) + Postgres. Point Render at this
repo, set `rootDir: dmg-scout`, add `ANTHROPIC_API_KEY` and `DASHBOARD_PASSWORD`.
The digest defaults to `console` transport (visible in cron logs); switch
`digest.transport` to `resend` or `smtp` in `config.yaml` when ready.

## Tuning

Everything lives in `config.yaml`: counties, keywords, watched companies, signal
certainty priors, window multipliers, sizing assumptions, digest threshold.
Add a Google Alert: append its RSS URL under `sources.rss.feeds`. Add a county:
one line under `sources.ceqanet.counties`. No Python edits.

## Non-negotiables honored

- Idempotent stages; content-hash dedupe; re-extraction replaces, never duplicates.
- Full audit trail: every field traces to a `raw_documents` row with a URL; every
  project–signal link carries method + confidence.
- Nulls over guesses (enforced in the extraction prompt and schema coercion).
- Fail loud: `source_runs` health table, dashboard Source Health view, digest section 4.
- 44 pytest tests: sizing math, scoring law, normalization, resolution, digest dedupe,
  adapters on recorded payloads, dashboard auth + rendering.
