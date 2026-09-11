# NetSuite customer dedupe — proposal, 2026-09-11

Report only. No schema change, no migration, nothing applied to `accounts` or
anywhere else — `accounts` is empty in the local DB today (the netsuite
customer importer, `app.importers.netsuite_customers`, has code and tests but
has never actually been run against real data), so this whole report is
computed directly off the two source NetSuite exports rather than the DB:
`ScoutResults653.csv` (7,093 customer rows) and
`ScoutSalesOrderLinesResults612.csv` (152,988 order lines, 0 unresolved).

## Method, and its limits

Every order line's `Name` field is `"<ID> <customer text>[ : <job/project>]"`.
The leading number is trustworthy (verified against the real file, same as
`docs/BACKTEST-2026-09-10.md`), but it is the customer's NetSuite **"ID"**
field, not "Internal ID" — and it is *only* populated for orders billed
directly to the top-level customer record. NetSuite jobs (sub-records under a
customer, e.g. "CENTRAL AIR SYSTEMS INC : La'i Loa - Mockup Stand") get their
**own** ID, not exported in the customer file at all, so a pure ID-match
under-counts. Where the two duplicate records' display text differs even by
case (`"CENTRAL AIR SYSTEMS INC"` vs `"Central Air Systems Inc"`), matching on
the exact text instead correctly recovers those job-level lines and attributes
them to the right record. Where the two records' text is **byte-identical**,
job-level lines pooled under that text cannot be split between them from this
export — flagged per row below, not guessed at.

**Order count** = distinct Document Numbers. **Dollars** = sum of `Amount`
across all lines, no Class filter (this task didn't ask for equipment-only).
**Last order date** = max `Date`. Figures below are the **direct** count (ID
exactly matches that record) unless noted — the unambiguous floor for that
specific record. Where text-matching recovers additional job-level orders
unambiguously (different text per record), that total is used instead and
noted. Where the pool is ambiguous, both are shown.

## The 10 pairs

### Therma
| | Internal ID 7547 | Internal ID 48871 |
|---|---|---|
| Name (as exported) | Therma LLC | THERMA LLC |
| Category | Mechanical Contractor | Controls Contractor |
| Sales Rep | Fabio Kwon | James Burwell |
| City | Irwindale, CA | San Jose, CA |
| Orders | 50 | 27 |
| Dollars | $34,272.06 | $33,901.24 |
| Last order | 2023-12-27 | 2023-10-18 |

Case differs between the two, so text-matching recovers job-level lines
cleanly: 7547's real total is 61 orders / $264,921.06 (last 2024-06-13); 48871's
is 28 orders / $35,181.24 (last 2023-10-18, unchanged). Direct-ID figures
above are the conservative floor; the text-matched totals are the more
complete picture and should be used for the actual rollup.

### Elite Air Conditioning — not a clean pair, 3 records
| | Internal ID 7776 | Internal ID 8550 | Internal ID 145409 |
|---|---|---|---|
| Name | ELITE AIR CONDITIONING | ELITE AIR CONDITIONING | Elite Air Conditioning |
| Category | Mechanical Contractor | Mechanical Contractor | (blank) |
| Sales Rep | Kevin Nolan | Kevin Nolan | (blank) |
| City | Norco, CA | Torrance, CA | Pinon Hills, CA |
| Direct orders | 0 | 0 | 24 |
| Direct dollars | $0.00 | $0.00 | $43,788.79 |
| Last order | — | — | 2026-03-03 |

7776 and 8550 are the genuine duplicate — identical ALL-CAPS name, identical
rep, zero order history each. 145409 is a third, differently-formatted record
(no rep, no category on file) that carries all the real order activity.
Text-matching on the shared ALL-CAPS name finds only 2 more orders / $7,233
pooled between 7776 and 8550 — and one of those two lines (id 28943) is
actually billed to a *different* customer entirely ("Allie Engineering") whose
own NetSuite ID happens to be 28943; "ELITE AIR CONDITIONING" only appears as
free text in that line's job name, not as the real customer. That's a live
example of exactly the text-matching trap `docs/BACKTEST-2026-09-10.md` §3
already documented. Net: 7776/8550 have no reliably attributable orders at
all. Flagged for a human, not resolved here — see §3 below.

### SAIC
| | Internal ID 8219 | Internal ID 8351 |
|---|---|---|
| Category | (blank) | (blank) |
| Sales Rep | Mike Richardson | TJ Cram |
| City | Fairfield, NJ | Honolulu, HI |
| Direct orders | 23 | 13 |
| Direct dollars | $30,761.00 | $23,695.71 |
| Last order | 2023-04-24 | 2023-05-19 |

Both records' display text is the bare, identical string `"SAIC"`. 29 more
orders ($87,413.69) sit under 26 distinct job-level sub-IDs pooled under that
same text — cannot be split between the NJ and HI offices from this export.
Total pooled activity: 65 orders / $141,870.40, last 2023-08-30.

### Central Air Systems
| | Internal ID 8322 | Internal ID 53773 |
|---|---|---|
| Category | (blank) | Mechanical Contractor |
| Sales Rep | Matt Tio | John Wilson |
| City | Ewa Beach, HI | Placentia, CA |
| Orders (text-matched, case disambiguates) | 34 | 1 |
| Dollars | $376,396.95 | $0.00 |
| Last order | 2025-10-23 | 2025-03-26 |

Case differs (`"CENTRAL AIR SYSTEMS INC"` vs `"Central Air Systems Inc"`), so
every job-level line resolves cleanly to one record or the other — no pooling
ambiguity here despite 6 extra job IDs under 8322.

### DMG Hawaii — not a customer duplicate, 5 near-identical records
| Internal ID | Name | Category | City | Direct orders | Dollars | Last order |
|---|---|---|---|---:|---:|---|
| 8385 | DMG HAWAII | (blank) | Aiea, HI | 0 | $0.00 | — |
| 8534 | DMG HAWAII | (blank) | Aiea, HI | 0 | $0.00 | — |
| 42430 | DMG HAWAII | (blank) | Aiea, HI | 0 | $0.00 | — |
| 48184 | DMG Hawaii | (blank) | Aiea, HI | 0 | $0.00 | — |
| 63675 | DMG HAWAII | **DMG Office** | (blank) | 9 | $5,065.94 | 2026-07-02 |

Four records share byte-identical "DMG HAWAII" text; the fifth (48184) has no
orders under either matching method. This is DMG's own internal Hawaii
branch-office record (`Category = "DMG Office"` on the only active one, 63675)
— it is not a customer at all. It should be excluded from customer dedupe
entirely, not merged the way a real customer duplicate would be; see §3.

### Hyatt (Hyatt Corporation)
| | Internal ID 8634 | Internal ID 8928 |
|---|---|---|
| Category | Owner | Owner |
| Sales Rep | (blank) | (blank) |
| City | Long Beach, CA | Huntington Beach, CA |
| Orders | 1 | 29 |
| Dollars | $0.00 | $29,031.93 |
| Last order | 2024-08-16 | 2026-08-18 |

No pooling ambiguity — both IDs account for the full 30-order, $29,031.93
total under the shared "HYATT CORPORATION" text.

### Performance Mechanical
| | Internal ID 8737 | Internal ID 18236 |
|---|---|---|
| Category | Supplier | Mechanical Contractor |
| Sales Rep | (blank) | Dave Beach |
| City | Temecula, CA | Temecula, CA |
| Direct orders | 1 | 9 |
| Direct dollars | $0.00 | $14,133.11 |
| Last order | 2023-01-24 | 2025-09-24 |

Identical display text on both, so 2 more orders ($2,461.02) pooled under 2
unknown job IDs can't be split between them. Pooled total: 12 orders /
$16,595.13, last 2026-06-21.

### Reeds Heating
| | Internal ID 18365 | Internal ID 20716 |
|---|---|---|
| Category | Mechanical Contractor | Mechanical Contractor |
| Sales Rep | Scott Tunnell | (blank) |
| City | (blank) | Paso Robles, CA |
| Direct orders | 0 | 5 |
| Direct dollars | $0.00 | $1,856.92 |
| Last order | — | 2025-09-30 |

Identical display text on both. A materially large chunk — 3 more orders,
**$44,584.99** — sits under 2 unknown job IDs pooled with this text and cannot
be split between 18365 and 20716 from this export. Pooled total: 8 orders /
$46,441.92, last 2026-06-29. This is the pair where the unattributable amount
is largest relative to what's confirmed — worth a real NetSuite job-hierarchy
pull before trusting any rollup number for this one.

### Northrop Grumman Innovation Systems
| | Internal ID 72065 | Internal ID 72069 |
|---|---|---|
| Category | General Contractor | General Contractor |
| Sales Rep | (blank) | (blank) |
| City | Commerce, CA | Minnetonka, MN |
| Orders | 5 | 2 |
| Dollars | $26,137.34 | $19,556.42 |
| Last order | 2025-01-15 | 2021-11-11 |

Both created the same day (2021-11-11), 30 minutes apart — looks like a
straight duplicate data-entry mistake. No pooling ambiguity; the two IDs
account for the full 7-order, $45,693.76 total.

### Beach Air
| | Internal ID 116649 | Internal ID 165537 |
|---|---|---|
| Category | (blank) | Mechanical Contractor |
| Sales Rep | (blank) | Evan Brown |
| City | (blank) | (blank) |
| Orders | 2 | 1 |
| Dollars | $4,320.83 | $81,602.32 |
| Last order | 2025-12-16 | 2025-11-19 |

No pooling ambiguity. Note the size mismatch: the *older*, blank-rep record
(116649) has the later last-order-date by less than a month, but the
*newer*, rep-assigned record (165537) carries 95% of the dollars on a single
order — see the recency-vs-volume risk in §3.

## NORMAN S. WRIGHT — flagged separately, not a name dedupe at all

Internal ID **7706** ("NORMAN S. WRIGHT", rep Lindsey Kelley, Brisbane, CA)
and Internal ID **5621** ("NORMAN S. WRIGHT CLIMATEC MECHANICAL EQUIPMENT",
no rep, Anaheim, CA) both carry NetSuite **"ID" 197** — the same ID, on two
different Internal IDs. Checked directly: zero order lines anywhere in the
152,988-row export use leading ID 197. This is not two accounts that look
alike; it's one NetSuite ID claimed by two different customer records, which
means any *future* order NetSuite attaches to ID 197 could land against
either Internal ID depending on which one happened to be active in NetSuite
at billing time, unpredictably. This needs to be fixed at the NetSuite source
(merge the two records or reassign one to a real, unique ID) before Scout's
dedupe logic tries to reconcile it — no rule on Scout's side can pick a
"canonical" one here, because the ambiguity is upstream of Scout's data
entirely.

(Two more "Norman S. Wright"-family records exist — Internal ID 8734, "NORMAN
S. WRIGHT" again with its own distinct ID 1221, and Internal ID 46888, "Norman
S Wright Climatec Mechanical Equipment" with distinct ID 12592 — plus a
separately-branded "Norman S. Wright Duckworth Mechanical," Internal ID
164888. None of those share the ID-197 collision; not in scope of this flag.)

## Proposed canonical_account rule

**Winner:** within a duplicate group, the record with the most recent
`last_order_date` (computed the same way as this report — direct-ID plus
unambiguous text-matched orders) becomes `canonical_account`.

**Loser(s):** keep their own `netsuite_internal_id` (never renumbered —
NetSuite is the source of truth for that field) and get a new
`canonical_account_id` column pointing at the winner's `id`.

**Rollups:** any downstream view of "this customer's orders/dollars/last
order" reads across the whole group — `SUM(orders)`, `SUM(dollars)`,
`MAX(last_order_date)` over the canonical record plus every record pointing
at it — not just the winner's own row.

## Where the rule picks wrong, and needs a human

1. **NORMAN S. WRIGHT (ID 197).** No signal to rank on — both records show
   zero orders. This isn't a "pick a winner" case; it's a source-data defect
   that has to be fixed in NetSuite first (see above).
2. **Elite Air Conditioning.** The rule would make 145409 canonical over
   7776 and 8550 by recency — but 145409 has no Category, no Sales Rep, and
   different name formatting than the other two, and one of the only two
   orders that *could* connect it to the pair turned out on inspection to
   belong to a completely different customer. A human should confirm 145409
   is really the same legal entity before merging 3 records into 1, not just
   run the rule.
3. **DMG Hawaii.** Not a customer at all — DMG's own branch office
   (`Category = "DMG Office"`). The rule as written would happily "dedupe"
   it like any customer pair and start rolling up order history into an
   internal record. It should be excluded from this rule by category, before
   the rule ever sees it.
4. **SAIC, Reeds Heating, Performance Mechanical, Therma** (identical or
   near-identical display text between duplicates). The rule correctly picks
   a winner by recency, but the rollup dollars will be **understated** —
   $87,413.69 (SAIC), $44,584.99 (Reeds Heating), $2,461.02 (Performance
   Mechanical) in job-level orders can't be attributed to either member of the
   pair from this export. A real NetSuite customer:job hierarchy pull is
   needed before any of these four rollups can be called complete, especially
   Reeds Heating where the unattributed amount dwarfs what's confirmed.
5. **General recency-vs-volume risk (Beach Air is the clearest example).**
   "Most recent order wins" can hand canonical status to a record with a
   trivial order over one with the real transaction history, if the trivial
   order happens to be a few weeks newer — Beach Air's 116649 (2 small
   orders, last 2025-12-16) would beat 165537 ($81,602 on one order, last
   2025-11-19) by less than a month under a pure-recency rule. The rule as
   proposed doesn't fail here (both eventually roll up either way, and the
   dollars aren't lost), but it's worth deciding up front whether "most
   recent" or "most dollars" should be the tiebreak, since they don't always
   agree.
6. **Field-level survivorship isn't defined.** The rule says nothing about
   what happens to the *loser's* own fields (Sales Rep, Category, city) once
   it's no longer canonical — e.g. Performance Mechanical's loser (8737) is
   Category `Supplier`, a different classification than the winner's
   `Mechanical Contractor`. Pointing `canonical_account_id` at the winner
   doesn't by itself say which Category/Sales Rep should represent the
   merged group in any UI that only reads the canonical row — that's a
   second decision this proposal doesn't make.
