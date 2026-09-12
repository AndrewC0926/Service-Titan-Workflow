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
