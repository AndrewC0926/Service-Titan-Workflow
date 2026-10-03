# Committee: project rules for Claude Code

Read docs/DESIGN.md before planning any work. It is the source of truth.

## Non-negotiables
- This system NEVER places an order without a human approval record that
  references a briefing hash. Only src/committee/broker/ may call a broker API.
- Agents (LLM calls) never call tools that act. They return JSON only.
- Risk, tax, sizing and eligibility are deterministic Python. Never move that
  logic into prompts.
- Every feature query uses the as-of (known_time) filter. A test enforces it.
- Every agent output, decision, order and config change is written to the
  hash-chained journal.
- Model IDs are pinned in config/models.yaml. Never use "latest" aliases.
- Secrets come from the keychain or .env; never log or print them.

## Engineering standards
- Python 3.12, uv, ruff, mypy --strict on src/, pytest with >85% coverage on
  engines (risk, tax, journal, allocator).
- Small modules, typed dataclasses or pydantic models, no global state.
- Every external call has retries with backoff, timeouts and a recorded
  fixture for tests (no live network in unit tests).
- When unsure about an API (edgartools, Alpaca, Anthropic SDK), look up the
  current docs (use the Context7 MCP if available) instead of guessing.
- Plan first. List files to create or change, tests to write, and risks.
- End each task with: what changed, how to run it, what is not done.

## Layout
- This project lives in the `committee/` folder of a larger repo. Run every
  command from `committee/` (`uv run pytest`, `uv run committee ...`).
