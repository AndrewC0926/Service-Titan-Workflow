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
