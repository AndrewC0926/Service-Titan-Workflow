# Line-card gap draft — item master reports

Report only. Read-only against the three real NetSuite CSV exports in
`~/netsuite-exports/` (`ScoutSalesOrderLinesResults612.csv`,
`ScoutQuoteLinesResults462.csv`, `ACItemMasterResults493.csv`, added to disk
2026-09-11 -- Block 1's note that no item master existed is now stale). No
schema change, no database write, no production write. All figures below
were computed directly from these files by a one-off script, not from the
local Postgres DB (which has 0 rows in `accounts` and no item/order tables
at all).

## 0. Item master, checked directly

542,610 rows, matching the instruction's own figure exactly. `Preferred
Vendor` (column 9) filled on 530,370 rows (**97.74%**), blank on 12,240
(2.26%) -- matches the stated 97.7%. `Manufacturer` (column 10) reads
"Needs Update" on 324,433 rows -- matches the stated ~324k; ignored per
instruction (Manufacturer is not the family key, Preferred Vendor is).

180,548 distinct item numbers (the join key, `Name` column) across 542,610
rows -- 149,132 of those item numbers appear on more than one row. Checked
whether the duplicates ever disagree on Preferred Vendor before treating
first-seen as authoritative: **9 of 180,548 (0.005%) have a genuine
Preferred-Vendor conflict across duplicate rows** (e.g. item
`1117392-3B572` shows both "917 SHARPE HEATING & VENTILATING" and "924 WEG
ELECTRIC MOTORS CORP."). Negligible; first-seen-wins was used for the join,
these 9 items are not separately flagged in the numbers below.

## 1. Vendor normalization

Every one of the 543 distinct raw `Preferred Vendor` values carries a
leading NetSuite vendor-record-id number ("1709 NAILOR INDUSTRIES INC") --
confirmed zero exceptions. A second, parallel naming pattern also recurs
throughout the file: an "R "-prefixed variant of the same vendor name ("R
DANFOSS LLC" alongside "DANFOSS INC", "R NAILOR INDUSTRIES INC." alongside
"NAILOR INDUSTRIES INC") -- cross-checked by name and by which item rows
carry each, confirmed to be the same manufacturer, not a different entity.

**Cleaning rule, applied in order:** strip the leading digit-prefix; strip
a leading "R " token if present; strip exactly one trailing corporate
suffix (INC, INC., CORP, CORPORATION, CO, CO., COMPANY, LLC, LTD) **only
when preceded by a space or comma** -- a boundary-free version of this
rule was tried first and silently corrupted real names by matching "CO"
inside a single word (`TACO INC` -> `TA`, `MAPCO` -> `MAP`, `INDEECO` ->
`INDEE`, `HEATCO INC` -> `HEAT`, `SAFE AIR-DOWCO` -> `SAFE AIR-DOW`) --
caught by manually inspecting the cleaned output against the real 543-value
list before trusting it, not by assumption. After the fix, 543 raw values
collapse to 434 distinct cleaned strings with a mechanical rule alone.

**The explicit allowlist** (config.yaml's `line_card.vendor_normalization`,
102 canonical vendors, editable without a deploy) collapses known variants
of the same manufacturer on top of that -- the four Nailor rows, four AAON
rows, and eleven LG Electronics rows the instruction named, plus about 90
more identified the same way (cross-referencing name similarity against
real row volumes, never guessed). A cleaned string not in the allowlist is
**ABSTAIN**, never inferred.

### Unmapped list

116 distinct cleaned vendor strings (of 434) are unmapped -- ABSTAIN.
Combined **$15,137,878.37** in Equipment Sales dollars since 2021 (1.88% of
the $805.3M total in section 2), across 2,825 order lines. Top 15 by
dollars (all real, all checked -- none obviously a missed alias of an
already-mapped manufacturer):

| Cleaned vendor | Lines | Dollars |
|---|---:|---:|
| PROTECALL CA | 1,134 | $5,800,313.34 |
| DEHUMIDIFIED AIR SOLUTIONS | 7 | $1,738,330.00 |
| WILSENERGY | 157 | $1,736,126.79 |
| COILMASTER | 39 | $648,732.59 |
| DYKMAN ELECTRICAL | 23 | $606,229.35 |
| TRANSOM | 14 | $505,731.65 |
| AMERICAN WARMING | 11 | $467,775.00 |
| RM MANIFOLD GROUP | 53 | $406,100.17 |
| RAHN INDUSTRIES | 35 | $363,659.93 |
| JOHNSTONE SUPPLY | 297 | $228,404.22 |
| UNITED COOLAIR | 4 | $193,865.01 |
| BLUE DIAMOND PUMPS | 15 | $165,443.98 |
| ADVANCED COIL TECHNOLOGY | 2 | $158,915.00 |
| ULTRA PURE SYSTEMS | 14 | $155,123.00 |
| PROVENT | 9 | $143,630.00 |

The remaining 101 unmapped strings total under $1.7M combined -- genuine
long tail, not a hidden concentration. PROTECALL CA (largest, $5.8M) reads
as a service/installation vendor, not an equipment manufacturer, on its
name alone -- plausible it never belonged in a "manufacturer" list, not
investigated further here.

## 2. (a) Equipment Sales dollars since 2021, by normalized vendor

**Join hit rate:** of 98,910 Equipment-Sales order lines dated 2021-01-01
or later (out of 152,988 total order lines, all-time, all classes),
**97,097 (98.17%) had their `Item` value found in the item master**;
1,813 (1.83%, $4,783,522.94) did not join at all. Of the 97,097 joined
lines: 82,420 got a mapped vendor, 11,852 had a blank Preferred Vendor on
their item master row, 2,825 were an unmapped vendor string (section 1).

**Total Equipment Sales dollars since 2021: $805,298,368.28.**

ABSTAIN (not-found + blank + unmapped, combined): **$53,270,937.06
(6.615%)** -- not dominant, not hidden.

| Rank | Vendor | Dollars | Share |
|---:|---|---:|---:|
| 1 | Energy Labs | $162,988,469.21 | 20.240% |
| 2 | LG Electronics | $150,437,210.67 | 18.681% |
| 3 | AAON | $149,092,679.29 | 18.514% |
| — | **ABSTAIN** | **$53,270,937.06** | **6.615%** |
| 4 | SPX Cooling Technologies | $48,276,401.78 | 5.995% |
| 5 | Vertiv | $24,198,625.97 | 3.005% |
| 6 | ClimateMaster | $22,037,780.03 | 2.737% |
| 7 | Strobic Air | $19,738,348.07 | 2.451% |
| 8 | International Environmental (IEC) | $16,854,059.01 | 2.093% |
| 9 | Vibro Acoustics | $14,210,010.13 | 1.765% |
| 10 | BASX | $12,856,916.00 | 1.597% |
| 11 | Scott Springfield Manufacturing | $12,704,270.61 | 1.578% |
| 12 | HVAC-R International | $12,504,061.60 | 1.553% |
| 13 | EBTRON | $11,226,834.67 | 1.394% |
| 14 | Dunham-Bush | $9,367,827.68 | 1.163% |
| 15 | Islandaire | $7,238,209.90 | 0.899% |
| 16 | Neptronic | $7,204,419.26 | 0.895% |
| 17 | Thermal Products Corporation | $7,040,242.57 | 0.874% |
| 18 | Climacool | $6,262,338.00 | 0.778% |
| 19 | VTS America | $5,149,566.70 | 0.639% |
| 20 | Danfoss | $4,190,640.23 | 0.520% |
| 21 | Nailor | $4,041,145.96 | 0.502% |

Energy Labs + LG Electronics + AAON alone are **57.4%** of all Equipment
Sales dollars since 2021 -- the top of DMG's real line card is extremely
concentrated in three names.

**Every line under 1% share: 67 vendors, combined $85,533,936.50 (10.621%
of the $805.3M total).** These are (rank 22 downward from the table above,
by dollars, all vendors below): Nortek Air Solutions, York International,
Clean Air Group / AtmosAir, Yaskawa, MacroAir, Sharpe Heating &
Ventilating, RAE Corporation, Laars Heating Systems, Amiad Water Systems,
UVDI (Ultra Violet Devices), Carel USA, RectorSeal, ClimateCraft,
Uni-Products, Thybar, Multiaqua, Critical Room Control, CDI - Crystal
Distribution, Hays Fluid Controls, Mestex, Cambridge Air Solutions, Stulz
Air Technology Systems, Sierra Filtration Products, Howden American Fan,
Toro-Aire (DMG), Heat Pipe Technology, S&P Ventilation Systems, Systemair /
Fantech, Seresco Technologies, Twin City Fan, Modine, MicroMetl, Hydronic
Components (HCI), American Metal Filter, WaterFurnace, American Aldes,
Camfil USA, Taco, Benoist, Safe-Air Dowco, Specified Controls, AES
Industries, McClintock & Bustad, PennBarry, Diversitech, Badger Meter,
Grainger, Navac, FabricAir, Marley Engineered Products, Panasonic, TPI
Corporation, M.A.T., Griswold Controls, Wattmaster Controls, McMaster-Carr,
Belimo, Reliable Controls, Allied Air Enterprises, Johnson Controls (the
last three at literally $0 mapped dollars since 2021 despite item-master
rows existing for them), plus the 116-entry ABSTAIN/unmapped tail from
section 1.

## 3. (b) Active mechanical contractors buying from exactly one vendor

**Method, and its real limitation, disclosed up front:** a customer is
identified the same way `docs/BACKTEST-2026-09-10.md` and
`docs/NETSUITE-DEDUPE-PROPOSAL.md` established -- the leading number on an
order line's `Name` field is the customer's NetSuite **"ID"**, matched
against the customer master's own `ID` column (`ScoutResults653.csv`).
That method is exact for orders billed **directly** to the top-level
customer record. It does **not** capture orders billed to a NetSuite job
(a sub-record under a customer with its own ID, never exported in the
customer file) -- confirmed extensively in the dedupe proposal, same gap
here. **This means every contractor's total below is a floor, not a
complete total** -- job-level equipment purchases are real dollars that
exist in the order-lines file but cannot be attributed back to a specific
top-level contractor from this export alone.

Of 1,637 "Mechanical Contractor"-category customers in the master, **40**
have >$50,000 in directly-attributed Equipment Sales dollars since 2021
AND are marked active (Inactive = "No"). Of those 40, **3** show purchases
from exactly one normalized vendor (their ABSTAIN-bucket dollars, if any,
are shown separately and don't count as a second vendor, since ABSTAIN
means "vendor unknown," not "a different vendor"):

| # | Contractor | Total equipment $ (since 2021) | Sole vendor | Also has $ under ABSTAIN |
|---|---|---:|---|---:|
| 1 | Air Shield, Inc. | $150,000.00 | Laars Heating Systems | $0.00 |
| 2 | GA Heating and Air | $85,584.63 | LG Electronics | $41,332.67 |
| 3 | National Service & Controls | $77,291.78 | AAON | $39,745.50 |

(That's the full top 20 -- only 3 contractors qualify.)

## 4. (c) Quote-to-order conversion by vendor

From `ScoutQuoteLinesResults462.csv`: 85,191 total quote lines. Status
breakdown: Processed 42,881, Expired 40,726, Open 1,581, Closed 2, blank 1.
Conversion is computed over Processed + Expired only (83,607 lines, the
resolved population) -- Open/Closed/blank excluded as not-yet-decided.

**Join hit rate on the resolved population:** item not found in master
4,446 (5.32%), blank Preferred Vendor 7,974 (9.54%), unmapped vendor 475
(0.57%) -- 12,895 lines (15.42%) land in ABSTAIN; 70,712 (84.58%) got a
mapped vendor.

| Vendor | Processed $ | Expired $ | Conv. ($) | Processed lines | Expired lines | Conv. (lines) |
|---|---:|---:|---:|---:|---:|---:|
| SPX Cooling Technologies | $7,305,160 | $44,415,175 | 14.1% | 2,777 | 8,147 | 25.4% |
| LG Electronics | $12,109,652 | $10,319,262 | 54.0% | 16,319 | 10,835 | 60.1% |
| AAON | $6,972,501 | $11,082,684 | 38.6% | 10,252 | 8,830 | 53.7% |
| **ABSTAIN** | $4,814,996 | $10,501,664 | 31.4% | 7,202 | 5,693 | 55.9% |
| Vertiv | $1,151,971 | $6,190,873 | 15.7% | 261 | 834 | 23.8% |
| Dunham-Bush | $1,149,538 | $3,045,846 | 27.4% | 383 | 727 | 34.5% |
| International Environmental (IEC) | $1,559,499 | $1,771,414 | 46.8% | 1,251 | 886 | 58.5% |
| Howden American Fan | $609,634 | $1,945,024 | 23.9% | 53 | 185 | 22.3% |
| ClimateMaster | $863,088 | $1,386,177 | 38.4% | 1,740 | 1,931 | 47.4% |
| Strobic Air | $440,524 | $1,613,743 | 21.4% | 45 | 105 | 30.0% |
| Energy Labs | $233,265 | $1,763,049 | 11.7% | 78 | 274 | 22.2% |
| Yaskawa | $442,092 | $1,064,856 | 29.3% | 316 | 454 | 41.0% |
| Neptronic | $450,762 | $499,208 | 47.5% | 807 | 642 | 55.7% |

**Finding worth a human's attention:** Energy Labs is the #1 vendor by
actual sold dollars (section 2) but has the **second-lowest** quote-dollar
conversion rate shown here (11.7%, only SPX is lower). Not diagnosed
further in this report -- plausibly Energy Labs equipment moves through a
different deal path than the formal quote pipeline (negotiated/design-build
jobs, e.g.), but that's a hypothesis, not a checked fact.

## 5. What this report deliberately does not do

No schema change. No database write, local or production. The vendor
normalization map lives in `config.yaml` (`line_card.vendor_normalization`)
for future editability, but nothing reads it at runtime yet -- this is a
one-off analysis script's output, not a wired pipeline stage. Part (b)'s
job-level attribution gap (section 3) is real and unresolved; closing it
would need a NetSuite customer:job hierarchy export this session does not
have, same conclusion `docs/NETSUITE-DEDUPE-PROPOSAL.md` already reached
for the same underlying data shape.
