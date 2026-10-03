# Changelog

All notable changes, one entry per build phase (DESIGN 12).

## phase-01: Scaffold and configuration
- uv project with `src/committee/` subpackages (config, data, signals, agents, engines, orchestration, journal, broker, evaluation, ui, ops).
- `config/`: models.yaml (pinned IDs, temperature 0), risk_limits.yaml (exactly DESIGN 9), tax_config.yaml, universe.yaml, policy_portfolio.yaml, signals.yaml, scenarios.yaml, app.yaml.
- Typed pydantic loader; invalid config is a startup failure naming the file and key. Rejects "latest" aliases, a Bear on the Chair's model, missing prohibitions, Kelly above half, policy weights not summing to 100.
- Secrets: keychain, then .env, then environment; live broker keys resolve only when `LIVE_TRADING_ENABLED=true`; masked in all output.
- `committee config check`; pre-commit; GitHub Actions CI (ruff, mypy --strict, pytest).

## phase-02: Hash-chained journal and config change control
- `journal/`: SQLite append-only table; `hash = SHA-256(prev_hash + canonical({seq, entry_type, created_at_utc, payload}))`, so no field can change without breaking the chain. UPDATE/DELETE blocked by triggers.
- `append`, `verify` (seq gaps, broken links, altered rows, non-canonical payloads), `export_anchor`; dated anchor files plus an email hook (stub until ops).
- `committee journal verify` freezes order submission (var/flags/frozen.json) and journals a P1 incident on any break; `committee journal anchor`, `committee journal tail`.
- Config change control: the journal is the source of truth for risk_limits.yaml and policy_portfolio.yaml. Edits are journaled as pending and take effect 7 days later; invalid edits are never journaled. `committee config pending`.

## phase-03a: Point-in-time foundation
- `data/lake.py`: immutable raw zone (gzip, read-only files, keyed by source/entity/known_time) and Parquet PIT lake (`<table>/known_date=…/part-<ingest>.parquet`, lineage columns on every row).
- `data/pit.py`: DuckDB views with the `as_of(known_time, $asof)` macro (DESIGN's `asof`; ASOF is reserved in DuckDB) and `PIT.latest` for latest-version-as-of reads.
- `data/http.py`: fetcher with rate limit, exponential backoff on 429/5xx, timeouts; `FixtureFetcher` for recorded responses.
- `data/security_master.py`: CIK-keyed security master with ticker history; delisted names kept; SIC → sector.
- `data/schemas.py`: column contracts for every PIT table; `domain.py`: shared Lot/Holding/Trade types.

## phase-12: Approval gate and broker
- `broker/approval.py`: approvals reference a journaled briefing/proposal hash; cooling-off (24h, 72h on Behavioral "stop" plus written justification), 7-day expiry, one-sentence reason, approve smaller never larger, only recommended legs; reject and expire are journaled decisions.
- `broker/gateway.py`: the only path to a broker. Kill-switch/freeze flags, approval → briefing chain check, limit orders at last close ± band, per-order (2%) and daily (5%) notional caps independent of the risk engine, tranche execution, deterministic client order ids (idempotent), broker errors journaled, nightly fill reconciliation (deduped, callback for tax lots). Live mode routes only taxable orders to the broker; IRA/401(k) become manual tickets.
- `broker/alpaca.py` (alpaca-py, paper by default), `broker/fake.py`, `broker/live_gate.py` (env flag AND a journaled, human-signed gate file hash).
- Kill switch: cancels open orders, sets the flag, journals it. Property test: caps never exceeded.

## phase-13: Core portfolio manager
- `core/holdings.py`: append-only position snapshots per account in the state DB; manual CSV import for IRA/401(k) (`account,symbol,qty[,cost_per_share,acquired_on]`, CASH pseudo-symbol); shorts rejected.
- `core/drift.py`: sleeve weights across all accounts vs the policy portfolio; band breaches; non-policy symbols counted as satellite and reported.
- `core/rebalance.py`: on any breach, close gaps with new cash first, then tax-free swaps inside IRA/401(k), then taxable sales ranked by tax cost; every buy respects the sleeve's location table; the satellite is never rebalanced here. Proposals carry the approval-gate fields and are journaled as `core_proposal`.

## phase-14: Evaluation, scorecards and capital allocator
- `evaluation/forecasts.py`: Brier, Brier skill vs the Base-Rate agent (paired on the same theses) and vs climatology, reliability bins and Murphy decomposition, per agent × model cohort × event; event resolution from price paths; quarterly agent reweighting with shrinkage and a 2x best/worst cap.
- `evaluation/cohorts.py`: BUY / PASS / VETO / OVERRIDE cohorts at 3/6/12 months with plain-English sample-size warnings; override-underperformance check.
- `evaluation/portfolio.py`: max drawdown, information ratio, years-to-significance (t ≈ IR·√T), PSR, deflated Sharpe with total trial count, minimum track record length, PBO via CSCV, factor regression and NNLS factor-matched ETF benchmark, turnover and after-cost returns.
- `evaluation/trials.py`: journaled trial registry.
- `engines/allocator/rules.py`: DESIGN 3 exactly — 36-month freeze, +5 only if all evidence holds, −5 if any guardrail trips (decrease wins), cap 35 / floor 10; journaled with inputs; human approval required.
- Tests on synthetic data with known answers.

## phase-16: Scheduling, monitoring and operations
- `ops/scheduler.py`: the full DESIGN 13 cadence as APScheduler cron jobs (America/New_York); each job runs a `committee` subcommand in a subprocess; failures journal an incident at the job's level and alert.
- `ops/backup.py`: encrypted (scrypt → Fernet) tar of var/ with consistent SQLite copies; restore and a restore test that verifies the journal chain; pruning.
- `ops/alerts.py` (SMTP, ntfy), `ops/health.py` (journal, order flags, ingest freshness, P1s in 60 days, disk).
- docs/RUNBOOK.md; Dockerfile + docker-compose (scheduler, dashboard, Caddy basic auth).

## phase-15: Dashboard and daily digest
- `ui/app.py` (Streamlit): Today (approval queue with cooling-off status, incidents, DQ), Briefing (briefing, agent outputs, approve smaller-or-equal / reject with reason, STOP justification, approval disabled during cooling-off), Portfolio (sleeves vs policy), Scorecards, Journal (search, verify, anchors), Costs, Settings (active limits, pending config changes, kill switch).
- `ui/views.py` page data as plain functions; `ui/digest.py` morning digest (awaiting briefings, re-review triggers, wash-sale windows, DQ, incidents, blocked-orders banner).
- `orchestration/briefing.py`: one-screen briefing contract + Markdown/HTML renderer.
- `broker/sim.py` offline paper broker; `broker/factory.py` (live only through the gate, else Alpaca paper, else simulator).
- Every page is smoke-tested with Streamlit's AppTest, including an approval from the dashboard.

## phase-18: Live-trading gate check
- `ops/gates.py`: evaluates G0 (red-team findings closed, CI-green note), G1 (90 days paper, 100% fill reconciliation, zero P1 in 60 days, journal verifies with daily anchors, ≥ 30 reviews) and G2 (IPS signed, adviser + CPA review noted) from journal evidence; writes docs/LIVE_GATE.md with PASS/FAIL per criterion. Only when all pass does it write an unsigned gate file; `sign` journals the human signature over its hash. A failing check deletes any stale gate file. Never enables live trading — the env flag stays manual.

## phase-09: Tax engine and lot accounting
- `engines/tax/`: specific-identification lot ledger across taxable, IRA and 401(k) (SQLite event store, replayed in date order); calendar-anniversary holding periods (Feb 29 handled); cross-account wash-sale guard with retroactive detection, partial-lot matching, basis/holding-period tacking in taxable and permanent disallowance in IRA/401(k); shared block list for the screen; lowest-tax lot selection and harvest mode; 60-day long-term warning (default WAIT unless the thesis is broken); location recommender; monthly harvest scan into pre-mapped replacements (journaled as `harvest_proposal`); after-tax hurdle with deferral; realized-gains CSV for the CPA. Every output says "Not tax advice. Confirm with a CPA."
- Substantially-identical groups now live in `tax_config.yaml` (`equivalence_groups`), validated to never overlap the replacement map.

## phase-05: Signal library and weekly screen
- `signals/`: Cohen-Malloy-Pomorski routine/opportunistic insider classifier, opportunistic purchase score `log1p(bps of market cap)`, cluster buys; Lazy Prices TF-IDF similarity of Item 1A / MD&A vs the prior-year same filing with structured diffs (negative-only); value, quality, 12-1 momentum and low-risk composites z-scored within sector and winsorized at ±3; earnings revision behind its flag; short interest and 13F crowding as flags only.
- Deterministic bucket tagging and the six-rule lottery filter (one function and one test per rule).
- `run_screen`: universe filter, weighted composite, top 25 + recent cluster buys, minus holdings under review and the wash-sale block list; signal rows persisted, screen journaled. `committee screen --asof DATE`.
- Fix: `PIT.latest` quotes key columns (`asof` is a DuckDB keyword).

## phase-08/10: Risk and scenario engines
- `engines/risk/`: pure `evaluate(proposal, state, limits)` → PASS/RESIZE/VETO with max order size (%, $, whole shares), every cap evaluated, binding constraints, exact veto rule, theme look-through (satellite and total), marginal satellite vol (constant 0.3 correlation), flags. Sizing: min(discrete log-optimal Kelly, closed form) × 0.5, then default initial size, bucket/satellite/asymmetric-bucket/sector/theme/name-count/ADV/speculative caps, vol scaling (never up), risk-budget modifier. Prohibited instruments, shorts, min holding period and turnover budget veto. Hypothesis: no output ever exceeds any limit.
- `engines/scenario/`: per-holding sensitivities (OLS estimator with known-beta tests, documented sector/sleeve defaults), theme and ETF look-through shocks, portfolio and satellite loss per scenario, risk-budget modifier (floored 0.5, never above 1), weekly regime snapshot for the Macro agent. `committee scenarios run`.

## phase-03/04: EDGAR, prices, macro, news, factors, risk indexes, data quality
- `data/edgar.py`, `form4.py`, `xbrl.py`, `sections.py`: filing index from submissions JSON (known_time = acceptance time, read as New York time), Form 4 parsing (multi-owner, amendments, holdings-only, 10b5-1 checkbox or footnote; failures to dead letter; XML entity declarations rejected), XBRL company facts with concept fallbacks and restatements as new rows, Item 1A / MD&A extraction.
- `data/prices.py` (Massive with Finnhub fallback, back-adjusted closes, corporate actions), `macro.py` (FRED/ALFRED vintages), `news.py` (Finnhub), `factors.py` (Ken French), `risk_indexes.py` (GPR, EPU; Excel via openpyxl/xlrd).
- `data/quality.py`: freshness by source in business days, price sanity with corporate-action matching, Form 4 completeness; journaled `dq_report`. `tests/test_lookahead_lint.py` fails the build on any PIT query without `as_of`.
- CLI: `committee ingest {securities,edgar,prices,macro,news,factors,risk-indexes}`, `committee data check`.

## phase-06/07: LLM infrastructure and the eleven agents
- `agents/llm.py`: Anthropic client wrapper with pinned models, prompt caching on preamble and packet, retries, cost accounting; temperature is sent only to models that accept it (current SDK/models reject it — determinism rests on pinned ids, fixed prompts, canonical packets and the journal). `budget.py`: monthly guard with the 80% downgrade.
- `agents/packets.py`: as-of evidence packets with [E#] ids, anonymized ids, scrubbed names, `<untrusted_content>` wrapping, relative dates, no balances or account data.
- `prompts/`: shared preamble, schema and all eleven prompts copied verbatim from DESIGN 7 (a test enforces it), content-hashed and registered as trials.
- `agents/schemas.py`: strict output models (citations must exist in the packet, probability bounds and sums, explainers cannot change engine numbers, severity ↔ cooling-off). `gates.py`: Chair gates in code (downgrade to WATCH/PASS, clamp size). One repair retry, then fail closed.
- Base-rate reference-class table from PIT prices; Bear sees anonymized, shuffled analyst outputs on a different tier from the Chair; recall probe with a binomial test; eval harness over 5 fixtures. `committee agents dry-run | eval-harness | recall-probe`.

## phase-11: Committee orchestrator and integration
- `orchestration/states.py`: explicit, enforced transitions (SCREENED → … → AWAITING_APPROVAL → APPROVED/REJECTED/EXPIRED → ORDERED → FILLED → MONITORING → EXIT_REVIEW; VETOED; NEEDS_ATTENTION), each journaled.
- `orchestration/review.py`: Base-Rate → analysts (parallel) + Macro → Bear → risk engine (Kelly from the Brier-weighted consensus) → tax engine (location, cross-account wash-sale veto) → explainers → Behavioral Auditor → Chair → code gates → post-Chair risk re-check with the Chair's own probabilities → briefing. Size = min(gate result, both engine maxima). Every forecast is pre-registered before the briefing; a VETO ends early but is scored; failures park in NEEDS_ATTENTION. Re-running a finished review returns the journaled briefing; `cache.py` replays identical LLM requests at zero cost.
- `orchestration/pit_source.py`: production evidence source from the PIT lake (signals, fundamentals, valuation, insiders, Lazy Prices diffs, 8-Ks, news, macro, risk indexes, scenarios, price path), all as of the review date.
- `orchestration/triggers.py`: falsifier dates, 8-K 4.01/4.02/5.02, −20% from entry (review, never auto-sell), quarterly reviews; journaled once and shown in the digest.
- `evaluation/resolve.py`: resolves due forecasts from prices (never early).
- `services.py` + `commands.py`: the full CLI — `screen`, `review run|batch|triggers`, `briefings list|show|expire`, `approve`, `reject`, `orders place|reconcile` (fills become tax lots), `kill-switch`, `core import|drift|propose`, `eval monthly|quarterly`, `digest send`, `ops backup|restore-test|health|scheduler|jobs`, `gate check|sign`, `ingest nightly`, plus the agents, scenarios, tax, ingest and data groups. A test asserts every scheduled job maps to a real command.
