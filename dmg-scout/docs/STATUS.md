# DMG Scout: Status

*Plain-language summary. September 2026.*

## 1. What Scout has today

- **Project board:** about 440 projects, the main list of new data-center and industrial construction leads, ranked and updated every night; this is the core product.
- **Retrofit list:** 53,252 LA County commercial and industrial buildings likely past HVAC service life (county assessor records), plus AB 802 energy filings for 14,426 buildings in our seven counties, 9,105 of them ranked (live since Sept 7).
- **Schools tab (OPSC):** 883 in-territory school funding applications and 183 recent signals from the state school facility program (statewide file loaded Sept 7).
- **Hospitals (AB 869 + HCAI):** 2,505 hospital buildings in our seven counties with seismic deadlines, 201 filed compliance plans (since Sept 2), plus 45,132 hospital project filings (since Sept 8), 269 of them open mechanical work right now.
- **Air-permit flag:** 8,644 South Coast air district facility records (since Sept 8), showing whether a building already has an air permit on file, a clue there's real HVAC equipment there.
- **BPELSG roster:** 5,347 licensed mechanical engineers in our 7 counties (since Sept 8), letting a rep check if a project's named engineer is really a licensed mechanical engineer.
- **AHJ A2L guidance register:** 212 jurisdictions on the list, 165 checked so far, 47 Los Angeles County cities still to go; five have written A2L rules, and only LADBS has a full guideline.
- **Nightly diff:** watches all 118,742 rows across the five lists above every night and automatically flags anything new, changed, or gone missing.

## 2. What is dead, and why

- **DIR PWC-100** (state public-works contractor records), the state's own site blocks all automated access; only a public records request gets in.
- **SCAQMD FIND** (equipment/permit lookup), same, blocks all automated access.
- **LA City permits,** the three LADBS open datasets carry no contractor, applicant, or licensee field (column metadata checked).
- **San Diego building permits,** the one name field on every permit mixes contractor, owner, and permit-expediter names with no way to tell them apart.
- **LBUSD bond program site,** the real project list is nearly empty, and the actual construction history lives on a page the site itself blocks.
- **SAN Airport construction list,** real but tiny (23 projects), and hasn't been updated since January despite being called a monthly list.
- **CARB refrigerant registry (R3),** covers supermarkets and cold storage only; comfort-cooling air conditioning is explicitly exempt, so it can't be an HVAC list.
- **City/county facility master plans:** the documents are inconsistent, manual to collect, and several counties block automated access; deferred, not dead.
- **PlanetBids,** terms of use (sections 3.4 and 6.2) bar automated reuse; bid documents only via agency websites or a special-use request.
- **Riverside County, City of Riverside, Kern County,** all three block Scout's software by name; never worked around.

## 3. Waiting on a person

- **Public records requests filed, pending:** HCAI, DIR, SCAQMD, the last path left for a few blocked sources.
- **Larry:** a read-only NetSuite login and the six saved searches to run in it, the current account list, a CoStar-vs-Reonomy decision, an introduction to the VRF team, whether Scout or the CRM owns the deal-lifecycle stages, and who owns this tool going forward.
- **Andy:** the developer design-team list, and who actually sold the HVAC on LBUSD's Measure E work.
- **Paul:** the LG installed-base report.

## 4. What Scout cannot do

- Name the engineer on private work, that name almost never appears in public records before a building is built.
- Name the real owner behind an LLC, that takes a paid lookup service Scout doesn't have yet.
- Tell a rep about a project before it has a design to bid on, there's nothing to find before that point exists.

## 5. The finding for leadership

The free, public data layer is exhausted. Four candidate sources died this week alone, the moment someone actually opened the file. Everything real that's left costs paid data (roughly $1,000–$1,600 a month), access to DMG's own systems (NetSuite history, AAON and LG job registrations), or a public records request that takes weeks.

A backtest against real NetSuite order history (2026-09-10, `docs/BACKTEST-2026-09-10.md`) found 0 of DMG's 1,359 biggest ($100k+) sold equipment jobs match a Scout-board project — not a matching bug, but confirmation that Scout's new-construction leads and DMG's sold history (dominated by hospital/school/repair work) are different populations today.

## 6. Open risk

Scout runs on cloud services and accounts Andrew pays for personally; DMG has no access to the code, the database, or the logins today.
