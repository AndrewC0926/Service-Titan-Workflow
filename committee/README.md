# Committee

A personal investment system whose job is to keep a disciplined, evidence-backed
process in charge of the account: an index core with small-cap, value and
emerging-market tilts, and a multi-agent research committee managing a 20%
satellite — with a human approving every trade. The design is in
[docs/DESIGN.md](docs/DESIGN.md) (source of truth); operations are in
[docs/RUNBOOK.md](docs/RUNBOOK.md).

**Research, not advice. The human decides.** Allocation numbers are illustrative;
set them with a fiduciary adviser and a CPA. Nothing here is tax advice.

## How it fits together

```
sources ──► point-in-time lake (as_of known_time) ──► signals + weekly screen
                                                     │
                 risk / tax / scenario engines ◄─────┤ (deterministic code decides size and eligibility)
                                                     ▼
          Base-Rate → 4 analysts + Macro → Bear → explainers → Behavioral Auditor → Chair → code gates
                                                     │
                       briefing (journaled) ──► HUMAN APPROVAL (cooling-off + reason) ──► order gateway ──► broker
                                                     │
        hash-chained journal ◄── everything     evaluation (Brier, cohorts, deflated Sharpe) ──► capital allocator
```

Agents only read, argue and forecast (JSON, no tools). Code computes limits,
sizes, taxes and gates. Only `src/committee/broker/` reaches a broker, and only
for a journaled approval that references a journaled briefing.

## Quick start

```bash
cd committee
uv sync --all-extras
uv run committee config check            # validates config, masks secrets
./scripts/check.sh                       # ruff, mypy --strict, pytest (the CI equivalent)

# Offline end-to-end demo: synthetic data + a SIMULATED committee, no network or API key
uv run committee demo --root /tmp/committee-demo --asof 2026-10-02
```

With real data (keys in the OS keychain under service `committee`, or `.env` — see `.env.example`):

```bash
uv run committee ingest securities
uv run committee ingest edgar --tickers AAPL,MSFT,JPM --since 2023-01-01
uv run committee ingest prices --tickers AAPL,MSFT,JPM,SPY
uv run committee ingest macro && uv run committee data check
uv run committee core import positions.csv      # IRA / 401(k) positions
uv run committee screen --asof today
uv run committee review run MSFT                 # one full committee review → briefing
uv run committee briefings list
uv run committee approve <briefing-hash> --reason "..."   # after cooling-off
uv run committee orders place <approval-hash>             # paper by default
uv run streamlit run src/committee/ui/app.py              # dashboard
uv run committee ops scheduler                            # the DESIGN 13 cadence
```

## Layout

| Path | What |
|---|---|
| `config/` | Pinned models, risk limits (7-day change control), policy portfolio, tax, scenarios, signals |
| `prompts/` | Shared preamble, schema and the eleven agent prompts, verbatim from DESIGN 7 |
| `src/committee/journal/` | Append-only SHA-256 chained journal, anchors |
| `src/committee/data/` | Raw zone, Parquet PIT lake + DuckDB `as_of`, EDGAR/prices/macro/news/factors ingestion, DQ |
| `src/committee/signals/` | Insider, Lazy Prices, value/quality/momentum/low-risk, buckets, lottery filter, screen |
| `src/committee/agents/` | LLM client, budget, evidence packets, schemas, Chair gates, the eleven agents |
| `src/committee/engines/` | Risk (Kelly, limits), tax (lots, wash sales, harvest), scenario, capital allocator |
| `src/committee/orchestration/` | Review state machine, PIT evidence source, re-review triggers, briefing |
| `src/committee/broker/` | Approval gate, order gateway (caps, kill switch), Alpaca, simulator, live gate |
| `src/committee/core/` | Holdings, drift, tax-aware rebalancing |
| `src/committee/evaluation/` | Brier/skill, cohorts, deflated Sharpe, PBO, forecast resolution |
| `src/committee/ui/`, `ops/` | Streamlit dashboard, digest; scheduler, backups, health, gates |

Real money only after every gate in DESIGN 13 passes (`committee gate check`).
