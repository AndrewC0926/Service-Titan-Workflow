# DMG Scout Runbook

Written for me-in-six-months who has forgotten everything. Start at "Daily
normal" to reorient, then jump to whatever's broken.

## Deploy branch — read this before touching git

Render's blueprint (`dmg-scout/render.yaml`) is pointed at
**`claude/dmg-scout-architecture-wvmkqy`**, not `main`. A push to that branch
*is* the deploy — nothing else to do, nothing to merge.

`main` in this repo is a **separate, unrelated project** (ISO 27001/PCI
compliance docs, an OPA policy gate, its own RUNBOOK) that happens to live in
the same GitHub repo. It has no connection to DMG Scout and Render is not
watching it. Do not merge `claude/dmg-scout-architecture-wvmkqy` into `main`
to "deploy" — that mixes two unrelated codebases and does nothing Render
cares about. If a Render dashboard check is ever needed to confirm which
branch a service watches, do that before assuming it's `main`.

## Daily normal

The Render cron runs `scout pipeline` at 6am PT: fetch → triage → extract →
resolve → score → notify. If it completed, healthchecks.io got a ping and the
digest email (or cron log, if transport is `console`) has anything new. The
dashboard is the web service; HTTP basic auth, password in the
`DASHBOARD_PASSWORD` env var on Render.

Quick health read, in order:
1. Phone got no healthchecks.io alert → the cron ran.
2. Dashboard → Source Health → every adapter green within 36h, LLM spend sane.
3. Board has PRE_BOD rows. A red "ZERO PRE-BOD" banner means the early-signal
   sources (CEQAnet NOP, GOED) have stopped producing — that's a fetch problem,
   not a scoring problem.
4. `scout doctor` from a shell on the web service runs all of these checks at once.

## How to add things

All in `config.yaml`. Never edit Python for these.

| Want to add | Where | Notes |
|---|---|---|
| A county | `sources.ceqanet.counties`, and `territory.CA/NV/AZ` if it's mine | CEQAnet list drives fetching; territory list drives the board filter |
| A keyword | `keywords.data_center` (triggers) or `keywords.supporting` | keyword gate runs before the LLM, so missing keywords = invisible documents |
| A news feed / Google Alert | `sources.rss.feeds` | name + RSS url; Google Alerts → create alert → deliver to RSS → paste url |
| A company's job board | `companies.developers/gcs/mep_firms` with `greenhouse:`/`lever:`/`ashby:`/`workday:` | **Never guess the token** — see "Adding a job board" below |
| An EDGAR search | `sources.edgar.queries` or `edgar_terms` on a company | quoted phrases; boolean AND/OR supported |
| A Legistar city | `sources.legistar.clients` | **Never guess the slug** — see "Adding a Legistar client" below |
| A firm to the roster | `roster.<type>` in config **or** the Add-firm form on Contacts | dashboard adds land in the DB only; config survives DB loss — prefer config for permanent rosters |
| A jurisdiction's watch words, thresholds, sizing constants | `scoring.*`, `sizing.*` | see "Config knobs" below |

After config changes: commit, push to `claude/dmg-scout-architecture-wvmkqy`
(see "Deploy branch" above), Render redeploys. Nothing else to do.

### Adding a Legistar client

Slugs are **not** derivable from the city name, and `<slug>.legistar.com`
returns 200 for every subdomain, so that is not a valid test. Confirmed live:
Clark County is `clark` (not `clarkcountynv`), Washoe County is `washoe-nv`,
Riverside County is `riversidecountyca`. Guessing scored 2/12.

```bash
# 1. Does the Web API know this client? 200 + real body names = yes.
curl -s "https://webapi.legistar.com/v1/<slug>/bodies?\$top=5" | head -c 300
# 2. Is the InSite tenant real? A nonexistent slug returns a ~19-byte body;
#    a real one returns 200KB with "<Jurisdiction> - Calendar" in <title>.
curl -s "https://<slug>.legistar.com/Calendar.aspx" | wc -c
```

`HTTP 500` with `LegistarConnectionString setting is not set up in InSite for
client: X` means **the slug does not exist** — it is a permanent config error
wearing a 5xx costume. Do not retry it, do not leave it in config.

If no slug resolves, the jurisdiction probably is not a Legistar customer at
all. Check what its agenda portal actually is (Granicus `ViewPublisher`,
PrimeGov, eScribe, CivicPlus `AgendaCenter`) and record it in the removed-clients
comment block in `config.yaml` rather than leaving a dead slug behind.

### Adding a CivicPlus AgendaCenter jurisdiction

For counties/cities on neither Legistar nor CEQA — which in Nevada means the ones
that matter most. Find the category IDs from the AgendaCenter page source:

```bash
curl -s "https://<host>/AgendaCenter" | grep -o 'id="cat[0-9]*"'      # category IDs
# then confirm a category returns rows (this is the endpoint the adapter uses):
curl -s -X POST "https://<host>/AgendaCenter/UpdateCategoryList" \
  -H "X-Requested-With: XMLHttpRequest" --data "year=2026&catID=4" | grep -c catAgendaRow
```

Use the `id="cat<N>"` numbers, **not** the `CID=` query params on the page — those
belong to unrelated CivicPlus modules (QuickLinks, CivicAlerts).

Then check the PDFs have a text layer before trusting the category. Storey County's
"Notices of Possible Quorum" category is entirely one-page scans (0 chars, 2
images), so it was left out of config:

```bash
curl -s "https://<host>/AgendaCenter/ViewFile/Agenda/_MMDDYYYY-1234" -o /tmp/a.pdf
.venv/bin/python -c "from app.pdftext import pdf_to_text; \
  print(len(pdf_to_text(open('/tmp/a.pdf','rb').read())))"
```

`scout verify-sources --only civicplus` downloads the newest packet on purpose, so
a category that lists rows but yields no text reports FAIL rather than OK.

### Adding a job board

Board tokens are opaque vendor strings. Two failure modes, and the second is
worse than a 404:

1. **404** — no such board.
2. **A real board owned by a different company**, which silently poisons the
   pipeline. `greenhouse/aligned` is alignedup.com (sales SaaS), not Aligned
   Data Centers. `ashby/vantage` is vantage.sh (cloud costs), not Vantage Data
   Centers. Both return 200.

So always eyeball the job titles and apply URLs before adding a board:

```bash
curl -s "https://boards-api.greenhouse.io/v1/boards/<token>/jobs" | head -c 400
curl -s "https://api.lever.co/v0/postings/<slug>?mode=json"        | head -c 400
curl -s "https://api.ashbyhq.com/posting-api/job-board/<name>"     | head -c 400
```

Workday boards are POST-only and configured as a mapping, not a string —
find the tenant/site in the careers-page link
(`https://<tenant>.wdN.myworkdayjobs.com/<site>`):

```yaml
workday: {host: "vantagedc.wd1.myworkdayjobs.com", tenant: "vantagedc", site: "Vantage"}
```

## When an adapter breaks

Symptoms: Source Health shows FAILED, digest lists it under SOURCE FAILURES,
or a source is green but fetching 0 records for days (suspicious — check it).

Diagnose from the archive: every request of every run is in the `http_log`
table (Source Health page shows the last error; for detail,
`SELECT * FROM http_log WHERE source_run_id = <id> ORDER BY ts`).

Tell the failure modes apart:

| Pattern in http_log | It's probably | Do |
|---|---|---|
| 429s, or 503s that recover on retry | Rate limit | Raise `request_interval_seconds`, confirm backoff worked; SEC blocks ~10 min after bursts — wait it out |
| **Intermittent** 403s that appear mid-run and then clear | Rate limit, not a block | CEQAnet does this: a 12-request burst returns 403 around requests 9-10. 403 is in `THROTTLE_STATUS` and retried with backoff. Reduce request count (one CSV per county, not per county x doctype) before touching the UA |
| 403 on **every** request, immediately | Block (UA or IP) | Check `user_agent` still identifies us with email; from a new IP it usually clears; do NOT rotate UAs to evade — if a site means to block us, remove the adapter |
| 404 on listing/CSV endpoints | Site redesign / moved paths, **or an endpoint that never existed** | Open the site, find the real form, read its `action` and field `name`s. CEQAnet's `/Search/DownloadCSV` was invented; the real one is `GET /Search?...&OutputFormat=CSV`, discovered from `/Search/Advanced`. 404s are never retried |
| 500 with a message naming the client/tenant | Config error, not an outage | Legistar returns 500 for unknown client slugs. Fix or remove the slug; 5xx is retried only twice for exactly this reason |
| 200s but 0 documents parsed | Schema drift (renamed CSV columns, changed HTML) | Compare a live response against `tests/fixtures/`; update parser + fixture together |
| 200s but mojibake / `UnicodeDecodeError` | Response is not UTF-8 | CEQAnet's CSV is cp1252 with no charset header. Read bytes and decode explicitly; do not trust `.text` |
| `A string literal cannot contain NUL (0x00) characters` | PDF text carries NUL bytes | `app.sources.base.scrub()` strips them for every adapter. This silently zeroed a whole GOED backfill chunk while the adapter looked healthy — if it reappears, something is bypassing `FetchedDoc` |
| Rows exist, requests are 200, but documents are tiny | **Storing stubs, not content** | `scout doc-stats`. EDGAR sat at 202 avg chars across 3,016 rows because it stored search results and never followed the document URL. Fix the adapter to fetch the body; `min_doc_chars` makes verify-sources FAIL on it |
| Connection errors only | Network/DNS/their outage | Wait a day before touching code |

Retry budgets (`config.yaml`): throttles and transport errors get
`request_max_retries` (4) because waiting helps; 5xx gets
`request_max_retries_5xx` (2) because a 5xx is often deterministic; 404 and other
definitive 4xx get **zero** — `NEVER_RETRY_STATUS` in `app/http.py`.

`scout verify-sources` after any fix. **OK means documents came back**; WARN means
reachable-but-empty or a dead sub-target; FAIL exits 1. Use `--only <adapter>` to
scope it while iterating.

If robots.txt starts disallowing a path we use: the client already refuses it
(RobotsDisallowed surfaces in Source Health). Disable the adapter in config and
note it here; we don't work around robots.

## Backfill

```
# --source is repeatable and comma-separated; sources run in sequence.
scout backfill --source ceqanet --source goed --source civicplus --since 2024-08-01
scout backfill --source edgar --since 2024-08-01
scout doc-stats                                        # rows + avg chars per source, flags stubs
scout purge-source --source edgar --yes                # wipe a wrong corpus before re-backfilling
scout backfill --source X --since ... --estimate       # price the LLM pass BEFORE triage
scout triage --limit 200 && scout extract --limit 100  # repeat until drained
```
A crash resumes at the first incomplete chunk. `--reset` refetches everything
(dedupe makes that safe, just slow). The daily LLM budget applies to backfill
too — a big backfill either raises `llm.daily_budget_usd` temporarily or
drains over several days. That's a feature.

## How much of a document each stage reads

Triage used to read `text[:6000]`. That meant **144 of 204 documents were classified
on their first few pages, and 86% of all stored text never reached triage at all.**
An 82,724-char Storey County commission packet was ruled "no specific building
project" on its first 7% — in the county that holds the Tahoe Reno Industrial
Center. Re-triaging the 48 `other` documents over 20,000 chars with a full read
flipped **11 of them (23%)**, seven being Storey County packets naming Vantage NV12,
a data center campus, PR TX 1, Redwood Battery Materials, and Asia Union.

So triage now reads the whole document up to `llm.triage_max_chars` (60,000) and
above that takes the head plus evenly spaced excerpts through to the last character.
Deliberately **not** extraction's section matcher: that targets EIR and SEC
headings, which a county agenda has none of, so it falls to head+tail and leaves
the middle — where agenda items live — just as invisible.

Coverage cannot be complete at a fixed budget (a 400,000-char prospectus gets
~15%), so every document records `meta['triage_coverage']`, and `run_triage`
returns `irrelevant_partial_read`. A negative verdict reached on 15% of a filing is
a weaker claim than one reached on all of it, and the row carries the difference:

```sql
-- dropped documents that were only partly read: the ones worth revisiting
SELECT id, source, length(raw_text), meta->>'triage_coverage', title
FROM raw_documents WHERE triage_result='irrelevant'
  AND (meta->>'triage_coverage')::float < 1.0 ORDER BY length(raw_text) DESC;
```

`scout doc-stats --fallbacks` lists documents where extraction's heading match
found nothing.

**Extraction had the same disease, one layer down.** Heading matching would stop
early and leave most of its budget unspent — 8,716 chars of a 65,291-char packet
against a 60,000 ceiling, 13%, while triage read 95% of the same file and correctly
reported a data center campus in it. It now tops up with a spanning sample whenever
matching fills less than `llm.section_topup_below_fraction` of the budget, which
took those documents to 74-99%. Unused budget is not a saving; it is a dropped
project.

**What remains is architectural, not a reading problem.** A county agenda packet
holds many projects and a signal holds one, so on a packet naming several the
extractor correctly returns nulls rather than picking one — three of the recovered
Storey packets still produce no project name at 85-99% coverage. Fixing that means
letting one document yield several signals, which is a schema change, not a prompt
change.

## Deferred: known gaps, not yet built

Recorded so they are decisions rather than surprises.

**Multi-signal documents (Phase 5).** One document yields one signal. A county
agenda packet names several projects, so the extractor correctly returns nulls
rather than picking one, and Storey County packets 4386, 4393 and 4402 produce no
project name at 85-99% chunk coverage. This is the binding constraint on the most
important jurisdiction. Fix is one-document-to-many-signals, a schema change.

**Mixed generator fleets (Phase 5).** `generator_count` and `generator_kw_each` are
single fields and cannot represent a fleet of differing units. Vernon stores
40 x 3000 kW = 120 MW; the filing says 38 x 3 MW on critical load plus 2 x 1 MW
house generators = 116 MW nameplate, against a 99 MW stated plant rating. Sizing is
unaffected — it uses the stated 99 MW, not the fleet — but the fleet as stored is
wrong and must not be quoted. Fix is a generator-fleet list on the signal
(`[{count, kw_each, role}]`). Vernon carries a review-queue note meanwhile.

Related: extraction stored `3000 kW` where the document says `3 MW`. The value is
arithmetically right but the prompt says do not convert units, and `scout grounding`
flags it because 3000 appears nowhere in the source.

## Extraction trust: the grounding audit

```
scout grounding             # every asserted number vs its source document
scout grounding --strict    # exit 1 if anything is ungrounded (CI gate)
```

The dangerous extraction failure is a confident wrong value, not a null. This asks
the one question that needs no human: if the model asserts 99 MW, does 99 appear in
the document at all? A number absent from the source cannot have been read from it.
Handles comma and "1.1 million" spellings, and flags matches under 10 as weak, since
a bare `2` matches almost any document by chance.

It does NOT check correctness. A grounded number can still be read off the wrong
row — the product rating instead of the building load. Only the golden set catches
that, which is why both exist.

## Two boards, one pipeline

Triage classifies rather than filters. Every document comes back `data_center`,
`industrial`, or `other`, and only `other` is dropped. The dashboard defaults to
the data center board; `/?category=industrial` and `/?category=all` are the others.

The distinction is what the building *does*, never what kind of document it is or
how technical the applicant sounds. A plant that builds servers is `industrial`.
A tax abatement application is relevant only if it concerns a real building.

Two things to know before quoting a number off the industrial board:

1. **Industrial tonnage never uses IT watts.** Its load is envelope and
   ventilation, so it is sized from floor area via
   `sizing.industrial_sqft_per_ton_*`. Using the data-center 150 W/sqft would
   overstate a warehouse by more than 10x — 1,000,000 sqft is ~1,000-2,900 tons,
   not ~24,000. Every industrial row is flagged LOW CONFIDENCE for this reason.
2. **A "stated" MW is not automatically trusted.** If it implies more
   watts-per-sqft than `sizing.max_watts_per_sqft_by_category` allows, it is
   discarded and the basis string says so. This exists because Amperesand's filing
   stated 500 MW for a 73,000 sqft transformer factory — the product rating, not
   the building load. Trusted, it became 357 MW IT and 136,964 tons, the largest
   number on the board, and it escaped every low-confidence flag *because* it was
   stated. It now sizes at 73-209 tons from floor area.

Re-triaging after a prompt change costs a triage pass, not a re-fetch — raw
documents are kept forever. Sequence:

```bash
# 1. reset the sources you want re-judged (keeps signals + processed_at)
psql "$DATABASE_URL" -c "UPDATE raw_documents SET triage_result='pending'"
scout triage --limit 250
# 2. sync existing signals to their new category, drop signals for now-irrelevant
#    docs, clear their processed_at, then extract whatever is newly relevant
scout extract --limit 100
# 3. the project layer is derived — rebuild rather than patch, or projects keep
#    absorbed values from signals that no longer exist
scout resolve && scout score
```

Manual entries skip triage, so `scout add-signal --category` (and the dashboard
form's category select) sets it. It defaults to `data_center`; if it defaulted to
`other` a hand-entered tip would be invisible on every board.

## Restore from backup

No `pg_dump` cron — Render's paid Postgres plans run continuous point-in-time
recovery, which covers this without a separate job that can fail quietly (the
old dmg-scout-backup cron failed every night for want of AWS credentials that
were never set). Restore from the Render dashboard: database → Backups → pick
a timestamp → Restore, which creates a new database you point `DATABASE_URL`
at after verifying it.

Test this quarterly: restore to a scratch database first, `SELECT count(*)
FROM projects;`, then trust it. The irreplaceable data is
`match_candidates`/`project_signals` (my hand merges), `outcome_events`,
`outreach`, and `contacts` — everything else refetches.

`scripts/backup.sh` (nightly `pg_dump` to S3) still exists for a Render plan
without PITR, or a future migration off Render — nothing calls it today.

## Rotate the API key

1. console.anthropic.com → create new key.
2. Render → both services (web + pipeline cron) → env → replace `ANTHROPIC_API_KEY`.
3. Redeploy, then `scout doctor` — the key check calls the API.
4. Delete the old key at Anthropic. Same procedure for `DASHBOARD_PASSWORD`
   (web only) and `RESEND_API_KEY`.

## Config knobs and sane ranges

| Knob | Default | Sane range | What it does |
|---|---|---|---|
| `request_interval_seconds` | 2.0 | 1–5 | per-domain politeness; SEC needs ≤10 req/s, we're far under |
| `llm.daily_budget_usd` | 15 | 5–50 | hard stop for LLM spend/day; kill switch `SCOUT_LLM_DISABLED=1` |
| `llm.budget_warn_fraction` | 0.8 | 0.5–0.9 | digest/dashboard warning threshold |
| `sizing.tons_per_mw_installed_default` | 325 | 300–400 | tons per MW IT, installed |
| `sizing.band_by_basis.*` | .10/.18/.28/.45 | keep the ordering | estimate band half-width per basis; must widen as input gets more indirect |
| `sizing.watts_per_sqft` | 150 | 100–300 | data centers only, and only when sqft is the sole input (flagged LOW CONF) |
| `sizing.industrial_sqft_per_ton_low/high` | 350 / 1000 | 250–500 / 700–1500 | **every industrial tonnage on the board comes from these two numbers.** Rule of thumb for ranking, not a takeoff — retune against jobs actually quoted |
| `sizing.max_watts_per_sqft_by_category` | 500 DC / 60 industrial | keep DC well above 300 | ceiling on a *stated* MW figure. Above it the number is treated as a misread and discarded — see "Two boards" below |
| `scoring.window_multipliers` | 1.0/0.7/0.15/0.05 | keep monotonic | the winnability core — POST_BOD near zero is the whole point |
| `scoring.signal_certainty.*` | table | 0–1 | priors per signal type; retune from `scout outcomes` once ≥20 closed outcomes |
| `scoring.corroboration_bonus` | 0.25 | 0.1–0.3 | added certainty per extra independent signal type |
| `scoring.recency_halflife_days` | 180 | 90–365 | score halves per this many quiet days |
| `resolution.auto_merge_threshold` | 0.88 | 0.85–0.95 | below it, LLM adjudication; too low = bad auto-merges |
| `resolution.review_threshold` | 0.55 | 0.4–0.7 | below it, new project without asking |
| `scoring.min_digest_score` | 0.15 | 0.05–0.5 | digest noise floor |

## Monthly maintenance checklist

- [ ] `scout doctor` clean.
- [ ] Review queue at zero (dashboard → Review Queue).
- [ ] Skim `scout backfill --estimate` output vs actual month spend on Source Health.
- [ ] Spot-check 3 extractions against their source URLs (project detail → source links).
- [ ] `scout golden report` still zero fabrications; add ~5 fresh docs to the
      golden set (`scout golden collect --limit 5 && scout golden review`).
- [ ] Quarterly: test restore from Render's point-in-time recovery.
- [ ] `scout outcomes` — once ≥20 closed outcomes, consider retuning
      `scoring.signal_certainty` toward what actually converts.
- [ ] Prune watch list (`/watchlist`): archive anything not worth tracking.
- [ ] Check Anthropic model deprecations; model IDs live in `llm.*` config.

## Session 3 live-run script (first deploy — run in order, stop on failure)

Everything below was built and tested against recorded data; these are the
steps that need live egress + the API key. Budget note: raise
`llm.daily_budget_usd` to ~40 for backfill week, then drop it back to 15.

```bash
# Step 2 — deploy + verify
scout doctor                       # DB, key, disk, dead man's switch armed
scout verify-sources               # fix flagged adapters; GOED red = stop, see below

# Step 3/4 — backfill (fetch is LLM-free)
scout backfill --source ceqanet --since 2024-08-01
scout backfill --source goed    --since 2024-08-01
scout backfill --source edgar   --since 2024-08-01
scout backfill --source ceqanet --since 2024-08-01 --estimate   # per-doctype token costs
# >>> approve the number, then drain in batches:
scout triage --limit 200 && scout extract --limit 100   # repeat until board banner clears
scout resolve && scout score

# Step 5 — blind spots
scout coverage                     # any SUSPECTED BLIND county = adapter check first

# Step 6 — extraction accuracy (hand-verify 30 docs)
scout doc-stats --fallbacks    # documents whose middle was never sent to the model
scout golden collect --limit 26 --include-doc <id> --include-doc <id>
scout golden review && scout golden report
# zero fabrications required; note mw_it recall honestly

# Step 7/8 — ranking + contacts
scout audit                        # flags: SINGLE_SIGNAL / SQFT_BASIS / AGGRESSIVE_STAGE
scout ladder                       # THE diagnostic: rung distribution

# Step 9 — digest end to end (set digest.transport: resend first)
scout notify

# Step 10 — deliverables
scout deliverables                 # output/call-list.csv, output/briefs/, output/baseline.json
```

**GOED contingency** (only if verify-sources fails for a real reason, not
network): Legistar already covers Storey/Washoe/Clark/Reno/Sparks and RSS
covers EDAWN + This Is Reno + NNBW; run `scout coverage` and read the Nevada
rows — that's the coverage number. The remaining gap is PUCN dockets / NV
Energy IRP (`sources.utility_filings`, currently disabled): building that
adapter against the live PUCN docket search is the contingency work item.

## Data posture

This system fetches only public data: state environmental clearinghouse
filings (CEQAnet), SEC EDGAR, Nevada GOED board materials, municipal agendas
(Legistar), public RSS news feeds, public ATS job boards (Greenhouse/Lever/
Ashby JSON), and PDFs those pages link. It identifies itself with a descriptive
User-Agent including a contact email, respects robots.txt (refusals surface as
errors rather than workarounds), rate-limits to one request per domain per 2
seconds, and backs off on 429/5xx. It does not scrape LinkedIn or any
authenticated service. It stores names, titles, and contact details of people
**as they appear in public government filings and press**, for sales research
use. Raw documents and the request log are retained indefinitely by default;
if that changes, add a retention job and note it here. Point-in-time recovery
is Render's, on the Postgres instance itself — see "Restore from backup" above.
