# DMG Scout — UI spec

Version 0.1, August 7 2026. Written during the Phase C board rebuild, because the
spec was referred to as governing and did not exist as a document. This records
the decisions the board is built on so the remaining views do not each reinvent
them. `docs/CHARTER.md` governs; this is subordinate to it.

---

## 1. Stack

| Layer | Choice | Why this and not the obvious alternative |
|---|---|---|
| Server | FastAPI + Jinja2 | Already there. The data is server-side and the pages are documents. |
| Interaction | HTMX | Partial updates without a build step or a client-side router. |
| CSS | Tailwind v4, **standalone binary** | No Node, no `package.json`, no lockfile in a Python service. Pinned in the Makefile (`TAILWIND_VERSION`) so a rebuild elsewhere cannot silently differ from what was reviewed. |
| Charts | ECharts by CDN | Not yet used. The board earns nothing from a chart; the first real use is the pipeline/coverage view. |
| Map | Leaflet | Map view only. |

`app/web/static/app.css` is a **build artefact**. `make css` after touching any
template — Tailwind only emits utilities it can see used. `make css-watch` while
working.

## 2. The design idea

**It reads like an engineering submittal, not like a dashboard.** That is a
constraint with consequences, not a mood:

- **A submittal is paper.** The sheet has a border, a title block saying what you
  are looking at and when it was true, and a sheet number. Default theme is bond
  paper; dark is the same design with the paper turned down, not a second design.
- **Numbers are data, not decoration.** Every figure is monospace with tabular
  figures. A column of tonnages that does not align on the digit is unreadable at
  289 rows. No number is ever set in the prose face.
- **Rules, not shadows.** Depth comes from line weight: hairline between rows,
  1px panel edges, 2px sheet border. No drop shadows, no rounded cards, anywhere.
- **Labels are drafting labels.** Uppercase, letter-spaced, small, quiet.
- **Density is the feature.** A submittal schedule fits on the sheet. Twenty rows
  visible at 1600px is the target, not twenty rows per page of pagination.

There is no component library. The whole system is the classes in
`app/web/styles/app.css` and it should stay small enough to read in one sitting.

## 3. Colour

One accent — drafting blue — plus three desaturated marker colours for state. A
board where everything shouts reads as though nothing is urgent.

Tokens live in `@theme`; dark overrides only the surface tokens. Never hard-code a
hex in a template.

**Colour never carries meaning alone.** Every state that matters also has a word:
the window stamp reads `PRE-BOD`, not just a green outline.

### Score is a bar, not a hue ramp

The board previously coloured the score column on a blue→red thermal gradient.
It failed twice against real data and both failures are worth remembering:

1. **It read as a traffic light.** High scores rendered red, which on a ranked
   list says "bad" where it means "act first".
2. **It discriminated nothing.** Live scores cluster between 0.30 and 0.70 —
   exactly the yellow-green middle of that ramp — so ~250 of 289 rows came out
   near-identical, in the worst contrast zone on white.

A bar in one ink fixes both: length is the ranking, it is scaled to the board's
own maximum so the visible range is the range that exists, and it needs no colour
semantics. It also survives a theme flip and a greyscale print.

## 4. Rules that outrank aesthetics

These come from the charter and are not negotiable at the template level.

1. **Callouts are only ever about the board's own trustworthiness.** Not tips,
   not marketing. If a callout is on screen, something below it is not safe to
   act on. The incomplete-board and zero-pre-BOD banners are the only two so far.
2. **Every row shows whether there is a human to call.** The charter's success
   criterion is "a project, early, plus at least one human with a phone or
   email". A board that does not show the human cannot be read as a call list.
3. **A name with no contact method is labelled `Research`, never counted as
   coverage.** Three states — callable, name-only, nobody — and the middle one is
   the honest part.
4. **A caveat is a caveat, not an alarm.** `fa` (sized from floor area) is the
   norm for industrial rows. Setting it in warning red put a red mark on most of
   the board and taught the eye to ignore red.
5. **Every number traces to a source.** Where a figure has a basis string, it is
   reachable — tooltip at minimum, link where one exists.

## 5. Layout primitives

| Class | Use |
|---|---|
| `.sheet` | Bordered panel. The page is sheets, not cards. |
| `.titleblock` / `.tb-cell` / `.tb-lbl` / `.tb-val` | Title block. Always carries "data as of". |
| `.tabs` / `.tab` | Sheet tabs. `aria-current="page"` marks the active one. |
| `.sec-head` / `.sec-title` | Section rule and label. |
| `.tbl` + `<colgroup>` | Data table. **Fixed layout, explicit widths.** Auto layout let the widest developer string starve the project column to ~9rem and wrap names to five lines. |
| `.clip` / `.clamp2` | One line clipped / two lines clamped. Full string in `title`. |
| `.stamp` + `-pre/-in/-post/-quiet` | QA stamp. Outlined, uppercase, no fill. |
| `.score` + `.bar` | Number plus proportional bar. |
| `.callout` + `-warn/-bad` | See rule 1. |
| `.chip` | Filter. `aria-pressed` marks selection. |
| `.kv` | Summary strip under a section heading. |
| `.caveat` | Quiet dotted-underline qualifier on a number. |
| `.note` | De-emphasised supporting text. |

## 6. Performance note that shaped the design

The contact column was absent from the board because `build_ladder` issued a
query per signal, per document and per person, plus a full `Firm` scan per
person: 289 rows took minutes. `build_ladders` loads everything up front — same
rung logic, 0.9s for the whole board. **Any view that renders a column per
project must use a bulk loader.** The pattern is in `app/ladder.py`.

## 7. Views

| Sheet | Route | Status |
|---|---|---|
| S-01 Board | `/` | Built |
| S-02 Watch list | `/watchlist` | Shares the board template |
| S-03 Project | `/project/{id}` | **On the shim** |
| S-04 Review queue | `/review` | **On the shim** |
| S-05 Contacts | `/contacts` | **On the shim** |
| S-06 Map | `/map` | Built — Leaflet + geolocation |
| S-07 Source health | `/health` | **On the shim** — intended first ECharts use |
| S-08 Ask | `/ask` | Built — plain-language search |
| S-09 Saved searches | `/searches` | Built |
| S-10 Firms | `/firms`, `/firm/{id}` | Built |
| S-11 Outreach | `/outreach` | Built |

### The shim

Sheets marked **on the shim** were written against the previous stylesheet and are
carried by the migration block at the end of `app/web/styles/app.css`. They render
correctly and coherently, but in the *old* vocabulary (`.dim`, `.tag`,
`.scorechip`, unclassed `<table>`) rather than the system above.

This is a real debt, deliberately taken and written down rather than hidden.
Restyling one means renaming its classes to the built vocabulary — `.dim` → `.note`,
`.tag` → `.stamp`, `.scorechip` → `.score` — and then deleting the shim rules that
sheet was the last user of. Nothing new has to be invented for any of them.

ECharts is still unused. The honest first application is Source health, where a
run-history strip answers a question a table answers badly; the board earned
nothing from a chart and did not get one.
