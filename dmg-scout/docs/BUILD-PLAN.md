# Scout Build Plan v2.1
Status as of Friday, September 11, 2026. Supersedes v2 (Sep 10). Merges v2, the Sep 10 deep-research report (delivery methods, data center decision chains, K-12 funding sequence, PE licensure, five build prompts), the Aug 25 agent-architecture research, and the NetSuite results from Sep 10 and 11.

---

## 1. What changed since v2 (in 24 hours)

**Done**
- Phase B: NetSuite customer importer, nullable `account_type` with an explicit 20-category map, `netsuite_internal_id` as the only identity key. 1,956 tests green. Committed and pushed; production deploy in progress at time of writing (Dockerfile runs `alembic upgrade head` on start, so both migrations apply on deploy).
- `normalize_company_name` split from `normalize_name`. The LM Construction collision (any "X Construction" firm would have merged into it) is fixed with regression tests. 27 Firm rows re-keyed by migration.
- Sales-order numbers verified from the real file: 655 house-only customers, 836 of 1,629 active mechanical contractors with zero orders since 2021, 14,602 named sold projects, 10 duplicate customer pairs with orders on both records (dedupe proposal written).
- Backtest written up (docs/BACKTEST-2026-09-10.md).

**Learned**
- **0 of 1,359 projects DMG sold into (over $100k) match anything on Scout's 441-project board.** The CEQA-sourced board and DMG's revenue are different populations. DMG's money is hospitals, studios, biotech, defense, industrial, one K-12. This is the strongest evidence for the v2 re-base and it demotes the board from "the product" to "one feed."
- Rep is on every order line; the customer-record rep field is unmaintained. "Underserved" must be measured from orders, not from the customer record.
- NetSuite quotes are not the equipment win/loss log (15% of dollars, loss fields 0% filled). Opportunities and Projects are where bidding contractors and awards live, and neither has been exported.
- Sales-order lines carry no job address. Without the Project export, sold history cannot be joined to the retrofit board or to anything geographic.
- Larry: Jason has a line-card expansion initiative; AHUs are the growth push; boilers are painful and Transom is the heat pump boiler; Energy Labs and Vertiv carry the data center business; Phoenix Controls, Nortec and Temtrol are held by other reps in parts of the territory; inside sales may become product-specific champions.
- The first-two-letters item family key is wrong (catches 2 contractors, should be ~17). Preferred Vendor from the item master is the key. The item master export is missing from disk and must be re-exported.

**Direction added (Sep 10-11)**
- Andrew wants continuous research agents at DMG: daily source scrubbing with a summary of new leads, changes and intel, plus market, competitor and target-segment research. This supersedes the "deliberately not agentic" stance and becomes WS10, under the Aug 25 constraints.

---

## 2. The three layers (unchanged from v2)

1. **Identity.** DMG's own book (NetSuite) joined to CSLB and BPELSG. Who the accounts are, what they buy, who holds an M-license, which contractors are design-build shops.
2. **Priors.** Who works with whom. Developer to design team, architect to mechanical engineer, design-builder to MEP. Seeded from NetSuite sold projects first, then verified public pairings, then Andy.
3. **Timing.** The public milestone that says the pen is about to move, per delivery method. ABSTAIN where Scout cannot classify.

The retrofit motion (53,252 buildings, AB 802, AB 869, Rule 1146.2) is the second engine with the same equipment and a different buyer. It stays.

---

## 3. Gates (hold regardless of workstream)

- **Ownership in writing before any DMG data enters production.** Schema columns are in; they stay empty until Larry or Jason answers. Reports and backtests run on the local restore only.
- **No CRM.** Charter section 7 stands. Opportunity lifecycle stays blocked until Larry decides. Weekly xlsx to Larry is the workaround.
- **Measure first.** Two-hour go/no-go before any parser. Negative results written down.
- **Null over inference.** ABSTAIN is first-class on delivery method, stage, license match, firm match, account type.
- **Terms and robots before touching a source.** Closed: PlanetBids harvest, Bonfire, DIR, SCAQMD FIND, BoardDocs, Granicus, PrimeGov, Legistar attachments, MWD, LADWP. Three PRAs out (HCAI, DIR, SCAQMD).
- **Verify agent closeouts against the repo before building on them.** Sep 10 evening's session claimed four items done and had done two.
- **A week with zero logged outreach is a failed week**, whatever shipped. Outcome count today: 0.

---

## 4. Workstreams

### WS0 Governance (Andrew, no code)
| Item | Status |
|---|---|
| Ownership question to Larry, in writing; to Jason if he defers | Open. Asked verbally Sep 10, no written answer |
| Larry's decision on the Opportunity lifecycle table | Open |
| Meaning of "House DMG" on order lines (parts counter, inside sales, or unassigned) | Open. Ask Andy. The 655-account underserved list depends on it |
| Kevin Nolan status and ACS reassignment | Open. Verify before asking |
| Energy Labs / Liebert channel in territory (direct, rep, national account) | Open |
| Transom spelling, FDNC meaning, Air Treatment situation | Open |
| Commission rate and draw offset, in writing | Open |

### WS1 The Book (NetSuite to identity layer)
| Item | Status |
|---|---|
| 1.1 normalize_company_name fix | Done Sep 10 |
| 1.2 account_type nullable with explicit map | Done Sep 10 |
| 1.3 NetSuite exports: Customer, Sales Order lines, Quote lines | Done. On disk at ~/netsuite-exports |
| 1.4 NetSuite exports still needed: **Item master (re-export), Project records, Opportunity records, Contacts** | Andrew, this week |
| 1.5 BPELSG match: contacts at the 686 Mechanical Engineer customers against the 5,347 M-license rows loaded Sep 8 | Blocked on Contact export (customer file has no contact names) |
| 1.6 Same match against 1,637 Mechanical Contractor customers to flag design-build shops | Blocked on 1.5 |
| 1.7 Underserved lists from orders: house-only (655), zero-purchase mech contractors (836), one-vendor equipment buyers (needs item master) | Reports done except one-vendor; needs House meaning before shown |
| 1.8 Canonical account grouping for the 10 duplicate pairs and NORMAN S. WRIGHT | Proposal written; needs a decision |
| 1.9 NetSuiteOrderLine and NetSuiteProject tables | Proposed; build after ownership answer |
| 1.10 Firm, PE, Project graph (research prompt e): link sold projects to firms and licensed engineers | Not started; depends on Project export |

### WS2 Priors (firm pairings)
| Item | Status |
|---|---|
| `firm_pairing` table: developer to design team, architect to MEP, design-builder to MEP, with source and confidence | **Built 2026-09-12, branch + local DB only.** Table (`design_builder_firm_id`, `partner_firm_id`, `partner_role`, `source`, `source_url`, `observed_date`, `confidence`), migration `b8f2e5a17c33`, schema only (no app.* import, per this repo's other migrations). Seeding is a separate, idempotent function (`app.pipeline.firm_pairing.seed_verified_firm_pairings`, `scout seed-firm-pairings`) that finds-or-creates each Firm by normalized name. **14 rows seeded** on local DB: the plan's 6 verified pairings (Hensel Phelps/P2S, Hensel Phelps/ACCO, Rudolph & Sletten/WSP, Clark/Syska, Webcor/Frank M. Booth, Hensel Phelps/CO Architects -- no source_url, none was given to re-cite) plus 8 more surfaced by WS3.2's document sample across both blocks, each with a real source_url and date where one exists (Rudolph & Sletten/Nacht & Lewis, Hathaway Dinwiddie/LMN, Hathaway Dinwiddie/Alvine Engineering, Suffolk/CO Architects, Gilbane/Gensler, DPR Construction/Steinberg Hart, Hensel Phelps/ZGF Architects, McCarthy/SmithGroup -- the last cross-source corroborated rather than a single direct fetch, disclosed as such). 7 new tests, idempotent-rerun and dedup verified directly. |
| Seed from NetSuite sold projects once the Project export shows engineer and GC fields | Blocked on export |
| Seed the verified public pairings: Hensel Phelps with P2S and ACCO (Harbor-UCLA), Rudolph and Sletten with WSP (San Diego courthouse), Clark with Syska (LA federal courthouse), Webcor with Frank M. Booth (UC Merced), Hensel Phelps with CO Architects (UC higher-ed) | Ready to seed |
| Seed from Andy: design teams for Prologis, Rexford, First Industrial, Link, IDI, Majestic, CRP | Open ask |
| K-12 architect to MEP via CPRA for ten districts, gated by the PlanetBids two-hour test | Days 46-90 |

### WS3 Timing (delivery method and stage)
| Item | Status |
|---|---|
| 3.1 Delivery-method field and stage model per project with ABSTAIN (research prompt a). Populated only from procurement solicitations, never from entitlement documents | **Built 2026-09-11, branch + local DB only.** Naming conflict found and NOT silently resolved: `Project.delivery_method` (plain string) already existed, LLM-populated from ANY triaged document including entitlement filings, read live by app.call_target's R2 rule -- exactly the Kill List's "delivery method from entitlement documents" practice, already live. Added a SEPARATE pair of columns instead of colliding with it: `Project.delivery_method_class` (`DeliveryMethodClass`: design_bid_build, design_build_gc, design_build_trade, progressive_design_build, p3, cmar, unknown, ABSTAIN default) and `Project.pen_holder_role` (`PenHolderRole`: consulting_me, design_builder, owner_standards, unknown, ABSTAIN default). Migration `f4b8c1a92e07`, applied to local DB only, `alembic current` confirms head. Classifier: `app/pipeline/procurement_delivery.py`, config-driven (`config.yaml`'s `procurement_delivery`/`pen_holder_role` keys), no LLM, source-gated -- only `legistar` is on the allowlist today (courts.ca.gov and Cal eProcure are named in the plan but neither has an ingested source module, checked directly). 16 new regression tests (`tests/test_procurement_delivery.py`), all fail without this module by construction (it didn't exist). **Distribution across the board: NOT computed.** Local DB has 0 rows in `projects`/`signals`/`raw_documents` -- confirmed directly, this is not a partial restore, every operational table checked this session (`accounts`, `source_rows_seen`, `opsc_projects`, `projects`, `signals`, `raw_documents`) has been empty. A production read for the real 441-project distribution was denied by the permission system this turn (see WS9's row, same blocker). Expectation stands unverified but is a safe analytical call regardless: since ABSTAIN is the default and only a `legistar`-sourced signal can move a project off it, and the board is CEQA/entitlement-sourced per every other finding in this plan, ABSTAIN should dominate heavily -- this needs a production read (or a fuller local restore) to turn into a real number. **Block 2 (2026-09-12): human decision made and built.** `delivery_method_class`/`pen_holder_role` are now the ONLY delivery fields any rule may read; `app.call_target`'s R2 was switched from the legacy string to `delivery_method_class == design_build_gc` (ABSTAIN/unknown = rule does not fire); the legacy column was renamed `Project.delivery_method_llm_hint`, made display-only, and every scoring/call-target read of it was removed (migration `a3d719c04b5e`) -- guarded by a new static-source-scan test that fails if either `app/call_target.py` or `app/pipeline/scoring.py` ever reads it again. **Distribution against the real 442-project restore: 442/442 (100%) ABSTAIN on both fields.** Not a partial number -- the classifier (`app.pipeline.procurement_delivery`) was never wired into the live signal-absorption pipeline (`app.pipeline.resolve._absorb`) in either block, so every project is at its column default; this is the honest current-state count, not evidence about what the board would show once wiring exists. That wiring is not yet scoped as a plan item. |
| 3.2 Progressive design-build and P3 RFQ and team-award watcher for UC, CSU, Judicial Council, county facilities; parse team members into Firms (research prompt b). Two-hour test first: can one UC award page be parsed into a team list | **Two-hour test done, 2026-09-11. CONDITIONAL GO -- no watcher built, per instruction.** Real page: UC Davis Health Facilities Planning & Development's own RFQ PDF (`health.ucdavis.edu/media-resources/facilities/documents/pdfs/CUP/RFQs/rfq-cup-code-peer-review.pdf`, Central Utility Plant Expansion, robots.txt checked -- blanket `Disallow:` empty, this path not otherwise restricted). Page 3, "1.1 Project Information": `Project Delivery Method: Progressive Design-Build` / `Design-Build Team: Rudolph & Sletten/ Nacht & Lewis Architects` -- a clean, structured label:value field, no LLM needed to pull Design-Builder (Rudolph & Sletten) and Architect of Record (Nacht & Lewis) out of it. **The gap: no MEP firm named anywhere on this document.** Cross-checked against a second real example (UC Irvine's Falling Leaves Foundation Medical Innovation Building -- straight design-build, not progressive, via DBIA's and UCI's own Design & Construction Services pages) which DID name the MEP firm (Alvine Engineering, "Mech./Plumbing Eng.") in a full team-roster table -- so MEP disclosure timing/format looks like it differs between progressive-DB (MEP often joins the design-build team AFTER initial award, not disclosed at RFQ stage) and straight DB (full team sometimes published together). **Sample size is 1 progressive-DB document** -- not enough to commit to a parser/watcher against the plan's own "measure first" gate; both a Design-Builder/Architect page format AND several more UC/CSU/Judicial Council PDB examples (to check the `Project Delivery Method:` / `Design-Build Team:` label format recurs, rather than being one campus's own template) are needed before Day 15-45 build. Source is a scanned/image PDF, not HTML -- app/pdftext.py (already used for OPSC workload PDFs) covers that, not a new capability. **Recommendation: GO on Design-Builder + Architect parsing once the format is confirmed across 5-10 more real documents; NO-GO on MEP at the award/RFQ stage specifically -- that needs a later-stage or different source (design development submittals, HCAI OSP filings for hospital work, or a human check), a separate problem this same watcher does not solve.**

**Block 2 (2026-09-12): 6 more real documents fetched, robots/terms checked on each. FINAL: NO-GO on a deterministic (no-LLM) watcher; the underlying finding held up.**

| Project | Delivery method | Design-builder | Architect | MEP named? | Source shape |
|---|---|---|---|---|---|
| UC Davis Health CUP Expansion | Progressive DB | Rudolph & Sletten | Nacht & Lewis | No | Structured PDF field (`Label: Value`) |
| UC Irvine Falling Leaves | Design-Build | Hathaway Dinwiddie | LMN Architects | **Yes** (Alvine Engineering) | Structured HTML table (retrospective project profile) |
| UCSF Mission Bay Ed. Center | Progressive DB | Suffolk | CO Architects | No | Prose (press release) |
| CSUN Sierra Annex | Design-Build | Gilbane | Gensler | No | Prose (campus FPDC page) |
| CSUN Matador Success Center | Design-Build | DPR Construction | Steinberg Hart | No | Prose (same campus FPDC page) |
| UC Riverside SoCal OASIS | Progressive DB | Hensel Phelps | ZGF Architects | No | Prose (press release) |
| UC Davis Health California Tower | Progressive DB | McCarthy | SmithGroup | No (Degenkolb named, but structural) | Prose, cross-source corroborated (ENR's own article 403'd) |

**Decisive findings:**
- **1 of 8 documents (12.5%) was genuinely structured** (the UC Davis CUP RFQ's own `Label: Value` fields, verified by reading the raw PDF text directly). The other 7 are free-flowing prose -- confirmed by pulling CSUN's own page with plain `curl` and reading the raw HTML text, not relying on a fetch tool's own LLM-based summarization to judge "structure." A regex could match one page's specific sentence shape, but there is no common template across campuses/outlets the way Legistar's one OData API gives WS3.1's classifier -- this is the reason for NO-GO on "deterministic."
- **MEP is named at the team-announcement stage in exactly 1 of 8 samples**, and that one (Falling Leaves) is a retrospective, after-the-fact project profile, not a real-time award announcement -- the other 6 straight/progressive-DB team announcements checked, including two more progressive-DB and two more straight-DB beyond Block 1's sample, all omit it. MEP is NO-GO regardless of extraction method (deterministic or LLM) at this stage of the process; it needs a later-stage or different source, unchanged from the Block 1 finding.
- Design-Builder + Architect ARE reliably present and correctly named across all 8 samples -- the facts are sound, only the extraction method is the open question. An LLM-based extraction (bounded, citation-verified, human-reviewed before landing, matching WS10's own constraints) remains realistic for design-builder + architect specifically; a deterministic keyword/regex parser does not scale past the one structured source type found. **No watcher built, per instruction.** |
| 3.3 K-12 early funding signals (research prompt d): bond ballot project lists, facilities master plans, OPSC eligibility filings 50-01 and 50-03; modernization tagged separately; stage clock on when the district asked | Days 46-90 |
| 3.4 OPSC status map fix (Closed is terminal, not "engineer, spec not locked") | **Verified NOT landed, then built 2026-09-11.** Checked directly: `signal_stage("New Construction", "Closed")` still returned `Stage.entitlement` (a matching pre-existing test asserted exactly that), and `opsc_call_target(..., "Closed")` still returned R4 "engineer, spec not locked" -- the Sep 10 prompt never landed code. Fixed: new `OpscStatusClass` enum + `classify_opsc_status()` (app/models.py) replace the scattered string literals; Closed now maps to `Stage.operating` (signal_stage) and a new `CallTarget.closed` / rule R6 "terminal, no active call target" (opsc_call_target) -- checked before R5/R4, still outranked by R1 standards-owner. `schools_board()` now excludes Closed rows from its default (no-status-filter) view; an explicit `status="Closed"` filter still returns them. 9 new/changed regression tests across `tests/test_opsc.py` and `tests/test_call_target.py`, all fail against the pre-fix code (one by literal old assertion, the rest by construction). Full 1958-test suite (chunked, 6-way parallel) + these 9 new: all green, 0 failures. |
| 3.5 Capital plans and programming-consultant awards for UC, CSU, Judicial Council, county JPAs (18-36 months ahead of any current source) | Measure first, then decide |

### WS4 Data centers (reframed)
| Item | Status |
|---|---|
| Hyperscalers and colos buy centrally as OFE via standards teams; the regional rep has no early in. Kill early-MEP identification | Killed |
| Retrofit and service on the existing LA and OC colo base (AB 802 data-center use-type flag; Vertiv service) | Days 46-90 |
| Follow developers to the Inland Empire and Nevada (NDWR water rights source) | Days 46-90 |
| DC reference layer: verified SoCal MEP firms and design-builders with DC practices, source-verified flag (research prompt c) | Reference only, low priority |

### WS5 Healthcare
| Item | Status |
|---|---|
| AB 869 board, HCAI open mechanical projects, OSP brief | Live |
| Join HCAI facilities to NetSuite customers and sold projects (Rady, UCIMC, Scripps, City of Hope are all in the sold list) | After ownership answer |
| OSP renewal ask to Jason (chillers, fans) | Open |
| Larry's "FDNC groups" comment | Clarify |

### WS6 Line card gap list for Jason
| Item | Status |
|---|---|
| Equipment dollars by Preferred Vendor since 2021; card lines under 1% share | Blocked on item master re-export |
| Quote-to-order conversion by manufacturer from quote lines | Ready to compute |
| OSP status per line, Larry's priorities (AHUs up, boilers painful), competitor rep holdings (Phoenix, Nortec, Temtrol) on the same row | Build after the two reports |
| Deliverable: one ranked page, counts behind every row | Day 45 |

### WS7 Internal docs (DMG-owned, not in Scout)
| Item | Status |
|---|---|
| One template per line, seven fixed corporate folders, layer-two flexibility per product team, START HERE pages, Draft / Verified / Stale states | Designed |
| Pilot on Energy Labs, AAON, LG, SPX/Marley, ClimateMaster | Not started |
| "Log a loss" link in the template as the mechanism that generates the win/loss data NetSuite lacks | Design decision made |
| Fits Larry's inside-sales champion idea; raise it as "here is my template" | Andrew |

### WS8 Field motion and outcomes
| Item | Status |
|---|---|
| 40 calls in 30 days spread across signal age and urgency, logged from the phone | 0 logged |
| Weekly xlsx to Larry | Not started |
| 200 outcomes = validated thesis | 0 |
| Today view: add active-learning spread so the three calls are not always the top three | Not built |

### WS9 Hygiene
| Item | Status |
|---|---|
| Verify the nightly diff and 6am brief ran | **Checked 2026-09-11 (Block 1), corrected 2026-09-12 (Block 2).** Render cron `dmg-scout-pipeline` succeeded every day 2026-08-23 through 2026-09-11, no gaps. **The Block 1 "16:00-18:16 UTC, not 13:00 UTC" timing flag was a misread** -- the schedule is fine, last run 6:00 AM PDT (13:00 UTC) today, per direct correction. **Now computed against the fresh production restore (442 projects, local DB):** `source_rows_seen` is 118,742 rows across ab802_buildings (50,259), hcai_projects (45,132), opsc_projects (14,506), scaqmd_facilities (8,644), ab869_plans (201) -- `last_seen_at` shows 118,742/118,742 on 2026-09-11 (today), confirming the bulk refresh ran, but that column is always overwritten to the latest run's date for every present row, so it cannot show a multi-day history by itself. `changed_at` (the real "something actually changed" signal) is **NULL on all 118,742 rows** -- zero genuine content changes registered recently across any of the 5 diffed sources; the "new or changed" digest section would show nothing today, which is a real, reportable quiet period, not a bug demonstrated here. `digest_log` (today's brief content) has real entries every day: 2026-09-09 (9: 3 contactable, 3 new_project, 3 stage_change), 2026-09-10 (3), **2026-09-11/today (4: 1 contactable, 2 new_project, 1 stage_change)**, max `sent_at` 13:08:33 today -- the brief is genuinely producing and logging output today. |
| Territory default (LA-office rep; Hawaii and Nevada behind a filter) | **Built 2026-09-12, branch + local DB only.** `config.yaml`'s `accounts.out_of_territory_states: [HI, NV]` (editable without a deploy, same pattern as `call_target.standards_owners`), read by a new `app.accounts.out_of_territory_states()` helper. Applied to the two genuinely ranked account views: `/accounts` (`accounts_list`, new `all_territory` query param + a checkbox in the filter form) and `app.accounts.line_account_matrix` (the per-line call list, `/line/{id}`, new `include_out_of_territory` param + a toggle link) -- both default to excluding HI/NV, both still show them on an explicit request, same escape-hatch discipline as `schools_board`'s Closed-status exclusion (WS3.4). A null `Account.state` is never treated as out-of-territory. 7 new tests. Local `accounts` table has only 1 row (the real 512-HI-customer NetSuite book isn't in this restore), so the real-world blast radius couldn't be re-confirmed here, but the mechanism is built and tested against synthetic HI/NV/CA/null-state accounts either way. |
| 11 manual corrections, 31 review candidates | Unworked |
| CAEATFA 15-SM005 production write | Pending |
| anthropic 1.x migration; session-scoped test schema (suite is 86 minutes in chunks on WSL) | Not started |
| WSL memory: .wslconfig 12GB, chunked test runs | Done Sep 10 |
| Rotate keys pasted into chat Aug 13 | Confirm |

### WS10 Research agents (new; Andrew's Sep 10-11 direction)
Constraints from the Aug 25 research, which stand:
- Writes stay single-threaded and human-gated. Agents read and propose; nothing lands on the board without review. Multi-agent failure rates in the literature run 41 to 87 percent, mostly specification and inter-agent misalignment.
- Deep-research agents hallucinate 3 to 13 percent of citation URLs. Every agent claim is retrieval-only, URL health-checked, cross-source corroborated, retrieval-date stamped, with mandatory abstention.
- Runs in a Render background worker (arq on Redis), never in the web process. Per-run and per-day budget caps. Langfuse for traces and cost. Route extraction to Haiku, narration to Sonnet, synthesis to Opus. Expected spend $15 to $60 a month at one user.

| Item | Status |
|---|---|
| 10.1 Daily source scrub is already the nightly diff plus the 6am brief. Confirm it runs and that the brief reads as "what changed" | Verify |
| 10.2 Daily research digest: for each new or changed project, one bounded research pass (who is the developer, any named team, any solicitation) that writes proposals to a review queue, not to Project | Design, then two-hour test |
| 10.3 Market and competitor research: weekly batch, not daily. Topics from Larry's list (line card expansion candidates, competitor rep holdings by territory, regulatory changes). Output is a memo with citations, reviewed before it goes anywhere | Design |
| 10.4 "Network segmentation" for target markets: meaning not yet defined. Andrew to clarify before anything is built | Clarify |
| 10.5 Evaluation harness: 50 to 100 hand-labeled cases from real captures before any agent output is trusted | Prerequisite |

---

## 5. Kill list (do not rebuild)
A/E selection mining from agendas. ESCO awards. SAM.gov as a spec source. Contractor names from LA permits. PlanetBids automated harvest. Bonfire. DIR PWC-100. SCAQMD FIND. CDE SARC facility conditions. Weather as a predictor. Nevada CSLB-equivalent. /retrofit optimization. Delivery method from entitlement documents. Data center early MEP identification. First-two-letters item family key. Name-based identity for anything NetSuite-sourced. The CEQA board as the primary project-discovery engine (kept as a feed; demoted).

---

## 6. Sequencing (re-based; day 1 = Sep 10)

**Days 1 to 14**
1. Verify the production deploy: alembic current, uq_firm_norm collisions 0, /firms and /accounts load.
2. Andrew: item master re-export; Project, Opportunity and Contact exports; House DMG meaning from Andy; ownership question in writing.
3. WS3.1 delivery-method and stage model with ABSTAIN, regression tests, branch and local only.
4. WS3.2 PDB watcher two-hour test on one UC award page.
5. WS1.5 BPELSG match once Contacts are on disk.
6. WS8: first 10 calls logged. Non-negotiable.

**Days 15 to 45**
7. WS6 line-card gap page (item master, quote conversion, OSP, Larry's priorities).
8. WS3.2 PDB watcher build if the test passed. First team award parsed into Firms.
9. WS2 `firm_pairing` seeded from the five verified public pairings and NetSuite Projects.
10. WS1.9 order-line and project tables, only after ownership is answered.
11. WS7 pilot on five lines. WS5 HCAI customer join.
12. WS10.5 evaluation harness; WS10.2 daily digest two-hour test.

**Days 46 to 90**
13. WS3.3 K-12 funding signals. WS2 K-12 architect to MEP via CPRA.
14. WS4 data center reframe (AB 802 use-type flag, NDWR source).
15. WS10.2 daily research digest in production if the harness holds; WS10.3 weekly market memo.
16. First regulatory piece published (Rule 1146.2 and heat pump boilers).
17. WS8 checkpoint: 40 or more outcomes; first honest read on whether the top of the board converts better than the middle.

---

## 7. Success at 90 days
- Ownership answered in writing.
- 40 or more logged outcomes with signal age recorded.
- Every assigned account joined to CSLB or BPELSG identity, design-build capability flagged.
- Ranked line-card gap list in Jason's hands with counts behind it.
- One PDB or P3 team award parsed into Firms before design, and one call made to that MEP because of it.
- Five manufacturer START HERE pages opened by outside reps.
- One daily research digest that Larry reads and corrects.
- One published piece under Andrew's name that a customer engineer mentions unprompted.

---

## 8. Open questions by person
- **Larry:** ownership; lifecycle table; Energy Labs and Liebert channel; FDNC; Transom spelling; Air Treatment; whether inside-sales champions want the doc template; what he wants from a daily digest.
- **Andy:** House DMG meaning; Kevin Nolan and ACS; CAMS history vs NetSuite ($62k since 2021, all House); ToroAire line cards; design teams for the seven industrial developers; who sold the LBUSD HVAC base.
- **Jason:** Opportunity export permission if role-gated; OSP renewal budget; commission rate and draw in writing; the line-card expansion criteria.
- **Andrew:** what "network segmentation" means for WS10.4; item master re-export today.

---

## 9. Block 3 (docs/MASTER-PLAN.md v3.2): redesign skeleton on public data, no DMG data, no gate

Branch, local DB (`postgresql://scout:scout@localhost:5432/scout_local`) only throughout. `docs/MASTER-PLAN.md` added and committed first (`bfb795c`, copy of the Sep 12 plan doc, supersedes v3.1 as the strategy/product spec; this file stays the execution-status log).

### Item 1: Object model and the Reason Block

**Report first, against the real local restore (442 projects, the same restore used throughout this doc):**

| Existing table | Rows | Maps to (v3.2 object) | Clean | ABSTAIN | Criterion |
|---|---|---|---|---|---|
| Project | 442 | Signal (`entitlement_milestone`) | 440 | 2 | has >=1 linked row in `project_signals` |
| Signal | 787 | Signal | 787 | 0 | has `event_date` (100%) |
| Firm | 468 | Firm | 404 | 64 | `firm_type != 'unknown'` (developer=259, consultant=82, unknown=64, mep=26, gc=20, mech_contractor=10, architect=7) |
| Account | 1 | Account | 0 | 1 | `netsuite_internal_id` present -- the one row ("Pacific Coast Mechanical Inc.") has it NULL |
| RetrofitBuilding | 60,166 | Building or Site | ~60,166 (structurally, all have APN+address) | Account-link always ABSTAIN (0), by design | model docstring: no owner data exists in the source; building identity itself is 100% clean, the separate Account-link question is ABSTAIN for every row |
| Contractor | 47,572 | Firm (reference-only -- v3.2 section 17 explicitly forbids a browsable contractor page) | 11 matched an existing Firm by `normalize_company_name` | 47,561 | normalized-name join against `Firm.name_norm`; separately, 0 matched `Account.name_norm` |
| Ab869Plan | 201 | Deadline | 201 | 0 | `plan_status` filled (100%) |
| FieldIntel | 0 | Signal (`relationship_intro`) | 0 | 0 (table empty in this restore) | -- |
| ProductLine | 70 | Line | 70 | 0 | has a matching `selection_tools` row (100%) |

**Added, additively, no renames of any live table:** `Opportunity` (`opportunities`) and `ReasonBlock` (`reason_blocks`), migration `d2e8f4a91b56`. `Opportunity`: `account_id`/`building_id` (at least one set, by application-code convention not a DB constraint), `contact_id` nullable, `signal_id` not nullable (every Opportunity traces to the one Signal that was promoted), `line_id` nullable, `stage` (`identified`/`contacted`/`engaged`/`quoted`/`won`/`lost`), `pen_holder` (reuses the existing `PenHolderRole` enum from WS3.1 rather than a new one), `next_action`, `last_touch`, `netsuite_opportunity_id` nullable and, by the plan's own words, never overwritten once set (no write path exists yet -- Block 3 is public data only -- so this is enforced today by convention and by a docstring on the one write path Block 4 will add). `ReasonBlock`: `opportunity_id`, `why_kind` (`them`/`now`/`win`, unique per opportunity via `uq_reason_block_opportunity_why`), `strength` (`Strong`/`Weak`/`ABSTAIN`), `evidence`/`source`/`source_url`/`computed_at`, plus `do_person`/`do_ask`/`one_sentence` (opportunity-level synthesis, read from any of the three rows, written to all three together).

**Migration verified rollback, actually run against local DB, not assumed from pattern:**
1. `alembic upgrade head` (`c1a4f6d2e9b0` -> `d2e8f4a91b56`) -- hit a real bug first try: `op.create_table`'s own implicit `CREATE TYPE` collided with the migration's explicit pre-create of the same enum, because `create_type=False` on a generic `sa.Enum` does not survive dialect adaptation to Postgres's native `ENUM` (confirmed by hitting `DuplicateObject: type "opportunitystage" already exists` inside a transaction that then rolled back cleanly -- `pg_type`/`\dt` showed nothing left over, ruling out an orphaned leftover). Fixed by switching every enum column to `sqlalchemy.dialects.postgresql.ENUM(..., create_type=False)` (the precedent already used by `81f4860b6471_field_intel.py` for the same reuse-an-existing-type case), which does honor the flag. Re-ran clean.
2. `\d opportunities` / `\d reason_blocks` confirm the live schema matches the model exactly, including all 5 FKs on `opportunities`, the unique constraint and both FKs on `reason_blocks`, and 9 indexes total.
3. `alembic downgrade -1` -- both tables dropped, `opportunitystage`/`whykind`/`reasonstrength` types dropped, `penholderrole` (pre-existing) confirmed untouched, `projects` row count unchanged at 442.
4. `alembic upgrade head` again -- restored to `d2e8f4a91b56 (head)` cleanly.

**Weakest-why rank:** `app/pipeline/reason_block.py::weakest_why_rank`, a pure sort-key function -- primary key is the single worst strength among the three whys (`Strong`=0, `Weak`=1, `ABSTAIN`=2, lower sorts first/stronger), secondary tiebreak is the sum of all three ranks. This directly enforces "ranks by its weakest why, never a weighted sum": any ABSTAIN always outranks (sorts after) any all-Strong-or-Weak combination regardless of the other two whys. 7 new tests (`tests/test_reason_block.py`), including the two required cases (3 Strong ranks above 2 Strong + 1 Weak; 2 Strong + 1 Weak ranks above any ABSTAIN) and a worked-example regression using the plan's own Rady Children's (Strong/Strong/ABSTAIN) vs. UC Davis Health CUP (Weak/Strong/Weak) shapes, confirming Rady's two-Strong-one-ABSTAIN does NOT outrank a plain three-Weak opportunity.

**Tests:** `tests/test_reason_block.py` (7, new, all fail against no such module by construction), `tests/test_migration_guard.py` + `tests/test_pipeline_health.py` (33, unaffected, re-run clean to confirm no model-drift breakage from the new tables).

### Item 2: Signals consolidation and the four-part filter

`app/pipeline/signals_feed.py` -- `unified_signals()` folds Project, RetrofitBuilding replacement candidates, AB 869 facilities with NPC outstanding, HCAI open mechanical projects, OPSC pre-spec rows, and FieldIntel into one read-time shape (`FeedSignal`: source, trigger_type, trigger_date, evidence, confidence-or-ABSTAIN). A view over existing tables, not a new persisted table -- no migration, `TriggerType` is a plain enum added to `app/models.py` (no table). `quiet_account` is deliberately left unbuilt (Account has 1 row in Scout today with no "last activity" concept to compute quiet from) rather than faked.

`four_part_filter()`: pure, config-driven, no LLM, reusing existing eligibility machinery rather than inventing a second one -- `app.accounts.line_offering_by_role` (the OSP register + line facets engine) for "eligible fitting line," `app.pipeline.opsc.classify_opsc_status` for the OPSC pre-spec cut. `promote_to_opportunity()` creates the Opportunity and its three ReasonBlock rows from a passed FeedSignal, filling each why from what's actually available: "them" is Strong when an account is known, Weak when only a building is known (owner ABSTAIN), ABSTAIN when neither; "now" is Strong whenever a dated reason exists (required to reach promotion at all); "win" is ABSTAIN on every single promotion this block makes, honestly, because Block 3 is public data only and no DMG pairing/relationship evidence exists yet to support a why-we-win claim.

**Report, computed against the real local restore:**

Signal count by trigger type (54,665 total):
| trigger_type | count | source |
|---|---|---|
| permit_gap | 53,252 | retrofit_building |
| public_work | 783 | hcai_project (267) + opsc_project (516) |
| entitlement_milestone | 442 | project |
| deadline | 188 | ab869_plan |
| relationship_intro | 0 | field_intel (table empty in this restore) |
| quiet_account | 0 | not built (see above) |

Four-part pass counts (out of 54,665 unified signals):
| part | pass | missing | why |
|---|---|---|---|
| named_reachable_contact | **0** | 54,665 | Contact has 5 rows total in the local restore, 0 with `reach_status='confirmed'` (phone or email populated) -- 10 `project_contacts` links exist, all to unreachable (pending) contacts. Confirms the plan's own prediction exactly: contacts are sparse. |
| sellable_account_or_building | 53,252 | 1,413 | Passes ONLY for retrofit_building-sourced signals (the only source with a building anchor Opportunity.building_id can point at, from Item 1). All 1,413 non-retrofit signals (442 project + 188 ab869 + 267 hcai + 516 opsc) fail this part -- a real, disclosed schema gap, not a bug: Project has no Account join in Scout today (the Item 1 mapping report's own Account finding), and HospitalBuilding-anchored sources have no FK target on Opportunity at all yet. |
| dated_reason | 1,411 | 53,254 | Fails for essentially all of retrofit_building (permit_gap is an absence-of-a-permit signal by definition, so it has no date) plus a small number of project/ab869/opsc rows missing a usable date. |
| eligible_fitting_line | 442 | 54,223 | Passes ONLY for project-sourced signals (the only source that carries a `Category` Scout's line card actually spans -- data_center/industrial/esco). Every other source ABSTAINs honestly rather than guessing a category to force a pass. |

**Pass all four: 0 of 54,665** -- literally zero, not merely near-zero, entirely because of `named_reachable_contact` (0 passing) intersected with the fact that the 440 signals that DO clear both `dated_reason` and `eligible_fitting_line` (the project-sourced ones) also have 0 confirmed contacts and 0 account links. Missing-part distribution: 440 signals miss exactly 2 parts (the project-sourced signals, missing only contact + account/building), the remaining 54,225 miss 3.

**Tests:** `tests/test_signals_feed.py`, 20 new tests, all fail against the old board-only path by construction (the module and every non-Project source it reads did not exist before this item) -- including one that asserts directly that `unified_signals()` returns a non-"project" source (`test_unified_signals_folds_more_than_just_the_board`).

### Item 3: Navigation and visual system

Corrected scope (superseding an earlier full-retirement plan I proposed and the user rejected): **zero content retired.** Every existing heavy page keeps its content and every existing test keeps its assertions -- each just moved to a new URL under one of the six destinations, with the old URL 307-redirecting to the new one (query string forwarded, so a filtered old link like `/retrofit?county=Orange` lands on the same filter at its new home, not a bare page -- a real bug caught by `test_territory_filter.py`/`test_line_card.py`/`test_widgets.py` failing against a first version that dropped the query string).

**Route mapping implemented** (old -> new; content byte-identical, only the path changed): `/board`->`/signals/entitlement`, `/retrofit`->`/signals/permit-gap`, `/replacement-leads`->`/signals/replacement-leads`, `/map`->`/signals/map`, `/add-signal`->`/signals/add`, `/searches`->`/signals/saved-searches`, `/contractors`->`/accounts/contractors`, `/firms`->`/accounts/firms`, `/contacts`->`/accounts/contacts`, `/watchlist`->`/accounts/watchlist`, `/hospitals`->`/deadlines/hospitals`, `/ab869`->`/deadlines/ab869`, `/review`->`/pipeline/review`, `/corrections-review`->`/pipeline/corrections`, `/outreach`->`/pipeline/outreach`, `/ask`->`/reports/ask`, `/reference`->`/settings/reference`, `/assumptions`->`/settings/assumptions`, `/lines`->`/settings/lines`, `/capture`->`/settings/capture`, `/captures`->`/settings/captures`, `/intel`->`/settings/intel`, `/health`->`/settings/health` (23 total; 5 of these -- firms/contacts/searches/add-signal/intel -- also redirect their paired POST). Untouched: `/`, `/accounts` (now the Accounts hub, plus a sub-tab strip to its 4 relocated sub-views), `/healthz`, every `{param}` detail route, `/capture/voice`, `/mcp*`.

**New hub index pages** (minimal in this item; Items 4/5 of this block flesh out Deadlines/Signals/Pipeline; Reports gets a light real KPI version now since no later item builds it further): `/pipeline`, `/signals`, `/deadlines`, `/reports`, `/settings` (operator-gated).

**Auth:** per-user HTTP Basic Auth from a config list, fixing the exact limitation `app/access_log.py`'s own docstring documented ("every visitor who is given the shared credential authenticates as the same name the admin does"). `config.yaml`'s `dashboard.users` (list of `{username, password_env}`, empty today) plus `dashboard.operator_usernames` (`["andrew"]` today); `app.access_log.configured_users()` falls back to the legacy single-user pair when `users` is empty, so the existing deploy needs no config change to keep working. `app.web.main.operator()` (403, not 401, for an already-authenticated non-operator) gates `/settings` and its sub-pages. `extract_basic_auth_username` (already reading the real username off the raw header, independent of `auth()`) needed no change -- confirmed a second real user now shows up as their own name in `AccessLog`, not as "andrew" (`test_two_configured_users_authenticate_as_distinct_usernames`).

**Nav:** real left rail (CSS flex sidebar, not the old horizontal tab bar), six always-visible destinations, Settings appended only for an operator; collapses to a horizontal bar under 640px so Today stays phone-first. The old 5-primary/19-overflow `.navmore` disclosure is gone entirely. New shared macros (`_macros.html`): `hub_subtabs`, `kpi_strip`, `card`, `row`, `empty_state` -- built on an 8-point spacing rule and exactly 3 of the existing 6 type-scale tokens (`--text-xs`/`--text-sm`/`--text-base`), applied ONLY to this new markup (the rail, the macros, the new hub pages) -- deliberately not retrofitted onto relocated/pre-existing content, which stays out of scope for a skeleton item. New `GET /search` (auth-protected, hx-get, server-rendered) matches Account and Project by name, wired into the existing Cmd-K palette as a second, live-data section alongside the existing static nav-destination filter.

**Tab count: 24 (5 primary + 19 overflow) before, 6 after (7 for an operator).**

**Route count:** 89 real routes before (no redirects existed); 95 real routes after (89 relocated-in-place + 6 new: 5 hub indexes + `/search`) plus 23 thin redirect-stub paths (28 counting the 5 paired POSTs) = 121 distinct served paths. Nothing removed.

**Content preservation, verified directly (`git diff --stat`):** 45 tracked files changed, 913 insertions / 602 deletions, every relocated template's delta is a small symmetric href/action-attribute swap, not a rewrite (largest: `base.html` at 134 lines, the real nav rewrite). 7 new untracked files (`_macros.html`, `_search_results.html`, 5 hub index templates). I spot-checked `config.yaml`, `app/access_log.py`, `app/web/main.py`'s auth/redirect code, `base.html`, `_macros.html`, and `tests/test_nav_ia.py` directly against this diff before accepting it -- all matched the specified design exactly.

**Tests:** `tests/test_web.py` (90 URL swaps, assertions unchanged), `test_line_card.py`, `test_ahj_a2l_web.py`, `test_territory_filter.py`, `test_voice_capture.py`, `test_widgets.py`, `test_access_log.py` (URL swaps only, no assertion deleted). `tests/test_nav_ia.py` fully rewritten (rail structure, operator gating both ways, per-user auth distinctness, all 23 redirects parametrized against `app.web.main._OLD_ROUTE_REDIRECTS`, `test_every_relocated_page_loads_at_its_new_path` as the direct "nothing retired" proof, and the new search endpoint).

**Verification, run independently by me (not just accepted from the implementing agent):** the 8 directly-affected test files, 251 passed, 0 failed, run in isolation first. Then the full chunked suite (same 6-way split): **2,056 passed, 0 failed, 2 deselected** -- up from Item 2's 2,010/0/2 baseline by exactly the new tests this item added, confirmed against the agent's own reported number before committing.

### Item 4: Deadlines page

`app/pipeline/deadlines.py` -- `deadlines_by_regulation()`, grouped by regulation, nearest date first within each group (None-date rows sort last, never guessed into a date). Deliberately NOT built on `app.pipeline.signals_feed.unified_signals` (Item 2): that shape is oriented around the four-part filter's six trigger types, and Deadlines needs fields (eligible line, OSP status) a generic Signal has no place for -- this groups the SAME underlying tables a different way. Every field reused from an already-computed column, nothing re-derived: AB 869 reuses `HospitalBuilding.npc_rating`/`npc_deadline_year` (same NPC-outstanding logic as Item 2's `ab869_plan` signal, independently re-verified here) plus `app.pipeline.hcai.hospital_capability_gaps` for OSP-eligible lines/status (a fact about DMG's line card, not about any one facility, so every AB 869 row shows the same pair, honestly). SB 1206 reuses `RetrofitBuilding.sb1206_trigger_status`/`sb1206_detail` (already evaluated at build time against config.yaml's regulatory triggers) against the fixed Jan 1 2030 virgin-refrigerant cutoff. EBEWE reuses the already-computed `ebewe_arcx_next_compliance_date`/`ebewe_arcx_due_this_year` (LAMC Table 9708.2 A/RCx cycle, `app.pipeline.regulatory.arcx_compliance_status`'s own output, persisted). Rule 1146.2 (boiler NOx, new to Scout -- no prior code referenced it) uses `RetrofitBuilding.equipment_type == 'boiler'`, age from `equipment_age_years` (permit-verified) or `building_age_years` (proxy), ABSTAIN explicitly when neither is known rather than guessed.

**Report, computed against the real local restore:** AB 869: 188 rows (0 ABSTAIN dates). SB 1206: 4,304 rows (0 ABSTAIN dates -- every flagged building has the fixed cutoff). EBEWE: 4,790 rows (0 ABSTAIN dates). Rule 1146.2: 33 rows, **all 33 ABSTAIN on date** -- Scout has 33 buildings identified as boilers but zero with a known age in this restore, an honest finding, not a bug (no install-year evidence exists for any of them yet). Total 9,315 rows across all four groups, rendered unpaginated on one page (3MB response) -- functionally correct and tested, but a real, disclosed scale concern for SB 1206/EBEWE specifically (4,000+ rows each) that this item does not address; pagination was not part of the item's literal ask and I did not add it unprompted.

**Tests:** `tests/test_deadlines.py`, 12 new tests covering all four regulations' inclusion/exclusion logic, the fixed SB 1206 cutoff, the Rule 1146.2 ABSTAIN-on-unknown-age rule, and nearest-date-first sorting (including a mixed known/ABSTAIN-date sort within one group). Smoke-tested the real `/deadlines` route against the local DB (200, all four regulation headers present) in addition to the pure-function tests.

### Item 5: Signals and Pipeline pages, minimal

**Signals** (`/signals`): filter chips by trigger type (real counts from Item 2's `unified_signals()`), each card with trigger/date/evidence/confidence, and a Promote button disabled with the missing parts named when `four_part_filter` fails -- reusing Item 2's function directly, nothing re-derived. Cards are capped at 200 regardless of filter (`_SIGNALS_CARD_CAP`): Master Plan v3.2 section 17's own "no browsable 53,000-row page" applies here exactly as it does to the old flat `/retrofit` list -- `permit_gap` alone is 53,252 signals in the real restore, and a card-per-signal UI does not scale to that. The full retrofit list-builder still exists unchanged at `/signals/permit-gap`.

**A real, structural finding, not just an empirical one:** Promote is not merely "near zero" or "zero today" -- it is **currently unreachable by any real signal**, by construction. `resolve_signal_id()` (new, `app/pipeline/signals_feed.py`) only returns a real `signals`-table id for `project`-sourced signals (via the existing `ProjectSignal` link); every other source (retrofit_building, ab869_plan, hcai_project, opsc_project, field_intel) has no `signals` row behind it at all -- Item 2 built their `FeedSignal` shape straight from their own table, never through `signals`. Meanwhile `sellable_account_or_building` only ever passes for `retrofit_building`-sourced signals (via `building_id`) or `field_intel` (via its own account FKs) -- never for `project`. **The intersection is empty**: the one source with a resolvable signal_id (project) never has an account/building anchor, and the only sources with an anchor never have a resolvable signal_id. This is a real Block 4 decision, not a bug to route around here -- flagged, not silently patched (candidates: give Project an account_id join, add a real `signals` row for the other five sources, or make `Opportunity.signal_id` nullable).

**Pipeline** (`/pipeline`): a table of every Opportunity (0 today, per the finding above), weakest-why first (`app.pipeline.reason_block.weakest_why_rank`/new `weakest_of()` helper -- "ranks by its weakest why, never a weighted sum"), columns account-or-building/weakest-why/line/stage/pen-holder/last-touch/next-action/NetSuite-ID-or-chip. Each row's expansion (`<details>`, the same idiom as the pre-existing collapsed-callout pattern) shows the full three-row Reason Block. No dispositions -- Block 4, per the item's own instruction.

**Report, computed against the real local restore:** 0 Opportunities exist (unchanged from Item 2's report -- nothing has been promoted, and per the finding above, nothing structurally can be through the real UI yet). `/signals` and `/pipeline` both smoke-tested 200 against the local DB. A disclosed performance gap: `unified_signals()` rebuilds its full 54,665-signal list (including a full `RetrofitBuilding` scan) on every `/signals` page load regardless of the `trigger` filter, since filtering happens in Python after construction -- observed ~3.5s per load locally; acceptable for this block's "minimal" scope but a real thing to fix before this page sees real traffic.

**Tests:** `tests/test_pipeline_and_signals_pages.py`, 13 new tests -- `weakest_of`, `resolve_signal_id`'s project-vs-non-project split, the Signals page's chips/disabled-Promote/filter behavior, `/signals/promote`'s fail-closed paths (still-failing filter -> 409, unknown source -> 400, not-found -> 404), an end-to-end promote-succeeds test (project-sourced signal manufactured by hand, since no real signal can pass today per the finding above -- proves the dispatch/re-check/promote/redirect wiring, not that real data can reach it), and the Pipeline table's weakest-why sort and Reason Block expansion.

---

## 10. Block 4A (Master Plan v3.6 Parts VI-VIII): the loop, the notes, the snapshots

`docs/MASTER-PLAN.md` updated to v3.6 -- adds Part VI (Decision Layer), Part VII (Reporting and Design), Part VIII (Per-Rep Experience), none of which existed in v3.2, plus `pen_state` (not_moved/moving/moved/ABSTAIN) on every Signal and Opportunity, gating the four-part filter's dated-reason part -- new in v3.6, so Block 3 was not incomplete against what it actually had to read at the time (confirmed: `pen_state` does not appear anywhere in the v3.2 copy committed during Block 3).

### Item 0: test speed

**Measured the actual bottleneck before changing anything**, per instruction: `tests/conftest.py`'s `db_session` fixture creates a fresh file-backed SQLite database per test via `SQLModel.metadata.create_all()` (73 tables). Timed directly: **6-14 seconds per `create_all()` call**, with heavy variance, on this machine -- SQLite's default `synchronous=FULL`/journal-mode behavior fsyncs after each of the ~300+ CREATE TABLE/CREATE INDEX statements, and WSL2's virtualized disk I/O makes each fsync round-trip expensive. With roughly 2,000 tests each paying this cost once, this alone plausibly accounts for the entire 40-90 minute chunked runtime -- confirmed by fixing it and re-measuring, not assumed.

**Fix:** two SQLite PRAGMAs (`synchronous=OFF`, `journal_mode=MEMORY`) attached via a `connect` event listener in `app.db.get_engine()`, gated to the `sqlite` branch only (production always uses postgresql, per `database_url()` -- zero production behavior change). Chosen over both alternatives the item suggested (session-scoped schema + per-test transaction rollback, or a template database) because it requires **zero structural change** to the fixture or to any test: every test still gets its own fresh, fully isolated, real on-disk file and its own real connection, exactly as before -- durability (fsync) is the only thing being skipped, and a throwaway per-test file has no durability requirement. This also automatically covers `tests/test_migration_guard.py`'s three tests that reset `app.db._engine` and call `init_db()` directly, bypassing the `db_session` fixture, since the fix lives in the one shared `get_engine()` function both paths call. **Zero test files touched** -- confirmed via `git diff --stat`: only `app/db.py` changed.

**Report:**
| | Before | After |
|---|---|---|
| `create_all()` (73 tables, single call, measured directly) | 6-14s | 0.05-0.07s |
| One real chunk (`chunk_00`, 421 tests, re-run identically) | 53:39 (3,219.66s) | 0:46 (46.42s) -- **69x** |
| Full suite, single process, no chunking needed at all | 40-90 min chunked across 6 parallel workers (this session's own baseline throughout Blocks 3-4A) | **2:56 (176.61s)**, one `pytest tests/` invocation |
| Result | -- | 2,081 passed, 0 failed, 2 deselected (identical to the pre-fix baseline -- same test count, same pass count) |

**Target was under 15 minutes; actual is under 3, with no parallelization.** Chunking is no longer necessary for this suite's runtime and I stopped using it for the rest of Block 4A's items as a result -- a single `pytest tests/ -q` run now serves the same "chunked suite" verification purpose in a fraction of the time.

### Item 1: Opportunity anchors

Schema (3 migrations, verified rollback on the two reversible ones, no-op-by-design downgrade on the enum-value addition matching this repo's own established precedent -- `a77ddb56a5a1`/`d3e6a9c42b57`, Postgres has no `DROP VALUE`):
- `067c0ef15e70`: new `PenState` enum (not_moved/moving/moved/ABSTAIN) plus a `pen_state` column, default ABSTAIN, on both `signals` and `opportunities` -- Master Plan v3.6 section 12b: "every Signal and Opportunity carries pen_state."
- `aebb7f6bd153`: `opportunities.facility_perm_id`, nullable FK to `ab869_plans.perm_id` -- the third anchor: "an Opportunity may anchor on an Account, a Building, or a Deadline facility."
- `7ad64088810f`: two new `SignalType` values (`retrofit_permit_gap`, `ab869_npc_deadline`) marking a Signal row as synthesized at promotion time rather than extraction-pipeline output.

**pen_state computed per source, from real evidence only, never guessed:**
| Source | Basis | Result |
|---|---|---|
| project | `Project.stage` (concept/entitlement/design -> not_moved: pre-Division-23; permitting -> moving; procurement/construction -> moved) | 153 not_moved, 190 moving, 8 moved, 91 ABSTAIN |
| retrofit_building | population's own definition -- "no permit on record" IS the not_moved evidence | 53,252 not_moved (100%) |
| ab869_plan | plan_status literal match to the plan's own rule ("Not Approved with no contractor named is early") -- `Not Approved`/`Not Submitted` -> not_moved, everything else ABSTAIN (no contractor field exists anywhere in Scout to confirm the rule's "late" half, so `moved`/`moving` are never set for this source) | 124 not_moved, 64 ABSTAIN |
| hcai_project | `HcaiProject.stage` (plan_review -> not_moved, pending_start -> moving, in_construction -> moved) | 75 not_moved, 105 moving, 87 moved |
| opsc_project | `app.pipeline.opsc`'s own existing "engineer, spec not locked" classification, reused directly | 516 not_moved (100%) |
| field_intel | no structured basis | ABSTAIN (0 rows in this restore) |

**The four-part filter's "sellable account or building" part** now also passes on a building or facility anchor for replacement-clock work, gated on `pen_state in (not_moved, moving)` -- a building/facility whose pen has already moved (or whose pen_state is unknown) is not sellable just because Scout knows where it is (section 12b: "the window closes when a contractor with an incumbent brand relationship is on site").

**Promote now has a real path for building/facility-anchored signals:** new `ensure_signal_for_promotion()` resolves an existing Signal (project-sourced) or **creates one** for retrofit_building/ab869_plan sources at the moment of promotion -- "a Signal row is created for any building or facility the moment it is promoted." `four_part_filter()`/`unified_signals()` themselves stay pure reads with zero side effects (signal creation happens only inside the actual promote action, never while someone is just browsing `/signals`). New `can_promote_signal()` names the fifth, still-real gate: hcai_project/opsc_project/field_intel still have no path to a real Signal row at all -- out of this item's scope, which named only "buildings and deadlines."

**Report, computed against the real local restore (54,665 unified signals):**
| Part | Pass | Missing | Change from Item 2 |
|---|---|---|---|
| 1. named_reachable_contact | **0** | 54,665 | Unchanged -- stays at zero until the Contact export lands, exactly as this item said it would |
| 2. sellable_account_or_building | **53,376** | 1,289 | **+124** (exactly the 124 new ab869_plan/not_moved facility anchors) |
| 3. dated_reason | 1,411 | 53,254 | Unchanged -- this item didn't touch it |
| 4. eligible_fitting_line | 442 | 54,223 | Unchanged -- this item didn't touch it |

**Pass parts 2, 3 and 4 together: still 0** -- a real, disclosed follow-on finding, not a bug: the only signals that ever pass part 4 (eligible_fitting_line) are project-sourced (442, needs a real `Category`), and project-sourced signals never pass part 2 (Project has no Account join in Scout, unchanged from Item 1/2's own findings); the only signals that now pass part 2 via a building/facility anchor (retrofit_building, ab869_plan) never pass part 4 (neither source carries a `Category` for `line_offering_by_role` to evaluate). Fixing part 2 for replacement-clock work did not, by itself, unblock any real Opportunity -- part 4's eligible-line logic would need its own building/facility-aware extension (not scoped to this item) before that changes. **Pass all four: still 0**, entirely on part 1, exactly as predicted.

**Tests:** `tests/test_opportunity_anchors.py`, 14 new tests (pen_state computation per source, the new sellable_account_or_building gate including the ABSTAIN and moved rejection cases, `can_promote_signal`, `ensure_signal_for_promotion` creating real rows for retrofit_building/ab869_plan and reusing an existing one for project, and one true end-to-end promotion onto a building anchor with a real, newly-created Signal row). `tests/test_signals_feed.py`'s pre-existing building-anchor test updated (not deleted) to reflect the new pen_state requirement -- the old assertion described real behavior that legitimately changed, not a regression.

### Item 2: Outcomes

New `Outcome` table (`app/models.py`: `Disposition` -- 8 values, the 6 named call-attempt dispositions plus `won`/`lost`; `LostReasonCode` -- 8 values; `OutcomeSource` -- web/capture), migration `3a3f9fc8ba41`, verified rollback. `app/pipeline/outcomes.py::log_outcome()` is the one writer (same "exactly one write path" discipline as `app.outreach.log_outreach`): validates `lost` requires `reason_code` and `reason_code='lost_to_competitor'` requires a named `competitor` (section 15's own words, both enforced here not left to callers), and syncs `Opportunity.stage`/`last_touch` so the two tables can never silently disagree (`won`/`lost` dispositions move `Opportunity.stage` to match; every disposition updates `last_touch`). Config-driven lists: `config.yaml`'s `outcomes.dispositions`/`outcomes.reason_codes` give label/display-order, read by `app.pipeline.outcomes.dispositions()`/`reason_codes()` -- the valid VALUE SET itself stays the closed, migration-defined Postgres enum (the plan's own "at most eight" is a fixed constraint, not something a config edit should widen).

**Wired into three entry points, one writer underneath all three:**
- **Pipeline** (`/pipeline/{opportunity_id}/outcome`, new route): one-tap buttons for the 6 call dispositions plus a `<details>`-disclosed Lost form (reason_code required dropdown, competitor field). New "Disposition" column on the Pipeline table.
- **`/capture`**: `capture_confirm` gained optional `opportunity_id`/`disposition`/`reason_code`/`competitor` fields -- when both `opportunity_id` and `disposition` are given, also logs an Outcome (`source=capture`) alongside the always-required Outreach row. **A real bug caught and fixed while wiring this**: `log_outreach()` commits internally, so calling it before validating the Outcome left a real, un-rollback-able partial write (Outreach persisted, Outcome/queue-status silently dropped) whenever the Outcome validation failed after. Fixed by validating/writing the Outcome FIRST (flush-only) so a bad `reason_code` 400s before anything commits at all -- confirmed atomic with a dedicated test.
- **`log_outreach` MCP tool**: now takes `project_id`, `account_id`, OR `opportunity_id` (exactly one) -- given `opportunity_id`, requires `disposition` and `user` (the MCP layer has no per-user session to read a username from, so the caller must state it) and writes an Outcome instead of an Outreach row, through the same `log_outcome()`.
- **Today**: the item's other named entry point is not wired yet -- Today's call cards are still Project-scored (old board logic), not Opportunity-scored, so there is nothing for a Today-side disposition button to point at yet. Lands with Item 5 (Today re-sourced from Pipeline), noted explicitly rather than built against a page that's about to be replaced.

**Tests:** 25 new -- `tests/test_outcomes.py` (12: every validation rule, stage/last_touch sync, config-driven lists), `tests/test_mcp_tools.py` (5: opportunity_id path writes an Outcome, requires disposition/user, lost-without-reason-code rejected, three-way exactly-one), `tests/test_voice_capture.py` (3: confirm-with-opportunity logs an Outcome alongside Outreach, confirm-without-opportunity logs nothing extra, lost-without-reason-code 400s atomically -- the bug-fix regression test), `tests/test_pipeline_and_signals_pages.py` (5: buttons render, connected updates last_touch not stage, won/lost sync stage, lost-without-reason-code 400s).

**Full suite: 2123 passed, 0 failed, 2 deselected** (up from Item 1's 2098/0/2 baseline by exactly the 25 new tests).
