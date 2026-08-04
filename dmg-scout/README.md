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
| `edgar` | SEC full-text search JSON API — ABS/8-K naming campuses | `abs_issuance` | Tier 1, enabled |
| `goed` | NV GOED board packets (PDF) — abatements pre-construction | `abatement_application` | Tier 1, enabled |
| `legistar` | City/county agendas via Legistar Web API | `planning_agenda` | Tier 1, enabled |
| `rss` | Trade press + regional news + Google Alerts | `news_report` | Tier 1, enabled |
| `ats` | Greenhouse/Lever/Ashby public job boards (never LinkedIn) | `job_posting` | Tier 2, enabled |
| air permits, utility filings, FAA 7460, water districts | | | Tier 2, config-stubbed off |
| `manual` | CLI + dashboard form: engineer moves, prequal/bid invites, tips | first-class | enabled |

**Source-verification note (2026-08-04):** built in a sandbox whose egress policy
blocks non-registry domains, so adapters were written against the documented API
shapes (EDGAR FTS params, Legistar OData, GOED WordPress upload paths, CEQAnet
CSV export confirmed via search) and tested against recorded payloads. **Run
`scout verify-sources` immediately after deploy** — it live-hits every enabled
adapter and reports which ones need URL/column adjustments (most likely: CEQAnet
CSV param names and Legistar client slugs). This is a first-deploy checklist item,
not an afterthought; the same command catches future drift.

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
