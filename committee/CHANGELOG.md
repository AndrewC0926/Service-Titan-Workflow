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
