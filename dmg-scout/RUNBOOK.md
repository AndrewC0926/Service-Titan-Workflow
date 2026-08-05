# DMG Scout Runbook

Written for me-in-six-months who has forgotten everything. Start at "Daily
normal" to reorient, then jump to whatever's broken.

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

After config changes: commit, push, Render redeploys. Nothing else to do.

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
scout backfill --source ceqanet --since 2024-08-01     # chunked + checkpointed
scout backfill --source goed    --since 2024-08-01
scout backfill --source edgar   --since 2024-08-01
scout backfill --source X --since ... --estimate       # price the LLM pass BEFORE triage
scout triage --limit 200 && scout extract --limit 100  # repeat until drained
```
A crash resumes at the first incomplete chunk. `--reset` refetches everything
(dedupe makes that safe, just slow). The daily LLM budget applies to backfill
too — a big backfill either raises `llm.daily_budget_usd` temporarily or
drains over several days. That's a feature.

## Restore from backup

Backups: nightly `pg_dump` custom-format to `$BACKUP_S3_URI/scout-YYYY-MM-DD.dump`,
30-day retention (`scripts/backup.sh` on the dmg-scout-backup cron).

```
aws s3 ls "$BACKUP_S3_URI/"                                   # pick a dump
aws s3 cp "$BACKUP_S3_URI/scout-2026-08-03.dump" /tmp/r.dump
pg_restore --clean --if-exists --no-owner -d "$DATABASE_URL" /tmp/r.dump
```
Test this quarterly against a scratch Postgres (Render lets you spin one up):
restore there first, `SELECT count(*) FROM projects;`, then trust it.
The irreplaceable data is `match_candidates`/`project_signals` (my hand merges),
`outcome_events`, `outreach`, and `contacts` — everything else refetches.

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
| `sizing.watts_per_sqft` | 150 | 100–300 | only used when sqft is the sole input (flagged LOW CONF) |
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
- [ ] Confirm last night's backup object exists; quarterly: test restore.
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
scout golden collect --limit 30 && scout golden review && scout golden report
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
if that changes, add a retention job and note it here. Backups live in
`$BACKUP_S3_URI` with 30-day retention.
