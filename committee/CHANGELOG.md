# Changelog

All notable changes, one entry per build phase (DESIGN 12).

## phase-01: Scaffold and configuration
- uv project with `src/committee/` subpackages (config, data, signals, agents, engines, orchestration, journal, broker, evaluation, ui, ops).
- `config/`: models.yaml (pinned IDs, temperature 0), risk_limits.yaml (exactly DESIGN 9), tax_config.yaml, universe.yaml, policy_portfolio.yaml, signals.yaml, scenarios.yaml, app.yaml.
- Typed pydantic loader; invalid config is a startup failure naming the file and key. Rejects "latest" aliases, a Bear on the Chair's model, missing prohibitions, Kelly above half, policy weights not summing to 100.
- Secrets: keychain, then .env, then environment; live broker keys resolve only when `LIVE_TRADING_ENABLED=true`; masked in all output.
- `committee config check`; pre-commit; GitHub Actions CI (ruff, mypy --strict, pytest).
