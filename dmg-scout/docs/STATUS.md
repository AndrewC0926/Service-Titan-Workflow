# DMG Scout — Status

*Plain-language summary. September 2026.*

## 1. What Scout has today

- **Project board** — the main list of new data-center and industrial construction leads, ranked and updated every night; this is the core product.
- **Retrofit list (AB 802)** — 50,259 California building energy filings (statewide file, live since Sept 7, 2026); flags owners whose old HVAC equipment is likely due for replacement.
- **Schools tab (OPSC)** — 14,506 state school-facility-funding records (statewide file, live since Sept 7, 2026); shows which districts have money and a project moving.
- **Hospitals (AB 869 + HCAI)** — 201 hospitals with seismic upgrade deadlines (since Sept 2) plus 45,132 hospital project records across our 7 counties (since Sept 8); flags hospitals with an open mechanical project right now.
- **Air-permit flag** — 8,644 South Coast air district facility records (since Sept 8); shows whether a building already has an air permit on file, a clue there's real HVAC equipment there.
- **BPELSG roster** — 5,347 licensed mechanical engineers in our 7 counties (since Sept 8); lets a rep check if a project's named engineer is really a licensed mechanical engineer.
- **AHJ A2L guidance register** — 212 city, county, and state building departments checked for new-refrigerant rules (built Sept 10–11); shows which cities already have written rules for the new A2L refrigerants going into HVAC equipment.
- **Nightly diff** — watches all 118,742 rows across the five lists above every night and automatically flags anything new, changed, or gone missing.

## 2. What is dead, and why

- **DIR PWC-100** (state public-works contractor records) — the state's own site blocks all automated access; only a public records request gets in.
- **SCAQMD FIND** (equipment/permit lookup) — same, blocks all automated access.
- **LA City permits** — the three LADBS open datasets carry no contractor, applicant, or licensee field (column metadata checked).
- **San Diego building permits** — the one name field on every permit mixes contractor, owner, and permit-expediter names with no way to tell them apart.
- **LBUSD bond program site** — the real project list is nearly empty, and the actual construction history lives on a page the site itself blocks.
- **SAN Airport construction list** — real but tiny (23 projects), and hasn't been updated since January despite being called a monthly list.
- **CARB refrigerant registry (R3)** — covers supermarkets and cold storage only; comfort-cooling air conditioning is explicitly exempt, so it can't be an HVAC list.
- **City/county facility master plans** — only 20 of 295 school districts and cities were even checked; most have nothing public.
- **PlanetBids** — terms of use (sections 3.4 and 6.2) bar automated reuse; bid documents only via agency websites or a special-use request.
- **Riverside County, City of Riverside, Kern County** — all three block Scout's software by name; never worked around.

## 3. Waiting on a person

- **Public records requests filed, pending:** HCAI, DIR, SCAQMD — the last path left for a few blocked sources.
- **Larry:** a read-only NetSuite login and the six saved searches to run in it, the current account list, a CoStar-vs-Reonomy decision, an introduction to the VRF team, whether Scout or the CRM owns the deal-lifecycle stages, and who owns this tool going forward.
- **Andy:** the developer design-team list, and who actually sold the HVAC on LBUSD's Measure E work.
- **Paul:** the LG installed-base report.

## 4. What Scout cannot do

- Name the engineer on private work — that name almost never appears in public records before a building is built.
- Name the real owner behind an LLC — that takes a paid lookup service Scout doesn't have yet.
- Tell a rep about a project before it has a design to bid on — there's nothing to find before that point exists.

## 5. The finding for leadership

The free, public data layer is exhausted — four candidate sources died this week alone, the moment someone actually opened the file. Everything real that's left costs paid data (roughly $1,000–$1,600 a month), a favor inside DMG's own systems, or a public records request that takes weeks.

## 6. Open risk

Scout runs entirely on Andrew's own computer, accounts, and logins — if he's unavailable, it stops.
