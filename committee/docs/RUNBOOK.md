# Committee runbook

The operating rhythm is deliberately slow: about 20 minutes a day of reading, one
decision session a week, structured reviews monthly and quarterly (DESIGN 13).
Real money follows only after every gate passes. Research, not advice.

## Daily

| When (ET) | System | You |
|---|---|---|
| 07:30 | `digest send` — new briefings, re-review triggers, wash-sale windows, DQ issues | Skim 5–10 min. Never trade from the digest. |
| 19:00 Mon–Fri | `ingest nightly` (EDGAR, prices, macro, news) | — |
| 20:00 Mon–Fri | `review triggers` — falsifier dates, kill criteria, 8-K 4.01/4.02/5.02, −20% from entry | — |
| 20:30 Mon–Fri | `orders reconcile` — fills → journal → tax lots | — |
| 21:00 Mon–Fri | `data check` | — |
| 23:00 | `journal verify` (a break freezes orders, P1) and `journal anchor` | Keep the anchor emails. |
| 23:30 | `ops backup` (encrypted) | — |

## Weekly

- Sunday 18:00 `screen --asof today`; Sunday 21:00 `review batch` (3–6 reviews; fewer when the API budget passes 80%).
- Wednesday 21:00 re-reviews only.
- **One fixed decision session** (30–45 min): open the dashboard → Today. For each briefing whose cooling-off has passed: read it, then approve (smaller or equal size, one-sentence reason) or reject (one-sentence reason). A Behavioral "stop" also needs a written justification and 72 hours.
- After approving: `orders place <approval-hash>` (or the dashboard button). Large approvals execute in tranches under the 2%/order and 5%/day caps.

## Monthly (30 min)

`tax harvest-scan`, `eval monthly` (factor regression, behavioral report, override cohort, cost report). Act on harvest proposals through the same approval gate.

## Quarterly (90 min, fixed checklist — journal the outcome as a `note`)

1. `eval quarterly`: allocator recommendation (frozen for the first 36 live months), agent reweighting (shrunk, ≤2x ratio), scenario refresh.
2. `ops restore-test` on the latest backup — must report the journal verifies.
3. Review pending config changes (`config pending`); any limit change takes effect 7 days after it is journaled. Controlled files: `risk_limits.yaml`, `policy_portfolio.yaml` and `app.yaml` (broker order caps, approval-gate settings). Tightening a cap applies at once; loosening waits the 7 days.
4. Read the scorecards with the sample-size warnings. Nothing short proves skill.

## Annually

Tax report (`tax realized-report --year YYYY`) for the CPA; update `config/tax_config.yaml`; policy portfolio and IPS review with the adviser.

## Gates to real money

| Gate | Criteria |
|---|---|
| G0 Build complete | Prompts 1–17 done; CI green; red-team findings closed |
| G1 Operational burn-in (paper) | 90 days paper; 100% fill reconciliation; zero P1 in final 60 days; journal verify clean daily; ≥ 30 reviews |
| G2 IPS | Written investment policy statement reviewed with a fiduciary adviser and CPA |
| G3 Live core | Core sleeve and rebalancer live; satellite still paper |
| G4 Live satellite at 10% | G3 + 90 days, no P1 |
| G5 Satellite at 20% | 12 months at G4, no guardrail breaches, Brier skill ≥ base rate |
| G6 Allocator in control | After 36 live months |

`gate check` writes docs/LIVE_GATE.md. Going live is manual: sign the gate entry, then set `LIVE_TRADING_ENABLED=true` yourself. No gate is passed because the paper portfolio went up.

## Incidents

| Level | Examples | Action |
|---|---|---|
| P1 | journal verification failure; order without approval record; executed limit breach; secret exposure | `kill-switch` (cancels open orders, blocks submission, read-only). Root-cause write-up before `kill-switch --release --reason "..."`; the journaled reason is the write-up (at least 20 characters). |
| P2 | a source stale > 2 days; agent schema failures > 5%; budget exhausted | Reviews pause; the core continues. |
| P3 | single parse failures, minor DQ warnings | Logged; fix in the next maintenance window. |

### Drills

- **Journal break:** `journal verify` exits 1, sets `var/flags/frozen.json`, journals a P1. Restore the journal from the last backup (`ops restore-test` first), compare with the off-site anchors, then remove the freeze flag only after the write-up.
- **Broker down mid-order:** the gateway journals the rejected order and raises; re-run `orders place` later — completed tranches are not repeated. The retry reuses the same client order id and first asks the broker whether that order already exists (the failure may have come after the broker accepted it); if so it is journaled as `recovered` instead of being sent twice. While the broker cannot be reached, the retry refuses to send anything.
- **Budget exhausted:** the LLM client refuses calls; reviews pause; the digest says so.
- **Lost secret:** rotate the key at the provider, update the keychain, journal a P1 note.

## Commands

```
uv run committee config check | config pending
uv run committee journal verify | anchor | tail
uv run committee ops backup | restore-test [--file F] | health | scheduler
uv run committee kill-switch [--release --reason "..."]
```
