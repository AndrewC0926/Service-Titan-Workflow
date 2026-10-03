# Committee: AI Investment Committee Architecture and Build Plan

2026-10-02 · Andrew

## 1. Plan-mode summary

Committee is a personal investment system whose job is to keep a disciplined, evidence-backed process in charge of the account, not to beat the market by cleverness. It runs an aggressive, roughly 93% equity profile: an 80% index core with small-cap, value and emerging-market tilts, and a multi-agent research committee managing a 20% satellite (range 10 to 35%), with a human approving every trade.

**Objective:** maximize long-run, after-tax, risk-adjusted wealth while producing an honest, tamper-evident record of whether the active satellite adds anything over a factor-matched benchmark.

**In scope**

- Core allocation, drift monitoring, rebalancing and tax-aware cash-flow placement across taxable, IRA and 401(k) accounts.
- Satellite research: idea generation, a structured agent debate, calibrated probability forecasts, position sizing and human-approved orders.
- Deterministic risk, tax and scenario engines that agents cannot override.
- A hash-chained decision journal, a calibration and performance scorecard, and a capital allocator that resizes the satellite from evidence.
- Paper trading on Alpaca first; live execution only behind explicit gates (Section 13).

**Out of scope (by design)**

- Intraday or headline-speed trading, options, leverage, shorting, crypto beyond a capped sleeve, and anything autonomous.
- Direct connection to a 401(k) recordkeeper. The 401(k) is modeled from manual position imports; trades there are placed by hand.

**Key decisions**

| Decision | Choice | Why |
|---|---|---|
| Language and runtime | Python 3.12, uv, single-user local or small VPS | Ecosystem for finance data; Claude Code fluency |
| Storage | DuckDB for analytics + Parquet point-in-time lake; SQLite for the journal | Columnar speed, append-only history, simple backups |
| Orchestration | Explicit Python state machine (no agent framework) | Determinism, auditability, easy replay |
| LLM access | Anthropic API, model IDs pinned in config, prompt caching on | Version control of behavior; cost |
| Agent independence | Bear and Risk explainers on a different model family or tier; disjoint inputs | LLM agreement is not independent evidence |
| Forecast format | Explicit probabilities, Brier-scored | Calibration is measurable; 1 to 10 scores are not |
| Broker | Alpaca (paper first; taxable account only if live) | Free paper API; human gate stays in front |
| UI | Streamlit dashboard + daily email digest | Fast to build, local, readable on phone |

**Assumptions to confirm before building**

- U.S. taxpayer with a taxable brokerage account plus IRA and 401(k).
- Horizon of 10+ years; no need to draw on the account within 3 years.
- Satellite universe: U.S.-listed common stocks above $300M market cap and $3M average daily dollar volume (asymmetric bucket), or $2B and $20M (core picks), plus ETFs for the core.
- Allocation numbers below are illustrative and should be set with a fiduciary adviser and a CPA. This is not personalized financial advice.

## 2. Design principles

Every component traces back to one of these twelve rules, each grounded in the research report. When a build choice conflicts with a principle, the principle wins.

| # | Principle | Evidence basis | Where it shows up |
|---|---|---|---|
| P1 | The index core is the default; active capital must earn its allocation | SPIVA: 79% of U.S. large-cap funds lagged the S&P 500 in 2025; persistence below chance | Capital allocator (Section 3) |
| P2 | No speed race; decisions on multi-week to multi-month horizons | LLM news edge decayed (Sharpe 6.54 to 1.22, 2021 to 2024) | Weekly committee cadence; no intraday signals |
| P3 | Haircut every published signal by at least 50% | McLean and Pontiff: returns 58% lower post-publication | Signal weights (Section 6) |
| P4 | Only broad, replicated factor themes | Jensen, Kelly, Pedersen; Hou, Xue, Zhang | Factor exposure engine |
| P5 | LLMs read, extract and argue; deterministic code sizes, limits and decides eligibility | Withdrawn LLM statement-analysis paper; live agent benchmarks mostly fail | Risk, tax, sizing are code, not prompts |
| P6 | Agent agreement is not confirmation | Correlated training data; TradeRank finding that agreement carries no signal | Decorrelation design; disagreement metric |
| P7 | Forecasts are probabilities and get scored | Tetlock; Brier scoring | Every agent outputs P(event) |
| P8 | Base rate first, story second | Bessembinder: most stocks lag T-bills over their lifetime | Base-Rate agent runs before analysts |
| P9 | Evaluate only after the model's training cutoff | Look-ahead and recall contamination studies | Forward-only scorecard; recall probes |
| P10 | After-tax is the only return that counts | Turnover drag; wash-sale rules | Tax engine, asset location |
| P11 | Guard the human as much as the model | Barber and Odean overtrading; disposition effect | Behavioral Auditor; cooling-off; turnover budget |
| P12 | Prepare for scenarios; never forecast politics or war | Partisan trading lowers returns; geopolitical shocks mostly transient unless oil or recession | Scenario engine sets risk budgets, not trades |

## 3. Account architecture

The account runs an aggressive profile: roughly 93% equities, no core bonds, deliberate small-cap and value tilts, and an active satellite that starts at 20%. Higher risk is taken where the evidence says it is paid for (equity, size and value premia), not where it isn't (concentration in a few unproven picks).

**What "high risk" means here**

- Expect a peak-to-trough loss of 45 to 55% in a severe bear market. A globally diversified all-equity portfolio fell by roughly half in 2008 to 2009; small-cap value fell further.
- Expect multi-year stretches where the portfolio trails the S&P 500, because the small-cap, value and ex-U.S. tilts behave differently from U.S. mega-cap growth.
- Compensated risk (more equity, small-cap value, emerging markets) carries an expected premium. Uncompensated risk (one big stock bet, an untested agent pick) mostly adds variance. The design leans into the first and keeps limits on the second.
- The real constraint is behavioral: an aggressive plan only pays if you hold through the drawdown. If a 50% paper loss would make you sell, the profile is too aggressive and should be dialed back before money moves.

**Illustrative policy portfolio (set final weights with an adviser)**

| Sleeve | Holding | Target % of total | Band | Account location |
|---|---|---|---|---|
| Core equity | U.S. total market index ETF | 33 | plus or minus 4 | Taxable first |
| Core equity tilt | U.S. small-cap value ETF | 10 | plus or minus 3 | IRA |
| Core equity | Developed ex-U.S. index ETF | 15 | plus or minus 3 | Taxable (foreign tax credit) |
| Core equity tilt | International small-cap value ETF | 5 | plus or minus 2 | IRA |
| Core equity | Emerging markets index ETF | 10 | plus or minus 3 | Taxable or IRA |
| Diversifier | Trend-following / managed futures ETF | 5 | plus or minus 2 | IRA |
| Liquidity | T-bill ETF or money market | 2 | plus or minus 1 | Taxable |
| Active satellite | Committee-selected stocks (10 to 20 names) | 20 (range 10 to 35) | Allocator-controlled | IRA preferred, then taxable |
| Optional speculative sleeve | Small, capped high-volatility bets (for example crypto), carved out of the satellite | 0 to 3 | Hard cap 3 | Taxable |

Bonds and TIPS are dropped from the core; the trend sleeve is kept because it is the cheapest evidence-backed crisis diversifier and it does not depend on stocks and bonds moving in opposite directions. The small-cap value and ex-U.S. tilts match the late-2026 capital market assumptions (CAPE about 40.6; small caps, value and non-U.S. carry the highest expected returns). If the horizon shortens below 7 years or an income need appears, add back a 10 to 20% bond sleeve.

**Capital allocator rules (deterministic, reviewed quarterly)**

- Start: satellite = 20% of total account value (only after gate G5; see Section 13).
- Increase by 5 points (cap 35%) only if all hold over at least 36 months of live (not paper) results:
  - Deflated Sharpe ratio of satellite active returns vs. the factor-matched benchmark above 0.95.
  - After-tax, after-cost cumulative excess return above zero vs. both SPY and the factor-matched benchmark.
  - Satellite Brier score better than the base-rate-only forecaster.
  - Satellite max drawdown not more than 1.5 times the benchmark's.
- Decrease by 5 points (floor 10%) if any hold:
  - 24-month after-tax excess return below minus 7% vs. the factor-matched benchmark.
  - Brier score worse than the base-rate forecaster for 4 straight quarters.
  - Two or more guardrail breaches (turnover, overrides, limit violations) in a quarter.
- Freeze at current size during the first 36 live months regardless of results. An aggressive profile raises the stakes of mistaking luck for skill; it does not lower the bar.
- Every allocator decision is a journal entry with the inputs that produced it.

**Position-level limits (enforced by the Risk engine)**

- The satellite holds two buckets, each with its own limits (details in Section 6):
  - Core picks (60 to 100% of satellite): established companies, $2B+ market cap, single name max 6% of total account and 30% of satellite.
  - Asymmetric bets (0 to 40% of satellite): smaller, faster-growing, more volatile companies, $300M+ market cap, single name max 2% of total account, max 10 names in the bucket.
- Theme (for example AI capex chain), counted across core look-through plus satellite: satellite contribution max 35% of satellite; total account look-through to the theme flagged above 35%.
- Sector: max 40% of satellite. Revenue concentration in one non-U.S. country: flag above 40%.
- Satellite name count: 8 minimum once fully invested, 25 maximum across both buckets.
- Sizing: half-Kelly as a ceiling (full Kelly is never used; estimation error makes it destructive), then capped by the limits above. Default starting size 3% of total for core picks, 1% for asymmetric bets.
- Not allowed even at this risk level: margin, leverage, short selling, and options (revisit only after G6 with a separate design).

## 4. System architecture

Data flows down one path: sources into a point-in-time lake, signals and engines into the agent committee, the committee into a human gate, and only then to the broker. Everything writes to the journal, and evaluation feeds the capital allocator.

*System architecture · 7 layers, journal and evaluation alongside*

```text
Data sources (EDGAR, prices, news, FRED, GPR/EPU, broker + manual IRA/401k imports)
        |
Point-in-time lake (raw zone, Parquet + DuckDB, as-of known_time; DQ + look-ahead lint) ----> Hash-chained journal
        |                                   |                                                  (append-only, SHA-256,
Signals and screen               Risk, tax, scenario engines                                     model/prompt hashes,
(insider, Lazy Prices, factors;  (deterministic; final word on                                   daily offsite anchor)
 haircut; weekly top 25)          eligibility and size)
        |                                   |
Agent committee (11 agents; Base-Rate first, analysts + Macro, Bear, explainers,  ----> Evaluation (Brier, cohorts,
 Behavioral Auditor, Chair; JSON probabilities only, no tool that can act)                factor-matched benchmark, allocator)
        |
HUMAN APPROVAL GATE (cooling-off + written reason, signed against briefing hash)  <---- Core sleeve manager (drift, rebalances)
        |
Broker adapter (Alpaca paper first; limit orders, notional caps, kill switch)
```

The one hard boundary: nothing above the approval gate can place an order, and nothing in the agent layer can change a limit or a size.

## 5. Data layer

Every record carries two timestamps, when it happened and when the system could have known it, and every query asks "as of" a knowledge time. This one rule is what prevents look-ahead bias.

**Sources**

| Domain | Source | Cost | Knowledge timestamp used | Cadence |
|---|---|---|---|---|
| Filings: 10-K, 10-Q, 8-K, Form 3/4/5, 13F | SEC EDGAR via edgartools | Free (User-Agent with name and email; stay under 10 requests/second) | EDGAR acceptance datetime | Daily 7:00 PM ET |
| Financial statements | EDGAR XBRL company facts | Free | Filing acceptance datetime | Daily |
| Daily prices, splits, dividends | Massive (formerly Polygon.io) or Alpaca market data; Finnhub fallback | Free tier to start; paid tier once universe exceeds about 300 names | Market close + 30 min | Daily |
| Company news | Finnhub | Free tier | Article published time | Daily |
| Macro series (rates, CPI, PCE, unemployment, term premium, breakevens) | FRED; ALFRED for vintages | Free key | Vintage release date | Daily |
| Factor returns for attribution | Ken French Data Library; JKP global factors | Free | Publication date | Monthly |
| Geopolitical and policy risk | Caldara-Iacoviello GPR; Economic Policy Uncertainty index | Free | Release date | Monthly (GPR daily series where available) |
| Valuation context | Shiller CAPE data; published capital market assumptions entered by hand | Free | Entry date | Quarterly |
| Scenario probabilities | Futures-implied rates; Kalshi/Polymarket (inputs only) | Free | Snapshot time | Weekly |
| Analyst estimate revisions (optional) | Point-in-time estimates vendor | Paid | Vendor snapshot | Weekly |
| Holdings and lots | Broker API (Alpaca); manual CSV import for IRA and 401(k) | Free | Import time | Daily / on change |

**Storage design**

- Raw zone: every API response saved as compressed JSON or the original filing, keyed by source, entity and knowledge time. Never mutated.
- Point-in-time lake: Parquet tables partitioned by date, each row with , , , .
- Analytics: DuckDB views over Parquet with an  macro used by every downstream query.
- Journal and state: SQLite, append-only tables, hash-chained (Section 11).
- Survivorship: the security master keeps delisted and merged tickers with CIK, start and end dates.

**Core tables**

| Table | Key fields |
|---|---|
| security_master | security_id, cik, ticker history, exchange, sector, list_date, delist_date |
| prices_daily | security_id, date, open, high, low, close, adj_close, volume, known_time |
| filings | accession, cik, form, period, accepted_at, url, sections_hash |
| filing_sections | accession, item (1A, 7, etc.), text, embedding_ref |
| insider_txns | accession, cik, insider_id, role, txn_code, shares, price, is_10b5_1, txn_date, filed_at |
| fundamentals | security_id, metric, fiscal_period, value, known_time |
| macro_series | series_id, obs_date, value, vintage_date |
| news | security_id, published_at, headline, summary, source_url |
| signals | security_id, signal_name, value, zscore, asof |
| theses | thesis_id, security_id, opened_at, status, version |
| agent_outputs | run_id, agent, model_id, prompt_hash, input_hash, output_json, created_at |
| forecasts | forecast_id, thesis_id, agent, event_def, horizon, probability, created_at, resolved_at, outcome |
| decisions | decision_id, thesis_id, action, human_reason, approved_at |
| orders, fills, lots | broker ids, account, qty, price, fees, lot basis, acquired_at |
| journal | seq, entry_type, payload_json, prev_hash, hash, created_at |

**Data quality checks (run after every ingest)**

- Row count and freshness by source; alert if stale more than one business day.
- Price sanity: no zero or negative prices; daily move above 50% requires a corporate-action match or manual review.
- Filing completeness: every Form 4 parsed or logged to a dead-letter table with the parse error.
- Look-ahead lint: a test that fails if any feature query lacks an  filter.

## 6. Signal layer

Signals are computed by deterministic code, stored with an as-of date, and handed to agents as evidence. Agents interpret signals; they never invent them. Starting weights are deliberately small and get reweighted only from the forward scorecard.

| Signal | Computation | Evidence grade | Starting weight in screen | Role |
|---|---|---|---|---|
| Opportunistic insider purchases | Form 4 code P, not 10b5-1. Insider is routine if they traded in the same calendar month in each of the prior 3 years, otherwise opportunistic. Score = log dollar value of opportunistic buys in 90 days, scaled by market cap | Moderate to robust | 0.20 | Primary idea generator |
| Insider cluster buy | 3+ distinct insiders with opportunistic buys within 30 days | Moderate | +0.05 bonus | Priority flag |
| Lazy Prices change score | Cosine similarity of 10-K/10-Q Item 1A and MD&A vs. prior year same filing; low similarity = negative | Moderate (haircut) | 0.10 (negative only) | Bear evidence, re-review trigger |
| Value composite | Rank average of earnings yield, free cash flow yield, book-to-market, EV/EBIT, within sector | Robust theme | 0.15 | Screen and Valuation agent |
| Quality / profitability | Gross profitability, ROIC, accruals (low is better), leverage | Robust theme | 0.15 | Screen and Fundamentals agent |
| Momentum | 12-month return skipping the latest month, vs. sector | Robust theme, crash-prone | 0.20 | Screen; capped exposure |
| Low risk | 1-year volatility and beta, low is better | Robust theme | 0 (sizing input only) | Sizing input |
| Earnings revision momentum | 3-month change in consensus EPS (only with point-in-time data) | Moderate | 0.10 if available, else 0 | Screen |
| Short interest | Days to cover; percent of float | Moderate | Risk flag only | Bear evidence |
| Earnings-call tone change | LLM-extracted tone vs. company's own last 4 calls | Moderate, short horizon | 0.05 | Fundamentals context |
| News | LLM digest of 30 days | Weak for returns | 0 | Thesis updates and re-review triggers only |
| 13F crowding | Count of top-50 hedge funds holding; change QoQ | Weak | 0 | Crowding risk flag |

**Screen pipeline (weekly, Sunday)**

- Universe filter: U.S. common stock, market cap above $300M, 60-day average dollar volume above $3M, not in an active M&A deal. Names under $2B or $20M ADV are eligible only for the asymmetric bucket and must pass the lottery filter below.
- Compute signal z-scores within sector; winsorize at plus or minus 3.
- Composite = weighted sum. Take the top 25 plus any name with a cluster buy in the last 30 days.
- Remove current holdings already under review and names on the 30-day wash-sale block list.
- Hand the shortlist to the Base-Rate agent and then the committee (Section 8). Target 3 to 6 full committee reviews a week to control cost and attention.

**Factor exposure engine**

- Monthly regression of satellite returns on market, size, value, profitability, investment and momentum factors.
- Builds the factor-matched benchmark: a weighted mix of factor ETFs matching the satellite's measured exposures. This is the benchmark that decides whether the agents add anything beyond cheap factor beta.

**Aggressive stock selection**

Individual picks lean toward smaller, faster-growing and more volatile companies, but the screen separates real upside from lottery tickets. The evidence is blunt on this: the most volatile, most hyped stocks have historically delivered the worst risk-adjusted returns (the low-volatility anomaly and the MAX effect), and most single stocks lag T-bills over their lifetime while a small minority drives all market gains. The goal is to own more of the rare big winners without paying for lottery tickets.

| Bucket | Profile | What the screen looks for | Hard filters |
|---|---|---|---|
| Core picks | $2B+ market cap, established | Composite of insider buying, value, quality, momentum | Standard universe filter |
| Asymmetric bets | $300M to $10B, high growth, higher volatility | Revenue growth above 20% a year with improving gross margin; opportunistic insider buying or cluster buys; positive 12-1 momentum; underfollowed (fewer than 6 analysts) | Lottery filter (below) |

**Lottery filter (asymmetric bucket only, deterministic)**

- Exclude the top decile of maximum single-day return over the past month (the MAX effect).
- Exclude negative gross margin, and require either positive operating cash flow or at least 24 months of cash runway at the current burn rate.
- Exclude share-count dilution above 10% in the trailing 12 months.
- Exclude names whose price rose more than 100% in 3 months without a matching earnings or revenue revision (chasing).
- Exclude recent IPOs and SPACs under 12 months old, and stocks under $5.
- Exclude names where retail-attention spikes (news volume more than 5x its 90-day average) drive the signal.

**How the committee judges an asymmetric bet**

A high-upside position can be worth owning even when it will probably lag, if the payoff is skewed enough. So these bets are judged on skew, not just hit rate: the Chair must forecast P(doubles within 36 months) and P(loses 50% within 36 months), and the expected value of the scenario payoffs must be positive after costs. Size stays at 1 to 2% of the account, so a total loss on any single bet costs at most 2%.

## 7. Agent roster and prompts

Eleven agents, each with one job, a fixed evidence packet built by code, and a JSON output validated against a schema. Four of them sit on top of deterministic engines and only explain or challenge; they cannot change limits.

| Agent | Runs | Inputs (built by code) | Output | Model tier |
|---|---|---|---|---|
| Base-Rate | First, before any analyst | Sector, size, signal profile, historical outcomes of similar setups | Reference-class probability of beating benchmark | Mid |
| Fundamentals | Parallel | 12 quarters of XBRL metrics, segment data, call tone deltas | Business quality assessment + forecasts | Top |
| Valuation | Parallel | Multiples vs. history and peers, reverse DCF inputs | Implied expectations + forecasts | Top |
| Filings and Insiders | Parallel | Form 4 classification, cluster flags, Lazy Prices diffs of Item 1A and MD&A, 8-K items | Information signals + forecasts | Top |
| News and Narrative | Parallel | 30-day digested news, no prices | Narrative state and change; re-review triggers | Mid |
| Macro and Scenario | Weekly portfolio-level, plus per-name exposure | FRED regime vars, GPR, EPU, scenario table | Scenario exposures; risk-budget modifier | Top |
| Bear | After analysts | All analyst outputs, anonymized; adversarial instructions | Strongest case against; kill criteria | Top, different family or tier from Chair |
| Risk explainer | After Bear | Risk engine verdict and numbers | Plain-English explanation; cannot override | Small |
| Tax explainer | After Risk | Tax engine output (lots, holding periods, wash-sale status, location) | After-tax framing; cannot override | Small |
| Behavioral Auditor | Before human gate | Human's override and trade history, proposal context | Bias flags, cooling-off requirement | Mid |
| Chair | Last | Everything above | Briefing, final probabilities, recommended action and size within limits | Top |

**Shared preamble (prepended to every agent's system prompt)**

```
You are one member of a personal investment research committee. You are not a
financial adviser and you do not make decisions. A human approves every action.

Rules that override everything else:
1. Use ONLY the evidence packet provided. Do not use outside knowledge of this
   company's later stock price, events or outcomes. If you recognize the company
   and recall what happened after the as-of date, say "CONTAMINATION_RISK" in
   the flags field and do not use that knowledge.
2. Every factual claim must cite an evidence id from the packet (e.g. [E12]).
   No citation means the claim is not allowed.
3. When evidence is missing, write "insufficient evidence" and lower your
   confidence. Never fill gaps with plausible-sounding numbers.
4. Express beliefs as probabilities of clearly defined events at stated
   horizons. Be calibrated: a 70% forecast should come true about 70% of the time.
   Most single stocks do not beat the market; respect the base rate you are given.
5. Published anomalies are weaker than their papers claim. Treat any signal as
   modest evidence, not proof.
6. Do not reason about politics, elections or wars as trade ideas. Note
   exposures only.
7. Return ONLY valid JSON matching the schema. No prose outside the JSON.

As-of date: {asof}. Ticker is replaced by an anonymous id: {anon_id}.
Benchmark for "beat": {benchmark} total return over the horizon, after an
assumed {cost_bps} bps round-trip cost.
```

**Shared output schema (all analyst agents)**

```json
{
  "agent": "string",
  "anon_id": "string",
  "bucket": "core_pick|asymmetric_bet",
  "thesis_points": [{"claim": "string", "evidence_ids": ["E1"], "direction": "positive|negative|neutral", "strength": 1}],
  "forecasts": [{"event": "beats_benchmark", "horizon_months": 12, "probability": 0.0},
                {"event": "drawdown_exceeds_30pct", "horizon_months": 12, "probability": 0.0},
                {"event": "doubles", "horizon_months": 36, "probability": 0.0},
                {"event": "loses_50pct", "horizon_months": 36, "probability": 0.0}],
  "scenario_payoffs": [{"scenario": "bear|base|bull", "probability": 0.0, "return_36m": 0.0}],
  "key_uncertainties": ["string"],
  "falsifiers": [{"observable": "string", "threshold": "string", "check_by": "YYYY-MM-DD"}],
  "insufficient_evidence": ["string"],
  "flags": ["CONTAMINATION_RISK|DATA_GAP|ACCOUNTING_RED_FLAG|LOTTERY_PROFILE|..."]
}
```

**Base-Rate agent**

```
ROLE: Base-Rate analyst. You speak first so the committee anchors on reality,
not on a story.

You receive: the security's sector, size bucket, signal profile (which screens
flagged it and their z-scores), and a table of historical outcomes for stocks
with similar profiles, computed by code from point-in-time data.

TASK:
1. State the reference class you are using and why it fits.
2. Report the historical frequency with which that class beat the benchmark
   over 3, 6 and 12 months, and the frequency of a 30%+ drawdown.
3. Adjust only for differences the evidence packet documents, and say how
   much each adjustment moves the probability. Keep total adjustment within
   10 percentage points of the raw base rate unless the packet shows an
   extreme, documented difference.
4. Output the base-rate forecasts. Analysts will be scored against you; if
   they cannot beat you on Brier score, they are adding noise.
```

**Fundamentals agent**

```
ROLE: Fundamentals analyst. Judge whether the business is getting stronger or
weaker and how durable its economics are.

Examine, citing evidence ids:
- Revenue growth, gross and operating margin trends over 12 quarters.
- Free cash flow conversion (FCF / net income) and accruals. High accruals
  with weak cash conversion is a red flag.
- Return on invested capital vs. cost of capital; reinvestment runway.
- Balance sheet: net debt / EBITDA, maturities in the next 24 months,
  interest coverage at current rates (assume refinancing at today's yields).
- Dilution: share count change over 3 years net of buybacks.
- Segment mix shifts and customer concentration if disclosed.
- Earnings call tone change vs. the company's own prior four calls.

State the 2 or 3 metrics that would most change your view if they moved, and
set them as falsifiers with thresholds and check dates.
```

**Valuation agent**

```
ROLE: Valuation analyst. Your question is not "is this a good company" but
"what does the current price already assume, and is that assumption
reasonable?"

TASK:
1. Reverse DCF: using the provided inputs, solve for the revenue growth and
   margin path implied by the current price at the provided discount rate.
   Compare it to the company's 10-year history and to peers.
2. Multiples: EV/EBIT, P/FCF, EV/sales vs. the company's own 10-year range
   and sector median. Report percentiles, not adjectives.
3. Scenario values: bear, base, bull with explicit assumptions, and the
   probability you assign to each. Probabilities must sum to 1.
4. Note the current market regime from the packet (high CAPE, high real
   yields). Long-duration growth valuations are more rate-sensitive; quantify
   the price impact of +100 bps in the discount rate.
```

**Filings and Insiders agent**

```
ROLE: Filings and insider-activity analyst. You read what management is
required to disclose and what insiders do with their own money.

Insider rules (from research; follow exactly):
- Only open-market purchases (code P) by insiders classified OPPORTUNISTIC
  carry meaningful positive information. Routine insiders' trades carry
  little. Sales and 10b5-1 plan trades are weak evidence; mention them only
  if unusually large relative to the insider's holdings.
- Cluster buys (3+ opportunistic insiders within 30 days) are stronger.
- Non-executive directors buying in smaller, less-covered companies is the
  most informative pattern documented.

Filing-change rules:
- You receive diffs of Item 1A (Risk Factors) and MD&A vs. the prior year's
  same filing, plus a similarity score. Large changes historically precede
  weaker returns. Summarize WHAT changed: new risks, deleted reassurances,
  changed language on liquidity, customers, litigation, going concern,
  material weaknesses, auditor changes.
- Flag any 8-K items 4.01 (auditor change), 4.02 (non-reliance on prior
  financials), 5.02 (executive departures) prominently.
```

**News and Narrative agent**

```
ROLE: Narrative analyst. You track the story the market tells about this
company. You do NOT predict short-term price moves from headlines; that edge
belongs to faster machines and has decayed.

TASK:
1. Summarize the dominant narrative in 3 sentences, citing articles.
2. Has the narrative changed in the last 30 days? What drove it?
3. List events that should trigger a full committee re-review (guidance cuts,
   regulatory actions, management changes, major contracts lost or won).
4. Narrative risk: how crowded or consensus is the story? Consensus stories
   leave less room for upside surprise.
Your forecasts should stay close to the base rate unless the news documents a
fundamental change.
```

**Macro and Scenario agent**

```
ROLE: Macro and scenario analyst. You never forecast markets, elections, wars
or central bank decisions. You measure exposure and set risk budgets.

You receive: current regime variables (fed funds, 10-year yield, term premium,
breakevens, core PCE, curve slope, oil, dollar, GPR and EPU indexes), the
maintained scenario table with probabilities taken from market prices, and
the security's or portfolio's estimated sensitivities.

TASK (portfolio level, weekly):
1. Classify the regime: growth up/down x inflation up/down, and real-rate
   level. Cite the variables.
2. For each scenario in the table, estimate the portfolio's loss using the
   sensitivities provided; flag any scenario where the loss exceeds the risk
   budget.
3. Recommend a risk-budget modifier between 0.5 and 1.0 applied to new
   satellite position sizes. Explain it in one sentence per driver.

TASK (per name): list the scenarios this company is most exposed to, the
direction, and the evidence for the exposure (revenue geography, energy
intensity, rate sensitivity, supply chain dependence on Taiwan or China).
```

**Bear agent**

```
ROLE: Bear. Your only job is to make the strongest honest case that acting on
this idea is a mistake. You are rewarded for finding real problems, not for
being negative.

You receive all analyst outputs with agent names removed and in random order.

TASK:
1. Identify the single most important assumption the bull case depends on.
   Attack it with evidence from the packet.
2. Find what the analysts missed or under-weighted: accounting quality,
   dilution, debt maturities, customer concentration, filing language changes,
   insider selling, valuation already pricing in success, crowding.
3. Check for agreement-without-independence: if analysts agree, is it because
   they relied on the same evidence? Name the shared evidence.
4. Write a pre-mortem: "It is 12 months later and this position lost 30%.
   The most likely reasons are..."
5. Propose kill criteria: observable events that, if they occur, should force
   an exit review regardless of price.
6. Give your own forecasts. Do not shade them to be contrarian; be calibrated.
```

**Risk explainer**

```
ROLE: Risk explainer. The risk engine (deterministic code) has already
computed limits, exposures and a verdict of PASS, RESIZE or VETO. You cannot
change the verdict or any number.

TASK: explain in plain English, in at most 120 words, what the verdict is,
which limits bind, the scenario losses that matter most, and the maximum size
allowed. If the verdict is VETO, state the exact rule that triggered it.
```

**Tax explainer**

```
ROLE: Tax explainer. The tax engine has computed lot-level consequences, the
recommended account for any purchase, wash-sale exposure across all accounts
including IRAs, and the after-tax hurdle. You cannot change these.

TASK: in at most 120 words, explain the tax consequences of the proposed
action, the after-tax return the position must earn to justify the trade, and
any timing consideration (a lot within 60 days of becoming long-term, a
wash-sale window). You are not a tax adviser; flag items for a CPA.
```

**Behavioral Auditor**

```
ROLE: Behavioral auditor. You protect the human from documented investor
mistakes. You review the proposal AND the human's recent behavior.

You receive: the proposal, the security's recent return path, the human's last
12 months of decisions including overrides of the committee, turnover to date,
and market conditions at each decision.

Check and flag, with evidence:
- Chasing: idea surfaced after a 20%+ run in 60 days, or the human requested
  a ticker that is trending in news.
- Disposition effect: selling winners while holding losers without a thesis
  change.
- Overtrading: turnover pace vs. the annual budget.
- Concentration creep: repeated adds to the same theme.
- Lottery preference: asymmetric-bucket requests that cluster in hyped
  names, or adding to an asymmetric bet after a big run instead of a
  thesis update.
- Panic or euphoria: decisions clustered after large market moves.
- Override pattern: how the human's overrides have performed vs. the
  committee's recommendation.

Output a severity (none, caution, stop) and the required cooling-off period
(0, 24, or 72 hours). "stop" requires a written justification from the human.
```

**Chair**

```
ROLE: Committee chair. You synthesize; you do not advocate.

TASK:
1. Start from the Base-Rate agent's forecast. Move away from it only as far as
   the analysts' cited evidence justifies, and show each adjustment.
2. Weight analysts by their historical Brier scores (provided). Discount
   agreement that rests on shared evidence, as the Bear identified.
3. Answer the Bear's strongest point directly. If you cannot, lower your
   probability.
4. Classify the idea's bucket using the deterministic tag provided:
   CORE_PICK or ASYMMETRIC_BET. You may not change the tag.
5. Final forecasts: P(beats benchmark) at 3, 6, 12 months; P(30% drawdown)
   at 12 months; P(doubles) and P(loses 50%) at 36 months; bear/base/bull
   scenario payoffs with probabilities summing to 1.
6. Recommendation: one of BUY, ADD, HOLD, TRIM, SELL, PASS, WATCH.
   - CORE_PICK: BUY or ADD requires P(beat, 12m) >= 0.58 AND a passing risk
     verdict.
   - ASYMMETRIC_BET: BUY or ADD requires (a) expected value of the scenario
     payoffs > 0 after a 1% round-trip cost, (b) P(doubles, 36m) >= 1.5 x
     P(loses 50%, 36m), (c) a passing lottery filter and risk verdict. A low
     hit rate is acceptable here; a negative expected value is not.
   Size is the risk engine's maximum times the scenario risk-budget modifier,
   never larger.
7. Write the thesis in 2 sentences, the falsifiers with check dates, and the
   kill criteria.
8. End with: "Research, not advice. The human decides."

The briefing must fit on one screen. The human should be able to disagree with
you in under two minutes of reading.
```

## 8. Orchestration

A review is an explicit state machine, not a chat between agents. Each state's inputs and outputs are journaled, so any briefing can be replayed exactly; a failure parks the review in NEEDS_ATTENTION instead of guessing.

*Committee workflow · 13 states, 1 human gate, 1 re-review loop*

```text
Weekly screen -> Base-Rate agent -> Analysts + Macro (parallel, anonymized) -> Bear (pre-mortem, kill list)
  -> Risk engine (PASS/RESIZE/VETO) -> Tax engine (wash-sale, location) -> Behavioral Auditor (flags, cooling-off)
  -> Chair (final probabilities) -> Briefing (one screen, journaled) -> HUMAN APPROVAL (cooling-off + reason)
       approve -> Paper/live order (limit, notional caps) -> Monitoring (falsifiers, triggers) --re-review trigger--> Base-Rate
       reject  -> Rejected / expired (scored as PASS cohort)
```

A Risk VETO ends the review early but the idea is still scored, so you learn whether the filters help. Re-review triggers: a falsifier's check date arrives, a kill criterion is hit, the News agent flags a trigger event, an 8-K item 4.01, 4.02 or 5.02 is filed, price falls 20% from entry (review, never auto-sell), or the quarterly scheduled review.

**Decision gate rules**

- The Chair may recommend BUY or ADD only when P(beat benchmark, 12 months) is at least 0.58 and the risk verdict passes.
- Size equals the risk engine's maximum times the scenario risk-budget modifier; the human may approve smaller, never larger.
- Approvals expire after 7 days; an expired briefing is rerun on fresh data.
- Sells follow the same path, with the tax engine choosing lots and the Behavioral Auditor checking for disposition effect.

## 9. Risk, tax and scenario engines

These three engines are plain Python with unit tests, configured from YAML, and they have the final word on eligibility and size. No prompt can loosen them; changing a limit is a journaled config change with a 7-day delay before it takes effect.

**Risk engine: ******

```yaml
profile: aggressive
account:
  satellite_target_pct: 20.0        # set only by the capital allocator
  satellite_min_pct: 10.0
  satellite_max_pct: 35.0
  speculative_sleeve_max_pct: 3.0
buckets:
  core_pick:
    min_market_cap_usd: 2000000000
    min_adv_usd: 20000000
    max_pct_total: 6.0
    max_pct_satellite: 30.0
    default_initial_pct_total: 3.0
    max_name_annual_vol: 0.70
  asymmetric_bet:
    max_bucket_pct_satellite: 40.0
    max_names: 10
    min_market_cap_usd: 300000000
    min_adv_usd: 3000000
    max_pct_total: 2.0
    default_initial_pct_total: 1.0
    max_name_annual_vol: 1.00
    lottery_filter: true
position:
  kelly_fraction_cap: 0.5
  min_names_satellite: 8
  max_names_satellite: 25
concentration:
  max_sector_pct_satellite: 40.0
  max_theme_pct_satellite: 35.0
  flag_theme_lookthrough_pct_total: 35.0   # core ETFs + satellite
  themes: [ai_capex_chain, taiwan_supply_chain, china_revenue, energy_price]
liquidity:
  max_position_pct_of_adv: 1.0
volatility:
  vol_scale_new_positions: true      # risk control, not return source
portfolio:
  max_satellite_drawdown_review_pct: 30.0   # triggers full review, not auto-sell
  max_total_drawdown_review_pct: 35.0       # triggers IPS review with adviser
  turnover_budget_satellite_annual_pct: 50.0
  min_holding_days: 90                # unless thesis-break logged
prohibited: [margin, leverage, short_selling, options]
cooling_off_hours:
  default: 24
  behavioral_stop: 72
  drawdown_over_25pct: 72             # any sell during a deep drawdown
```

Risk engine outputs per proposal: verdict (PASS, RESIZE, VETO), max allowed size, binding constraints, look-through theme exposure, marginal contribution to satellite volatility, and scenario losses.

**Tax engine rules**

- Lot tracking by specific identification across all accounts; the engine chooses which lots to sell (highest basis, long-term first, unless harvesting).
- Holding-period clock: warn when a lot with a gain is within 60 days of turning long-term; default proposal is to wait unless the thesis is broken.
- Wash-sale guard: block any purchase of a substantially identical security in any account, including IRA and 401(k), within 30 days before or after a loss sale. The block list is shared with the screen.
- Asset location: high-turnover satellite positions go to the IRA first; broad equity index funds to taxable; bonds, TIPS and trend to tax-deferred.
- Harvesting: monthly scan of taxable lots with losses above $1,000 and 5% of basis; harvest into a pre-mapped replacement ETF (correlated, not substantially identical). Log every pair.
- After-tax hurdle: for any taxable sale, compute the return the replacement must earn to beat holding, given the realized tax.
- Annual: produce a capital-gains summary for the CPA. Tax brackets and rates live in a dated config file to update each year.

**Scenario engine**

Maintained scenarios, each with shocks to factors and asset classes, refreshed quarterly and whenever the Macro agent flags a regime change:

| Scenario | Shock definition | Primary exposures |
|---|---|---|
| Oil spike | Brent to $130 for 6 months; CPI +1.5 pts | Airlines, chemicals, consumer discretionary, importers vs. energy, defense |
| Oil collapse | Brent to $65 (Hormuz fully reopens, reserves released) | Energy producers vs. consumers |
| Rate shock | 10-year to 6.0%; equity-bond correlation positive | Long-duration growth, REITs, utilities, long bonds |
| Disinflation rally | 10-year to 4.0%; Fed cuts | Duration, growth; hurts value tilt relatively |
| AI capex reversal | Hyperscaler 2027 capex down 30% | Semiconductors, memory, power equipment, data-center REITs |
| Taiwan supply freeze | Taiwan-dependent revenue goes to zero for 12 months | Semis, hardware, autos |
| U.S.-China truce lapses | Tariffs and mineral export controls reinstated after the January 2027 expiry | China-revenue names, rare-earth users |
| Fiscal standoff | Shutdown or debt-limit brinkmanship under divided government; term premium +50 bps | Broad equity, Treasuries |

Scenario probabilities come from market prices where available (futures, prediction markets) and are inputs to risk budgets only. The engine reports portfolio loss under each scenario; any loss above 2x the satellite's normal 1-year expected volatility reduces the risk-budget modifier.

## 10. Evaluation

The evaluation layer answers one question honestly: is the committee better than a base rate and a cheap factor portfolio, after costs and taxes? Expect it to take years to know; the design makes sure nothing fools you in the meantime.

**Why short records prove nothing.** The t-statistic of active returns is roughly the information ratio times the square root of years:

```latex
t \approx IR \cdot \sqrt{T}
```

An excellent IR of 0.5 needs about 16 years to reach t = 2. So the 90-day paper period is an operational burn-in only, and the capital allocator freezes the satellite size for 36 live months.

**Three scorecards**

| Scorecard | Unit | Metrics | Compared against |
|---|---|---|---|
| Forecast quality | Every probability forecast, per agent and Chair | Brier score, Brier skill score vs. base rate, reliability diagram, resolution | Base-Rate agent; climatology (always predict the historical frequency) |
| Idea quality | Every committee review, acted on or not | 3, 6, 12-month excess return; hit rate with sample size shown; separate cohorts for BUY, PASS and human overrides | SPY, equal-weight S&P 500, sector ETF |
| Portfolio quality | Satellite and total account | After-tax, after-cost return; information ratio; max drawdown; turnover; deflated Sharpe ratio; probability of backtest overfitting; minimum track record length | SPY; factor-matched ETF benchmark; policy portfolio |

**Rules that keep the scorecard honest**

- Pre-registration: every forecast is written to the hash-chained journal before the outcome window opens; resolution is computed by code from price data.
- Trial counting: every prompt version, signal weight change and strategy variant is logged as a trial; the deflated Sharpe ratio uses the total trial count.
- Cohorts by model version: results are split by pinned model ID and prompt hash. A model upgrade starts a new cohort; old and new are compared head to head on the same ideas before switching.
- Rejected ideas are tracked: PASS and VETO ideas get the same scoring, so you learn whether the filters help.
- Human overrides are their own cohort. If overrides underperform the committee, the Behavioral Auditor reports it monthly.
- Agent weights update quarterly by Brier skill, with shrinkage toward equal weights (no more than a 2x ratio between best and worst agent).

**LLM leakage defenses**

- Forward-only evaluation: only forecasts made after the pinned model's training cutoff count.
- Anonymization: tickers and company names replaced with ids in agent packets; dates shifted to relative terms where possible.
- Recall probe (weekly, automated): ask the model, without context, for a sample of securities' returns over past periods inside the evaluation window. Correct recall above chance flags contamination and quarantines that cohort.
- Historical replays are labeled "sanity check, not evidence" in the dashboard and never feed the capital allocator.
- Decorrelation monitor: monthly correlation of agents' forecast errors. Above 0.7 between two agents triggers a review of their inputs or model choice.

## 11. Governance, security and audit trail

Treat the system like a regulated AI deployment with one customer: you. The controls below map to ISO/IEC 42001 themes (risk assessment, human oversight, logging, change management) so the record could stand up to scrutiny later.

**Hash-chained journal**

- Every entry: ,  (ingest summary, agent output, forecast, briefing, decision, order, fill, config change, allocator decision, incident), canonical JSON payload, , , UTC timestamp.
- Agent output entries include , ,  and token counts, so any decision can be replayed exactly.
- Daily anchor: the latest hash is emailed to yourself and written to a dated file in a separate cloud bucket with object lock. Tampering would require altering both.
-  runs nightly and on demand; a break raises a P1 incident and freezes order submission.

**Human oversight controls**

| Control | Rule |
|---|---|
| Approval gate | No order is created without a signed human approval record that references the briefing hash |
| Cooling-off | 24 hours default between briefing and approval; 72 hours on a Behavioral "stop" |
| Written reason | Every approval and every override requires a one-sentence reason, stored in the journal |
| Order caps | Per-order notional cap (for example 2% of account) and daily cap (5%) enforced in the broker adapter, independent of the risk engine |
| Kill switch | One command and a dashboard button: cancels open orders, revokes the trading key flag, sets system to read-only |
| Config changes | Limit changes require a journaled entry and take effect after 7 days |

**Security**

- Secrets in the OS keychain or a  excluded from git; never in prompts or logs. Separate keys for paper and live; live key loaded only when the live flag is on.
- Broker key scoped to trading only. Money movement and withdrawals stay manual through the broker website with MFA.
- Prompt-injection defense: news and filing text is wrapped as data with an explicit "untrusted content" delimiter; agents have no tools that act. Only deterministic code can call the broker adapter.
- Least data: no account numbers, SSNs or balances in agent packets; position weights only.
- Backups: nightly encrypted backup of Parquet, DuckDB and SQLite to cloud storage; quarterly restore test.

**Model and prompt change management**

- Model IDs pinned in ; no "latest" aliases.
- A model or prompt change runs in shadow on the next 20 committee reviews alongside the current version.
- Promote only if Brier skill is not worse and schema validity is at least 99%.
- Every change is a new evaluation cohort (Section 10).

**Cost controls**

- Prompt caching on the shared preamble and evidence packets.
- Monthly API budget cap in the Anthropic console plus an in-app budget guard that downgrades to fewer reviews per week when 80% of budget is used.
- Expect roughly 3 to 6 full reviews per week; track cost per review and per forecast in the dashboard.

## 12. Build plan: Claude Code prompts

Eighteen prompts build the system in dependency order, each ending in tests and a demo you can verify. Export this doc to Markdown as  in the repo first; every prompt tells Claude Code to read it. Run each prompt in plan mode, review the plan, then let it execute.

**Working method for every phase**

- Enter plan mode, paste the prompt, review and edit the plan, then approve.
- Require: tests green,  and  clean, a short demo command, and a  entry.
- Commit at the end of each phase with a tag . Never start the next phase on a red build.
- After each phase, paste the summary back here or into chat and get the next prompt adjusted to what was actually built.

**Prompt 0: CLAUDE.md (save at repo root before Phase 1)**

```markdown
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
```

**Prompt 1: Scaffold and configuration**

```
Read CLAUDE.md and docs/DESIGN.md (sections 1, 4, 5, 11). Plan, then build the
project skeleton for Committee.

- uv project, src/committee/ package with subpackages: config, data, signals,
  agents, engines (risk, tax, scenario, allocator), orchestration, journal,
  broker, evaluation, ui, ops.
- config/: models.yaml (pinned model IDs per agent tier, temperature 0),
  risk_limits.yaml (exactly as in DESIGN section 9), tax_config.yaml (dated
  brackets placeholder, wash-sale window 30 days), universe.yaml,
  policy_portfolio.yaml (DESIGN section 3 table), scenarios.yaml.
- Typed config loader with validation (pydantic). Invalid config = startup
  failure with a clear message.
- Secrets loader: keychain first, .env fallback; .env.example listing
  SEC_USER_AGENT, FINNHUB_KEY, MASSIVE_KEY, FRED_KEY, ANTHROPIC_API_KEY,
  ALPACA_PAPER_KEY/SECRET, ALPACA_LIVE_KEY/SECRET, LIVE_TRADING_ENABLED=false.
- CLI entry point `committee` (typer) with stub commands.
- pre-commit with ruff, mypy, pytest; GitHub Actions CI running the same.
- Tests for config validation.
Demo: `committee config check` prints a validated summary with secrets masked.
```

**Prompt 2: Hash-chained journal and config change control**

```
Read DESIGN section 11. Build src/committee/journal/ before anything else so
every later module writes to it.

- SQLite append-only table journal(seq, entry_type, payload_json, prev_hash,
  hash, created_at_utc). Canonical JSON (sorted keys, no whitespace variance).
- API: append(entry_type, payload) -> JournalEntry; verify() -> report;
  export_anchor() -> latest hash + seq.
- Block UPDATE and DELETE at the DB level with triggers.
- `committee journal verify` and `committee journal anchor` (writes a dated
  anchor file to a configurable path; email hook stubbed).
- Config change control: any change to risk_limits.yaml or
  policy_portfolio.yaml detected at startup is journaled as a pending change
  effective 7 days later; until then the old values stay active.
- Tests: tampering with any row is detected; triggers block edits; canonical
  JSON is stable; pending config changes activate on the right date.
```

**Prompt 3: Security master and SEC EDGAR ingestion**

```
Read DESIGN section 5. Build the point-in-time foundation and EDGAR ingestion
using edgartools.

- Security master with CIK, ticker history, sector, list and delist dates.
  Seed from SEC company tickers; keep delisted names.
- Raw zone writer: store original filings and API JSON keyed by source,
  entity and known_time; never overwrite.
- Ingest for the universe: 10-K, 10-Q, 8-K (with item numbers), Forms 3/4/5,
  XBRL company facts. Use EDGAR acceptance datetime as known_time.
- Respect SEC fair access: User-Agent from config, <= 8 requests/second,
  exponential backoff on 429/503.
- Parse Form 4 into insider_txns including txn_code, shares, price, 10b5-1
  flag (from footnotes or checkbox), insider role. Unparseable filings go to a
  dead-letter table with the error.
- Extract 10-K/10-Q sections Item 1A and Item 7 (MD&A) text into
  filing_sections.
- Parquet point-in-time tables + DuckDB views with an asof() macro.
- Journal an ingest summary entry per run.
- Tests with recorded fixtures, including amendments (4/A), multi-owner
  Form 4s and holdings-only Form 4s.
Demo: `committee ingest edgar --tickers AAPL,MSFT,JPM --since 2023-01-01`
then `committee data check` showing counts and freshness.
```

**Prompt 4: Prices, macro, news and data quality**

```
Read DESIGN section 5. Add the remaining ingestion and the data-quality
suite.

- Prices: Massive (formerly Polygon.io) daily aggregates with splits and
  dividends; Finnhub fallback. known_time = close + 30 minutes. Adjusted and
  unadjusted closes.
- Macro: FRED series list in config (DFF, DGS10, DGS2, T10Y3M, T10YIE,
  THREEFYTP10, CPIAUCSL, PCEPILFE, UNRATE, DCOILBRENTEU, DTWEXBGS). Use
  ALFRED vintages so each value has its vintage_date.
- News: Finnhub company news, 30-day window, stored as untrusted text.
- Factor data: Ken French daily and monthly factors; JKP if accessible.
- GPR and EPU indexes: monthly download with release date.
- Data quality checks from DESIGN section 5 as a `committee data check`
  command and a nightly job; failures journaled and emailed.
- Look-ahead lint: a test that scans src/ for DuckDB queries against PIT
  tables without the asof() macro and fails the build.
Demo: run a full ingest for a 50-name test universe and show the DQ report.
```

**Prompt 5: Signal library and weekly screen**

```
Read DESIGN section 6. Build src/committee/signals/ and the screen.

- Insider classifier: routine vs opportunistic per Cohen-Malloy-Pomorski
  (routine = traded in the same calendar month in each of the prior 3 years).
  Opportunistic purchase score and cluster-buy flag (3+ insiders, 30 days).
- Lazy Prices: TF-IDF cosine similarity of Item 1A and Item 7 vs the prior
  year's same filing; store score and a structured diff (added, removed
  paragraphs) for the Filings agent.
- Value, quality/profitability, momentum (12-1), low-risk composites,
  z-scored within sector, winsorized at +/-3.
- Earnings revision signal behind a feature flag (off unless PIT estimates
  configured).
- Short interest and 13F crowding as flags only.
- Bucket tagging (core_pick vs asymmetric_bet) and the deterministic lottery
  filter from DESIGN section 6, with a test for every exclusion rule.
- Screen per DESIGN: universe filter, weighted composite with weights from
  config, top 25 + cluster buys, minus holdings under review and wash-sale
  blocked names.
- Every signal row stores asof; unit tests on hand-built fixtures verify
  each formula, including the routine-insider rule edge cases.
Demo: `committee screen --asof 2026-09-27` prints the shortlist with the
contributing signals per name.
```

**Prompt 6: LLM client, evidence packets and anonymization**

```
Read DESIGN sections 7, 10 and 11. Build the agent infrastructure (no agent
prompts yet).

- Anthropic client wrapper: pinned model per tier from models.yaml,
  temperature 0, prompt caching on the shared preamble and packets, retries,
  timeout, token and cost accounting, budget guard.
- Structured output: validate every response against the pydantic schema;
  one repair retry with the validation error, then fail closed.
- Evidence packet builder: pulls as-of data per agent, assigns evidence ids
  [E1..En], replaces ticker and company names with an anon_id, wraps all
  news and filing text in <untrusted_content> delimiters.
- Prompt registry: prompts stored as files under prompts/, versioned by
  content hash; preamble composed at runtime.
- Journal every call: agent, model_id, prompt_hash, input_packet_hash,
  output, tokens, cost.
- Recall-probe utility (DESIGN section 10) as a command.
- Tests use recorded responses; no live API calls in unit tests.
```

**Prompt 7: The eleven agents**

```
Read DESIGN section 7. Implement all eleven agents using the infrastructure
from Prompt 6.

- Copy each system prompt and the shared preamble and schema EXACTLY from
  DESIGN section 7 into prompts/. Do not paraphrase.
- Base-Rate agent: build the reference-class outcome table in code from PIT
  data (similar sector, size bucket, signal profile; outcomes at 3/6/12m).
- Bear receives analyst outputs with agent names removed, shuffled.
- Bear and Chair use different model tiers or families per models.yaml.
- Risk and Tax explainers receive only engine outputs (stub engines for now
  with fixed fixtures).
- Behavioral Auditor receives decision history (empty for now) and price
  path.
- An evaluation harness: run all agents on 5 fixture packets and report
  schema validity, citation coverage (every claim has evidence ids that
  exist), probability sanity (sums, bounds) and contamination flags.
Demo: `committee agents dry-run --fixture fx_001` prints each agent's JSON.
```

**Prompt 8: Risk engine**

```
Read DESIGN sections 3 and 9. Build src/committee/engines/risk/ as pure
functions with exhaustive tests.

- Inputs: proposal, current holdings across all accounts (with core ETF
  look-through weights), prices, volatility, ADV, theme tags.
- Outputs: verdict PASS/RESIZE/VETO, max size, binding constraints, theme
  look-through, marginal satellite volatility, scenario losses (from the
  scenario engine interface; stub until Prompt 10).
- Sizing: half-Kelly ceiling using the Chair's probability and the
  scenario-implied payoff, then every limit in risk_limits.yaml, then the
  risk-budget modifier.
- Turnover budget and min holding period checks.
- Property-based tests (hypothesis): no output ever exceeds any limit.
```

**Prompt 9: Tax engine and lot accounting**

```
Read DESIGN section 9 (tax engine rules). Build src/committee/engines/tax/.

- Lot ledger across taxable, IRA and 401(k) with specific identification.
- Holding-period clock and 60-day long-term warning.
- Cross-account wash-sale guard (30 days before and after any loss sale),
  including IRA and 401(k) purchases; shared block list with the screen.
- Asset-location recommender for new purchases.
- Monthly harvest scan with a pre-mapped replacement ETF table in config.
- After-tax hurdle calculation for any taxable sale.
- Annual realized-gains report (CSV) for a CPA.
- Tests for wash-sale edge cases: partial lots, purchases in IRA, repeated
  harvests, replacement mapping.
All outputs labeled: "Not tax advice. Confirm with a CPA."
```

**Prompt 10: Scenario engine and Macro integration**

```
Read DESIGN sections 9 and 7 (Macro agent). Build src/committee/engines/
scenario/.

- Scenarios from scenarios.yaml with factor and asset-class shocks.
- Sensitivities: estimate each holding's betas to market, rates (10y),
  oil, dollar, and tag-based exposures (Taiwan supply chain, China revenue,
  AI capex) from fundamentals and config tags.
- Portfolio loss per scenario; risk-budget modifier rule.
- Weekly regime snapshot from FRED, GPR and EPU for the Macro agent packet.
- Wire the engine into the Risk engine interface.
Demo: `committee scenarios run` prints losses per scenario for the current
paper portfolio.
```

**Prompt 11: Committee orchestrator**

```
Read DESIGN section 8. Build the explicit state machine in
src/committee/orchestration/.

- States: SCREENED -> BASE_RATE -> ANALYSTS (parallel) -> BEAR -> RISK ->
  TAX -> BEHAVIORAL -> CHAIR -> BRIEFED -> AWAITING_APPROVAL -> APPROVED |
  REJECTED | EXPIRED -> ORDERED -> FILLED -> MONITORING -> EXIT_REVIEW.
- Each transition journaled; failures move to NEEDS_ATTENTION with reason.
- Re-review triggers: falsifier check dates, kill criteria hit, News agent
  trigger events, 8-K items 4.01/4.02/5.02, price drop > 20% from entry
  (review, not auto-sell), quarterly scheduled review.
- Briefing renderer: one-screen Markdown and HTML using the Chair output plus
  explainers.
- Idempotent: re-running a stage with the same inputs reuses journaled
  outputs.
Demo: `committee review TICKER --asof today` runs end to end on paper data
and prints the briefing.
```

**Prompt 12: Approval gate and Alpaca paper broker**

```
Read DESIGN section 11 (human oversight). Build src/committee/broker/ and the
approval flow.

- Approval record: briefing hash, chosen action and size (<= recommended),
  one-sentence reason, timestamp; enforced cooling-off window.
- Alpaca adapter using alpaca-py with paper=True by default. Live mode only if
  LIVE_TRADING_ENABLED=true AND a live-gate file signed in the journal exists.
- Order rules: limit orders only, priced from last close +/- configurable
  band, day or GTC; per-order and daily notional caps independent of the risk
  engine.
- Reconcile fills to lots (tax engine) nightly; journal every order and fill.
- Kill switch command and flag.
- Tests with a fake broker: no path creates an order without approval;
  caps enforced; kill switch blocks submission.
```

**Prompt 13: Core portfolio manager**

```
Read DESIGN section 3. Build the core sleeve logic.

- Policy portfolio from config; drift calculation across all accounts.
- Rebalance proposals when any sleeve breaches its band: use new cash first,
  then tax-free accounts, then taxable sales with lowest tax cost.
- Asset-location aware: proposals respect the location table.
- Proposals go through the same approval gate (no agents needed).
- Manual import of IRA/401(k) positions from CSV.
Demo: `committee core drift` and `committee core propose`.
```

**Prompt 14: Evaluation, scorecards and capital allocator**

```
Read DESIGN sections 3 (allocator) and 10. Build src/committee/evaluation/
and engines/allocator/.

- Forecast resolution from prices; Brier score, Brier skill vs base rate,
  reliability bins, per agent, per model cohort.
- Idea cohorts: BUY, PASS, VETO, human override; 3/6/12m excess returns.
- Portfolio metrics after costs and taxes; factor regression and the
  factor-matched benchmark; deflated Sharpe with total trial count;
  probability of backtest overfitting; minimum track record length.
- Trial registry: every prompt hash, weight change and model change counts.
- Agent weight update (quarterly, shrinkage, max 2x ratio).
- Capital allocator implementing DESIGN section 3 exactly, including the
  36-month freeze; output is a journaled recommendation requiring approval.
- Tests with synthetic data where the right answer is known.
```

**Prompt 15: Dashboard and daily digest**

```
Build a Streamlit app in src/committee/ui/ and an email digest.

Pages: Today (briefings awaiting approval, alerts, DQ status); Briefing detail
(all agent outputs, Bear case, explainers, approve/reject with reason and
cooling-off timer); Portfolio (sleeves vs policy, drift, theme look-through,
scenario losses); Scorecards (calibration plots, cohorts, sample-size warnings
in plain English); Journal (search, verify status, anchors); Costs; Settings
(read-only view of limits and pending config changes; kill switch).
Daily digest email at 7:30 AM local: new briefings, re-review triggers,
wash-sale windows, DQ issues. Mobile-readable.
```

**Prompt 16: Scheduling, monitoring and operations**

```
Add operations.

- Scheduler (APScheduler or cron): nightly ingest 7 PM ET weekdays; DQ 9 PM;
  journal verify and anchor 11 PM; weekly screen Sunday 6 PM; committee
  reviews Sunday night and Wednesday; monthly harvest scan, factor
  regression, Behavioral report; quarterly allocator and agent reweighting.
- Health checks and alerting (email; optional ntfy/Pushover).
- Runbook docs/RUNBOOK.md from DESIGN section 13.
- Encrypted nightly backups and a restore-test command.
- Optional deployment: Docker Compose for a small VPS with the dashboard
  behind authentication.
```

**Prompt 17: Red-team and hardening review**

```
Act as an independent reviewer. Do not add features.

1. Threat model: prompt injection through news and filings, secret leakage,
   broker misuse, journal tampering, config tampering. Test each.
2. Look-ahead audit: trace every feature used by agents back to known_time.
3. Failure drills: data source down, malformed Form 4, LLM returns invalid
   JSON, broker API down mid-order, budget exhausted.
4. Verify every non-negotiable in CLAUDE.md with a test that would fail if
   it were violated; add missing tests.
5. Produce docs/REVIEW.md with findings, severity and fixes made.
```

**Prompt 18: Live-trading gate check**

```
Read DESIGN section 13 (gates). Do not enable live trading.

Produce docs/LIVE_GATE.md evaluating each gate criterion with evidence from
the journal and scorecards: operational burn-in complete, zero unresolved P1
incidents in 60 days, journal verify clean, red-team findings closed, paper
order reconciliation 100%, written investment policy statement signed,
adviser/CPA review noted. Output PASS/FAIL per gate. If all pass, generate the
live-gate journal entry for the human to sign; the human sets
LIVE_TRADING_ENABLED manually.
```

## 13. Operating runbook and gates to real money

The operating rhythm is deliberately slow: about 20 minutes a day of reading, one decision session a week, and structured reviews monthly and quarterly. Real money follows only after every gate passes.

**Cadence**

| When | System does | You do (time) |
|---|---|---|
| Weekday mornings | Digest email: new briefings, triggers, DQ issues | Skim (5 to 10 min); no trading from the digest |
| Sunday evening | Screen runs; 3 to 6 committee reviews | Nothing; briefings land Monday |
| One fixed weekly session | Approval queue with cooling-off satisfied | Read briefings, approve or reject with reasons (30 to 45 min) |
| Monthly | Harvest scan, factor regression, Behavioral report, cost report | Review reports; act on harvests (30 min) |
| Quarterly | Allocator recommendation, agent reweighting, scenario refresh, restore test | Formal review using a fixed checklist; journal the outcome (90 min) |
| Annually | Tax report, policy portfolio review, investment policy statement refresh | Meet adviser/CPA; update dated tax config |

**Gates**

| Gate | Criteria | Earliest |
|---|---|---|
| G0 Build complete | Prompts 1 to 17 done; CI green; red-team findings closed | About 8 to 12 weeks of evenings |
| G1 Operational burn-in (paper) | 90 days of paper operation; 100% fill reconciliation; zero P1 incidents in final 60 days; journal verify clean daily; at least 30 committee reviews logged | G0 + 90 days |
| G2 Investment policy statement | Written IPS: goals, horizon, policy portfolio, satellite rules, limits, what would make you stop; reviewed with a fiduciary adviser and CPA | Before G3 |
| G3 Live core | Core sleeve and rebalancer go live; satellite still paper | After G1 and G2 |
| G4 Live satellite at 10% | Satellite live at half its starting target; same gates, cooling-off unchanged | G3 + 90 days with no P1 incidents |
| G5 Satellite at 20% | 12 months at G4 with no guardrail breaches and Brier skill not worse than base rate | G4 + 12 months |
| G6 Allocator in control | Allocator rules (Section 3) govern size after 36 live months | G4 + 36 months |

Passing a gate is about operations and discipline, not returns. No gate is passed because the paper portfolio went up.

**Incident levels**

- P1: journal verification failure, order without approval record, limit breach executed, secret exposure. Action: kill switch, freeze, root-cause write-up before restart.
- P2: data source stale more than 2 days, agent schema failures above 5%, budget exhausted. Action: reviews pause; core continues.
- P3: single parse failures, minor DQ warnings. Action: logged, fixed in the next maintenance window.

## 14. Risks, open questions and what not to build

The biggest risk is not a bug; it is believing a short, lucky record and scaling the satellite. Every other risk below is manageable with the controls already designed.

**Risks**

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Mistaking luck for skill | High | High | 36-month freeze; deflated Sharpe; factor-matched benchmark; trial counting |
| LLM look-ahead contamination | Medium | High | Forward-only cohorts; anonymization; recall probes |
| Correlated agents giving false confidence | High | Medium | Different model tiers; Bear sees anonymized outputs; error-correlation monitor |
| Human overrides and overtrading | Medium | High | Cooling-off; Behavioral Auditor; turnover budget; override cohort |
| Concentration in the AI capex theme through both core and satellite | High in 2026 | High | Look-through theme limits; AI capex reversal scenario |
| Stock-bond correlation turns positive (rate shock) | Medium | High | TIPS, trend sleeve, short duration options in policy portfolio |
| Data vendor changes or outages | Medium | Medium | Fallback sources; DQ freeze on stale data |
| Wash-sale mistakes across accounts | Medium | Medium | Cross-account guard including IRA and 401(k) |
| Tax law and bracket changes | Medium | Medium | Dated config; annual CPA review |
| Build complexity outruns maintenance time | Medium | Medium | Phase gates; core runs even if satellite pauses |
| Selling in a 45 to 55% drawdown (aggressive profile) | Medium | Very high | Written IPS with a pre-committed drawdown plan; 72-hour cooling-off on sells during deep drawdowns; total-drawdown review at 35% with adviser, not a panic exit |

**Open questions to settle before Prompt 1**

- Which accounts exist and their approximate weights (taxable, IRA, 401(k)); which broker holds the taxable account?
- Is moving the taxable account to Alpaca acceptable for live trading, or should live orders stay manual at the current broker with the system producing tickets?
- Final policy portfolio weights and bond duration, set with an adviser.
- Monthly API budget ceiling.
- Hosting: local machine or a small VPS behind authentication.
- Whether to pay for point-in-time analyst estimates (enables the revision signal).

**What not to build**

- Headline or intraday news trading.
- Autonomous execution or any path from an agent to the broker.
- A leaderboard that promotes the "best" agent or model on short records.
- Agreement counting as confirmation.
- 13F or guru cloning as a buy trigger.
- Market timing on CAPE, elections, Fed meetings or geopolitics.
- Continuously held put protection as a default.
- Backtests inside a model's training window presented as evidence.
- Anything built on the withdrawn LLM financial-statement-analysis results.

This design is a framework for a personal research tool, not personalized financial or tax advice.
