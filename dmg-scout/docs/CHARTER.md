# DMG Scout — Architecture and Execution Charter

Version 1.0, August 5 2026. This is the document Claude Code works from. It defines the vision, the architecture, the invariants that make autonomous execution safe, what may be done without asking, and where work must stop for a human decision.

---

## 1. The vision

**One sentence:** see commercial mechanical opportunities 18 to 24 months before anyone else in the territory, and hand the rep a person to call about each one.

Two words matter. **Early**, because DMG wins roughly 80 percent of jobs where it is basis of design and loses roughly 80 percent where it is not, and basis of design locks during design development, long before anything reaches a bid desk. **Callable**, because a project with no human attached is reading, not selling.

The success criterion is deliberately not "find the named mechanical engineer of record." Three independent measurements proved that name does not exist in public records before design development: zero across 58 projects on the original ladder, two mechanical design awards in 37,651 Legistar matters over 24 months, and zero MEP subconsultants in 6.5 million characters of full CivicPlus staff reports. The mechanism is structural — the owner contracts a prime, and the prime selects its MEP sub privately.

**The criterion is: a project, early, plus at least one human with a phone or email who can tell the rep who is designing it.** Vernon is the reference implementation: 99 MW, LA County, parcels, defensible tonnage from stated megawatts, a CEC project manager with a direct line, and the developer's regional director with a mobile and email.

---

## 2. What we are building

Three products sharing one machine.

**A. New construction discovery.** Public filings (CEQAnet, CEC, Nevada GOED, county agendas) surfacing projects at entitlement and permitting, 12 to 36 months before mechanical bid. Two boards: data center and industrial. This exists and works.

**B. Replacement and retrofit.** Building permit history against ASHRAE service life, LA energy benchmarking, and assessor parcel data, identifying buildings whose equipment is at end of life. This is 55 to 62 percent of the HVAC equipment market, larger than new construction and growing faster, and its decision maker is the owner or property manager, who appears in public assessor records. Does not exist yet.

**C. The contact layer.** Every row carries a human with a contact method, ranked by reachability. Partially built.

---

## 3. Architecture

**Pipeline A (unstructured):** fetch → dedupe → triage → extract → ground → resolve → size → score → notify. Documents are PDFs and agenda packets, so this stage is LLM-heavy and needs section-aware chunking, unit grounding, and entity resolution. This is what exists.

**Pipeline B (structured):** ingest → normalize → join → compute → score. Permits, parcels and benchmarking arrive as JSON from Socrata APIs. **This pipeline must be deterministic SQL. Do not use the LLM where a join will do.** It is cheaper and it removes fabrication risk entirely.

The two pipelines share the territory config, the contact model, the scoring shell, the dashboard, and the deliverables generator. They share nothing else.

---

## 4. Invariants

These are the lessons of twelve silent failures, turned into automated checks. **Every one runs after the relevant stage and fails loudly.** They are the reason autonomous execution is safe.

1. **No source reports OK while producing nothing.** A source whose documents average below the stub floor, or whose sub-targets majority-fail, reports FAIL and exits non-zero.
2. **No project carries a score with zero linked signals.** Surfaced and failed, not rendered.
3. **No duplicate projects.** Any two projects sharing an SCH number, or a normalized name plus county, are reported after every resolve.
4. **No unparseable field votes against a match.** A field that cannot be parsed into comparable values abstains from the similarity score. It never contributes zero.
5. **No extracted number survives ungrounded.** Every unit-bearing value must be found in the source text with its unit. Rejections null the field and record the reason.
6. **No join hides its miss rate.** Every join reports match rate and unmatched count. Unmatched rows are counted, never silently dropped.
7. **No stage is judged on a partial read without saying so.** Triage and extraction record coverage; anything judged on less than full text is flagged.
8. **No estimate is trusted that has not been validated against actuals.** The estimator must measure the real pending corpus, not a hardcoded assumption.
9. **Commit per document, never per chunk.** One bad record costs one record.
10. **robots.txt is absolute.** A disallowed host is not fetched. The user-agent is never altered to evade a block. A blocked source ships disabled with the finding recorded.
11. **Every number on a deliverable traces to a public URL.**
12. **Null over inference, always.** A missed field is acceptable. An invented one is a system failure.
13. **Every regulatory-trigger entry carries a verification status and a check date; anything not `verified` is EXCLUDED from customer-facing output, never caveated.** A caveated wrong date is still a wrong date in front of a customer — the trigger table's whole value is that it can be quoted cold, so exclusion is the only safety mechanism that actually protects that. See config.yaml's `regulatory_triggers` block and app/pipeline/regulatory.py's `customer_facing_triggers()`.

---

## 5. Autonomy rules

**Proceed without asking:**
- Writing, refactoring and testing code
- Running any pipeline stage within the approved daily budget
- Fetching (LLM-free) from approved sources
- Fixing any bug found, including ones not on the plan, with a test
- Investigating a suspicious number before reporting it

**Stop and ask:**
- LLM spend beyond the daily cap, or any single run projected above $5
- Any destructive operation: purge, reset, force, delete
- Adding a data source not named in the plan
- Any scope not in the current phase
- **A measurement that contradicts the plan.** This is the important one. If a gate metric comes back materially worse than expected, report it and stop. Do not design a workaround.

**Never:**
- Alter the user-agent to evade a block
- Fetch from a disallowed host
- Ship a number that has not been grounded
- Mark a gate passed on partial data
- **Apply a schema migration while a pipeline stage is running.** `ALTER TABLE` takes an exclusive lock, and a pending exclusive lock in Postgres blocks new readers too — so the migration queues behind the running stage's open transaction, and every subsequent query queues behind the migration. The stage wedges, and so does everything after it. Wait for the stage to exit.

---

## 6. Phase plan and gates

Each phase ends with a measurement. **Report it and stop. Do not begin the next phase.**

**Phase A — Resolve integrity and contactability.** In flight.
Fix the SCH key. Make APN abstain rather than vote against. Re-resolve. Add the duplicate check as a standing invariant.
*Gate:* contactability reported per project row and per distinct project after duplicates collapse, split by board, plus the resolve disposition split. Ten sampled projects with source documents.

**Phase B — Ranking and known defects.**
Reorder the ladder by reachability first, then proximity to the spec decision. Root-cause the orphaned projects (ids 423-428) and add invariant 2. Fix the estimator against validated constants and model adjudication. Audit the similarity function for other fields with the APN failure mode.
*Gate:* top 20 rows before and after; estimator within 10 percent of a measured actual.

**Phase C — Replacement module.** The larger market.
LA and San Diego only. Report data availability with sample records **before** writing ingestion. Then permits, EBEWE, San Diego, assessor parcels, the address join, the building record, the replacement signal, scoring, and contractor routing.
*Gate:* at least 50 LA buildings ranked with owner names, estimated tonnage, and a stated reason each is in a replacement window, plus reported join match rates.

**Phase D — Ship.**
Deploy to Render with cron. First notify run. Stage age on every row, with anything over 12 months flagged unverified. Deliverables for all three boards.
*Gate:* a digest arrives on a morning nobody did anything, containing a real project and a real person.

**Phase E — Validate against reality.** Requires DMG data, so it waits until after August 31.
Reconstruct 15 to 20 recent wins and losses from DMG's history. At what stage did DMG first learn of each, and would a signal here have surfaced it earlier? If fewer than a third could have been caught earlier, say so plainly.

---

## 7. Out of scope, permanently

A CRM (RepFabric and ROM exist). Quoting or selection software (factories provide it). Energy modeling (TRACE, HAP, CBECC-Com own it). A/E selection extraction (measured and killed three ways). Accela scrapers. Anything touching BoardDocs, Granicus, PrimeGov or Simbli. LinkedIn scraping. The one-document-to-many-signals schema change, until a gate demands it.

**ESCO / ESPC as a channel** (measured and killed). Detection was never the problem — two live hits both correctly rejected as not-genuine in testing. Volume was: a title-only, no-LLM scan of everything Legistar and CivicPlus cover, 24 months back, using the shipped `_esco_match` word-boundary matcher, verified against synthetic positives before trusting a null result. 19,173 Legistar matter titles across six tenants (Fontana, San Bernardino Co, Riverside Co, LA Co, Clark Co, Washoe Co) and 98 CivicPlus agenda packets (Storey County, Planning Commission + Board of Commissioners) — zero hits, either platform.

Caveat that matters more than the number: six counties plus Storey is not the MUSH market ESPCs actually get awarded in. School districts run on BoardDocs, already out of scope and closed to us; universities and hospitals aren't in the tenant list at all. The null says we cannot see ESPC awards from here, not that they don't happen. Worth one more look if school district agendas ever become reachable. Until then: the `esco` keyword category, its detection code, and its tests stay in the repo as evidence it was tried — nothing gets built on top of it.

---

## 7a. Watch items — not built, not adopted, tracked so it isn't re-litigated from zero

**"2030 gas furnace ban."** Neither the statewide CARB zero-emission space/water heater rulemaking (Board vote delayed past 2025, not yet adopted as of Aug 2026) nor the LA-region SCAQMD rule targeting the same outcome (Proposed Amended Rules 1111/1121) is enacted law — PAR 1111/1121 was REJECTED 7-5 by the SCAQMD Governing Board in June 2025. Do not add this to `regulatory_triggers` in config.yaml as an enacted deadline. Worth re-checking if either rulemaking closes: CARB program page (https://ww2.arb.ca.gov/our-work/programs/building-decarbonization/zero-emission-space-and-water-heater-standards) and SCAQMD's PAR 1111/1121 status page (https://www.aqmd.gov/home/rules-compliance/rules/scaqmd-rule-book/proposed-rules/rule-1111-and-rule-1121).

---

## 8. Definition of done

Every morning, without anyone touching it, a digest arrives containing new and materially changed projects only, each with an estimated tonnage traceable to a public record, a stage with a date attached, and at least one human with a phone or email — or an explicit flag that there is nobody to call.

Anything beyond that is optional.
