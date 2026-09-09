"""The assumptions register: every tunable constant this system scores,
sizes, or ranks with, in one place, and — honestly — where each one
actually came from.

The board already fixed this once for tonnage: a project sized from stated
IT load and one sized from a floor-area rule of thumb do not print the same
way, because they are not the same kind of claim (see
app/pipeline/sizing.py's `estimate_low_confidence` / basis strings). The
board's SCORE has never gotten the same treatment — a number built from six
multiplied judgment calls prints with exactly as much visual confidence as
one built from a verified line-card figure. This register is the fix at the
constant level, one page instead of one field: for every weight, band, or
threshold this system quotes a customer or a VP off, is it measured from our
own data, stated by a named person on a date, an industry rule of thumb, or
an invented placeholder nobody has gone back to fix?

Values are read LIVE from config.yaml on every request — this module never
hardcodes a number, so the page can't silently drift from what's actually
running. The SOURCE CLASSIFICATION and review-date text are hand-curated
here, deliberately: whether a number is "measured" or "a guess" is an
editorial judgment already made once, in the config.yaml comment beside it
— this module transcribes that judgment onto one page rather than
re-deriving it from the prose automatically, which would risk quietly
misclassifying an entry no one has re-read closely enough to catch.

Say so plainly rather than dressing it up: several entries below are
invented placeholders, and one (service life for state/municipal ownership)
is interpolated with no source at all. That is the finding, not something
to hide the page's own honesty from.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from app.config import Config
from app.delivery import DELIVERY_METHOD_LABELS


def slugify(name: str) -> str:
    """Anchor id for a row on /assumptions, derived from its name so a link
    can point at a specific constant without a second hand-maintained id to
    drift out of sync with it (see Assumption.slug)."""
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")

# Controlled vocabulary — every entry below must be exactly one of these.
# Deliberately not a spectrum or a score: a number is measured, or it isn't,
# and dressing an invented placeholder up as "estimated" the same way an
# interpolated-between-two-real-anchors figure is estimated would be the
# same false-precision problem this whole page exists to fix.
MEASURED = "measured"
STATED = "stated_by_a_named_person"
RULE_OF_THUMB = "industry_rule_of_thumb"
PLACEHOLDER = "invented_placeholder"
PHYSICS = "physics_or_arithmetic"

SOURCE_LABELS = {
    MEASURED: "Measured from our own data",
    STATED: "Stated by a named person, on a date",
    RULE_OF_THUMB: "Industry rule of thumb / published reference",
    PLACEHOLDER: "Invented placeholder — a judgment call, not a measurement",
    PHYSICS: "Physical or arithmetic constant",
}
# Display order — measured first (the most defensible), placeholders last
# (the ones to interrogate first if a ranked list looks wrong).
SOURCE_ORDER = [MEASURED, STATED, RULE_OF_THUMB, PHYSICS, PLACEHOLDER]


@dataclass
class Assumption:
    group: str                       # section heading on the page
    name: str
    config_path: str | None          # None => hardcoded in Python, not even config-tunable
    value: str                       # pre-formatted for display; always read live from cfg
    source_type: str                 # one of the constants above
    source_detail: str               # the honest account — quote the config comment where one exists
    verified: bool | None = None     # None: no verified/unverified concept applies to this entry
    last_reviewed: str | None = None  # an actual date/basis if one exists; None => not recorded, say so
    # Distribution-strip widget (app/web/templates/_widgets.html:dist_strip) --
    # ALL THREE or none. Only set for the handful of entries with a genuinely
    # documented plausible range already sitting in source_detail above (a
    # published rule-of-thumb range, a measured CI, a sensitivity sweep) --
    # every other entry (the majority) renders no strip at all rather than a
    # fabricated one. range_is_proxy=True draws the strip's tick dashed, for
    # the one entry whose own source_detail already says its range is an
    # unvalidated proxy, not a confirmed bound.
    numeric_value: float | None = None
    plausible_low: float | None = None
    plausible_high: float | None = None
    range_is_proxy: bool = False

    @property
    def slug(self) -> str:
        return slugify(self.name)


def _fmt_table(d: dict, fmt: str = "{:.2f}") -> str:
    if not d:
        return "—"
    return ", ".join(f"{k}={fmt.format(v)}" for k, v in d.items())


def load_assumptions(cfg: Config, service_calls_coverage: dict | None = None,
                     delivery_method_coverage: dict | None = None,
                     ownership_recency_coverage: dict | None = None,
                     portfolio_coverage: dict | None = None,
                     hospital_coverage: dict | None = None,
                     hospital_capability_gaps: dict | None = None) -> list[Assumption]:
    """Everything below is read from cfg at call time — never hand-copied —
    so the VALUE column can't drift from what's actually running even if
    this function's prose goes stale.

    service_calls_coverage and delivery_method_coverage are the two
    exceptions to "cfg only": live row counts (app.pipeline.retrofit:
    service_calls_coverage, app.pipeline.resolve:delivery_method_coverage)
    for the entries below where the honest VALUE is "how many rows have
    this populated right now", not a config constant. Both optional and
    default to None (value reads "not available" rather than crashing) so
    every existing caller that passes only cfg keeps working unchanged."""
    out: list[Assumption] = []

    # ---- Score weights ----------------------------------------------------

    wm = cfg.get("scoring.window_multipliers", {})
    out.append(Assumption(
        group="Score weights", name="Window multiplier",
        config_path="scoring.window_multipliers",
        value=_fmt_table(wm),
        source_type=PLACEHOLDER,
        source_detail="How much a project past basis-of-design should be discounted. No measurement or named "
                      "source recorded in config.yaml — a judgment call about the sales-critical piece of the "
                      "formula (a 500 MW project already out to bid must rank below a 40 MW project at NOP).",
    ))

    sc = cfg.get("scoring.signal_certainty", {})
    out.append(Assumption(
        group="Score weights", name="Signal certainty priors",
        config_path="scoring.signal_certainty",
        value=(f"{len(sc)} signal types, {min(sc.values()):.2f}–{max(sc.values()):.2f}" if sc else "—"),
        source_type=PLACEHOLDER,
        source_detail="Per-signal-type confidence (e.g. a bid invite reads more certain than a job posting). "
                      "A judgment call about relative reliability, not calibrated against actual win/loss "
                      "outcomes yet — see Phase E in docs/CHARTER.md, which waits on DMG's own history for "
                      "exactly this kind of check.",
    ))

    cb = cfg.get("scoring.corroboration_bonus", 0.25)
    cc = cfg.get("scoring.corroboration_cap", 2)
    out.append(Assumption(
        group="Score weights", name="Corroboration bonus / cap",
        config_path="scoring.corroboration_bonus, scoring.corroboration_cap",
        value=f"+{cb:.2f} per extra distinct signal type, capped at {cc:.0f}",
        source_type=PLACEHOLDER,
        source_detail="Design choice — two independent signal types is treated as the threshold separating a "
                      "real build from a speculative queue entry — not derived from measured outcomes.",
    ))

    rh = cfg.get("scoring.recency_halflife_days", 180)
    out.append(Assumption(
        group="Score weights", name="Recency half-life",
        config_path="scoring.recency_halflife_days",
        value=f"{rh:.0f} days",
        source_type=PLACEHOLDER,
        source_detail="\"No new signal in 180 days ~ halves the score.\" A round-number judgment call, and "
                      "still not being changed in this pass -- there are no call outcomes to fit a decay "
                      "rate against. What Scout's own history CAN show, as a proxy (checked 2026-08-16): the "
                      "distribution of gaps between successive signals on the same project -- how often new "
                      "information actually arrives, not how fast confidence in stale information should "
                      "decay, a related but distinct question. 90 projects have 2+ dated signals, 115 "
                      "consecutive gaps: median 130 days, mean 159, p25=53, p75=236, p90=319 (18% of gaps "
                      "≤30d, 63% ≤180d, 6% >365d). 180 days sits between the median and p75 -- a plausible "
                      "ballpark, not a validated number: a project that averages a new filing every 130 days "
                      "isn't necessarily one whose SCORE should have halved by day 180, and this proxy can't "
                      "tell the two apart. Does not prove 180 is correct, or measure the actual thing this "
                      "constant claims (how fast a REP's confidence a project is still live should erode).",
        numeric_value=rh, plausible_low=53, plausible_high=319, range_is_proxy=True,
    ))

    sfc = cfg.get("scoring.size_factor", {})
    out.append(Assumption(
        group="Score weights", name="Size factor formula",
        config_path="scoring.size_factor",
        value=f"max({sfc.get('floor', 0.25):.2f}, log10(tons_midpoint) − {sfc.get('offset', 2.0):.2f}); "
             f"unknown size → {sfc.get('unknown_default', 0.5):.2f}",
        source_type=PLACEHOLDER,
        source_detail="Config-tunable as of 2026-08-16 -- was hardcoded in app/pipeline/scoring.py, the one "
                      "row on this page with no config_path at all, until a sensitivity sweep (see this "
                      "page's own methodology, and the offset/floor/unknown_default fields' commit history) "
                      "found it the single most sensitive constant in the whole scoring system: a ±50% "
                      "perturbation of the offset moved 98% of board rows and only 10 of the top 20 "
                      "survived, with no way to calibrate it. Log-scaled so a 10x bigger project doesn't "
                      "drown out winnability; the specific offset/floor/unknown_default values are still "
                      "not measured against anything -- being tunable now is what makes that possible next, "
                      "not evidence it's already been done.",
    ))

    ip = cfg.get("scoring.identity_penalty", {})
    out.append(Assumption(
        group="Score weights", name="Identity penalty",
        config_path="scoring.identity_penalty",
        value=_fmt_table({str(k): v for k, v in ip.items()}),
        source_type=PLACEHOLDER,
        source_detail="Steep on purpose — a row missing two of {name, developer, county} scores near zero. "
                      "Design decision to suppress placeholder rows (\"Unnamed project (Storey)\" once "
                      "outranked fully-identified rows), not a measured relationship between missing fields "
                      "and actual callability, and still not being changed in this pass -- there are no call "
                      "outcomes to fit against. Proxy checked 2026-08-16: whether a project ever flagged "
                      "with uncertain identity later resolved (merged into a fuller duplicate) or was "
                      "dropped (lost/dead/archived), vs staying an open lead. Genuinely thin, and one real "
                      "limitation up front -- Project has no history of past identity states, so this is "
                      "CURRENT snapshot, not \"flagged then later resolved\": of 335 projects, 247 are fully "
                      "identified (1 merged, 0.4%), 80 are missing one field (1 merged, 1.25%), 5 are "
                      "missing two, 3 are missing all three (0 merged from either). Only 2 merges exist in "
                      "this system's entire history, and ZERO projects have ever reached lost/dead/archived "
                      "-- there is no dropped population to compare against AT ALL yet. Consistent with (not "
                      "proof of) thinner identity meaning a duplicate is somewhat likelier, at a sample size "
                      "(n=2) nowhere near large enough to size a penalty from, and answers only the "
                      "\"merged\" half of the question the proxy was meant to check.",
    ))

    dtb = cfg.get("scoring.days_to_bid_by_stage", {})
    dtb_entitlement = dtb.get("entitlement")
    if isinstance(dtb_entitlement, dict):
        out.append(Assumption(
            group="Score weights", name="Days to estimated bid: entitlement (NOP to NOD)",
            config_path="scoring.days_to_bid_by_stage.entitlement",
            value=f"mean {dtb_entitlement.get('mid')}d, 95% CI [{dtb_entitlement.get('low')}, "
                 f"{dtb_entitlement.get('high')}]d",
            source_type=MEASURED,
            source_detail="CEQAnet NOP→NOD spread, in days, for every SCH number with both a stored NOP and "
                          "a stored NOD (checked 2026-08-16). 9 SCH numbers had both; 3 showed the NOD dated "
                          "BEFORE the NOP -- the same SCH number reused for a later, separate environmental "
                          "review action (a real CEQA practice), not a valid single-cycle interval, so those "
                          "3 were excluded rather than sign-flipped in. n=6 valid pairs: 199, 225, 350, 393, "
                          "470, 552 days. CI is the mean ± t(0.975, df=5)×SEM -- a small sample, "
                          "correspondingly wide interval, reported honestly rather than narrowed by a bigger "
                          "assumed n. Date range: earliest NOP 2024-08-08, latest NOD 2026-06-19. Replaces a "
                          "flat, invented 540 (\"midpoint of lead-time windows\").",
            numeric_value=dtb_entitlement.get("mid"), plausible_low=dtb_entitlement.get("low"),
            plausible_high=dtb_entitlement.get("high"),
        ))
    else:
        # Not yet fit (config reverted, or this environment never ran the
        # 2026-08-16 calibration) -- same placeholder framing as before,
        # scoped to entitlement alone rather than silently disappearing.
        out.append(Assumption(
            group="Score weights", name="Days to estimated bid: entitlement (NOP to NOD)",
            config_path="scoring.days_to_bid_by_stage.entitlement",
            value=f"{dtb_entitlement}" if dtb_entitlement is not None else "—",
            source_type=PLACEHOLDER,
            source_detail="\"Midpoint of lead-time windows\" per config.yaml — a judgment call, not measured "
                          "from this territory's own CEQAnet filings.",
        ))
    other_stages = {k: v for k, v in dtb.items() if k != "entitlement"}
    out.append(Assumption(
        group="Score weights", name="Days to estimated bid: other stages",
        config_path="scoring.days_to_bid_by_stage",
        value=_fmt_table(other_stages, fmt="{:.0f}"),
        source_type=PLACEHOLDER,
        source_detail="\"Midpoints of lead-time windows\" per config.yaml — judgment calls about typical "
                      "stage duration, not measured from this territory's own closed deals. Checked "
                      "2026-08-16 whether Scout's own LA mechanical-permit data could calibrate "
                      "\"permitting\": it can't -- live-verified against all three LADBS dataset windows, "
                      "none has an application-received date or a valuation field, only issue_date/"
                      "status_date, so \"application-to-issuance by permit type and valuation band\" is not "
                      "answerable from data Scout holds. Left as a placeholder rather than fit from a field "
                      "that doesn't exist.",
    ))

    # ---- Spillover ----------------------------------------------------------

    sw = cfg.get("scoring.spillover.weight", 0.15)
    out.append(Assumption(
        group="Spillover", name="Spillover weight (max bonus)",
        config_path="scoring.spillover.weight",
        value=f"×{1 + sw:.2f} max (+{sw * 100:.0f}%)",
        source_type=PLACEHOLDER,
        source_detail="\"Modest on purpose... should nudge a ranking, not remake one\" per "
                      "app/pipeline/spillover.py — the reasoning for keeping it small is stated, but the "
                      "specific 0.15 ceiling is not measured against any outcome.",
    ))

    ssat = cfg.get("scoring.spillover.saturation_mw", 500)
    out.append(Assumption(
        group="Spillover", name="Spillover saturation",
        config_path="scoring.spillover.saturation_mw",
        value=f"{ssat:,.0f} MW (full bonus reached here)",
        source_type=PLACEHOLDER,
        source_detail="No measurement or named source recorded for why 500 MW is the saturation point rather "
                      "than 300 or 1,000.",
    ))

    shl = cfg.get("scoring.spillover.halflife_days", 365)
    out.append(Assumption(
        group="Spillover", name="Spillover half-life",
        config_path="scoring.spillover.halflife_days",
        value=f"{shl:.0f} days",
        source_type=PLACEHOLDER,
        source_detail="\"The construction workforce effect is front-loaded and temporary\" — the reasoning "
                      "for having a shorter half-life than recency decay is stated; the specific 365-day "
                      "figure is not measured.",
    ))

    churn = cfg.get("iepr.churn_discount", 0.0)
    out.append(Assumption(
        group="Spillover", name="IEPR interconnection-request churn discount",
        config_path="iepr.churn_discount",
        value=f"{churn * 100:.0f}% discounted",
        source_type=MEASURED,
        source_detail="Comparing SCE's 2024 and 2025 IEPR filings, ~44% of tracked interconnection requests "
                      "did not survive unchanged year over year (canceled, or otherwise dropped) — an "
                      "interconnection request is not a project. The one genuinely measured number feeding "
                      "spillover; included here for contrast with the three placeholder rows above it.",
        last_reviewed="Measured from the 2024 vs. 2025 IEPR filing comparison — no later re-check recorded.",
    ))

    # ---- Equipment sizing -----------------------------------------------

    tpm = cfg.get("sizing.tons_per_mw_installed_default", 325)
    out.append(Assumption(
        group="Equipment sizing", name="Tons per MW installed (default)",
        config_path="sizing.tons_per_mw_installed_default",
        value=f"{tpm:.0f} tons/MW IT",
        source_type=RULE_OF_THUMB,
        source_detail="\"Installed capacity (non-IT heat, N+1 redundancy) runs 300-400 tons per MW\" per "
                      "app/pipeline/sizing.py — a published-range rule of thumb; the specific 325 midpoint "
                      "within that range is a judgment call, not itself measured. Feeds every EST. TONS "
                      "figure derived from a stated or inferred MW figure (not the floor-area rule-of-thumb "
                      "path, which uses the sqft/ton tables below instead).",
        numeric_value=tpm, plausible_low=300, plausible_high=400,
    ))

    bbb = cfg.get("sizing.band_by_basis", {})
    out.append(Assumption(
        group="Equipment sizing", name="Tonnage band width by basis",
        config_path="sizing.band_by_basis",
        value=_fmt_table(bbb) if bbb else "default 0.25 (±25%) for any basis not listed",
        source_type=PLACEHOLDER,
        source_detail="How wide the low-high tonnage band is around the midpoint, by which input drove the "
                      "estimate (a stated IT load gets a narrower band than a figure inferred from generator "
                      "capacity). No measurement or named source recorded for the specific widths.",
    ))

    evt = cfg.get("equipment_value.value_per_ton_by_facility_type", {})
    verified_rows = [k for k, b in evt.items() if "VERIFIED" in (b.get("source") or "")]
    est_rows = [k for k in evt if k not in verified_rows]
    out.append(Assumption(
        group="Equipment sizing", name="Equipment value per ton, by facility type",
        config_path="equipment_value.value_per_ton_by_facility_type",
        value=(f"{len(evt)} facility types, "
              f"${min(b['low'] for b in evt.values()):,}–${max(b['high'] for b in evt.values()):,}/ton"
              if evt else "—"),
        source_type=MEASURED,
        source_detail=(f"TWO points are real, from DMG's VP of sales: a VU Flow CO2 simultaneous "
                       f"heating/cooling chiller at ~$8,500/ton (cleanroom row), and a commodity LG "
                       f"heat-or-cool chiller at ~$1,500/ton (light_manufacturing row) — "
                       f"{', '.join(verified_rows) or 'none'} carry the VERIFIED tag directly. Every other "
                       f"row ({', '.join(est_rows) or 'none'}) is interpolated between those two anchors and "
                       f"is an estimate, not a second measurement — rows whose dominant equipment is packaged "
                       f"rooftop rather than chillers (distribution, warehouse) are extrapolated slightly "
                       f"below the commodity anchor and are the least certain in the table."),
        verified=None,  # mixed per-row by design; see source_detail
    ))

    wps = cfg.get("sizing.watts_per_sqft", 150)
    out.append(Assumption(
        group="Equipment sizing", name="Data center watts/sqft fallback",
        config_path="sizing.watts_per_sqft",
        value=f"{wps:.0f} W/sqft",
        source_type=PLACEHOLDER,
        source_detail="\"Used only when sqft is the only size input; low confidence\" per config.yaml — no "
                      "source cited. This is the input that sets Project.estimate_low_confidence when used.",
    ))

    isqft = cfg.get("sizing.industrial_sqft_per_ton_by_type", {})
    isqft_other = {k: v for k, v in isqft.items() if k != "cleanroom"}
    out.append(Assumption(
        group="Equipment sizing", name="Industrial sqft/ton — cleanroom",
        config_path="sizing.industrial_sqft_per_ton_by_type.cleanroom",
        value=f"{isqft.get('cleanroom', {}).get('low', '?')}–{isqft.get('cleanroom', {}).get('high', '?')} sqft/ton",
        source_type=RULE_OF_THUMB,
        source_detail="Upgraded 2026-08-16 from a bare stated figure once a fit was attempted and found "
                      "corroboration, not a fit: a peer-reviewed semiconductor-cleanroom cooling-load study "
                      "(200-1,400 W/m², ScienceDirect 10.1016/j.energy.2023.129948) plus an industry "
                      "technical source (60-220 W/sqft electrical load, airinnovations.com) convert to "
                      "roughly 16-190 sqft/ton. Wide — cleanroom load varies enormously by ISO class — but "
                      "this figure sits inside it. The value itself is UNCHANGED (still Andrew's original "
                      "50-150): the published range corroborates it without being precise enough to justify "
                      "moving it. Scout's own project data gave zero usable ground truth (see the sibling "
                      "'other facility types' entry) — this classification rests entirely on the published "
                      "sources above, not on anything measured internally.",
    ))
    out.append(Assumption(
        group="Equipment sizing", name="Industrial sqft/ton — other facility types",
        config_path="sizing.industrial_sqft_per_ton_by_type",
        value=(f"{len(isqft_other)} types, "
              f"{min(b['low'] for b in isqft_other.values())}–{max(b['high'] for b in isqft_other.values())} sqft/ton"
              if isqft_other else "—"),
        source_type=STATED,
        source_detail="\"These are Andrew's numbers, not defaults — the spread across types is 50x, so type "
                      "matters more than area\" — verbatim from config.yaml. Stated by name, no date "
                      "recorded. A fit was attempted 2026-08-16 (see cleanroom's sibling entry for the one "
                      "type it worked for) and came back empty for these five: Scout's own data has only 3 "
                      "industrial signals in the whole system stating both floor area and any MW/generator "
                      "field, and all 3 fail the app's own data-completeness rules for an independent "
                      "tonnage figure (no generator corroboration, or an incomplete generator record) — "
                      "n=0 usable ground truth. Published ASHRAE/AHRI figures: nothing credible found for "
                      "distribution_fulfillment, warehouse_conditioned, light_manufacturing, or "
                      "heavy_manufacturing (only mutually-contradictory, uncited web rules of thumb — one "
                      "pair disagreed by ~3x for the same building type); office_rnd had one source "
                      "attributing 190-360 sqft/ton to ASHRAE \"national average data\", but with no "
                      "specific handbook/table/edition cited and no coverage of the R&D-equipment-load "
                      "component this type is meant to capture — too weak to act on. Left exactly as "
                      "stated rather than fit on data that doesn't exist.",
        last_reviewed=None,
    ))

    rsqft = cfg.get("retrofit.candidate_sqft_per_ton", {})
    out.append(Assumption(
        group="Equipment sizing", name="Commercial/industrial sqft/ton (retrofit tonnage estimate)",
        config_path="retrofit.candidate_sqft_per_ton",
        value=_fmt_table({k: f"{v['low']}-{v['high']}" for k, v in rsqft.items()}, fmt="{}") if rsqft else "—",
        source_type=RULE_OF_THUMB,
        source_detail="\"NOT ASHRAE, NOT a measurement of any specific building — industry-standard ballpark "
                      "ranges\" per config.yaml. Used only for the replacement_candidate absence-based "
                      "opportunity population, separate from the new-construction sizing table above.",
    ))

    # ---- Replacement service life, by ownership --------------------------

    for tier in ("private_commercial", "federal", "state_municipal"):
        band = cfg.get(f"replacement.service_life.ownership.{tier}", {}) or {}
        equip = band.get("equipment", {}) or {}
        verified = bool(band.get("verified", False))
        source_type = {
            "private_commercial": RULE_OF_THUMB,
            "federal": STATED,
            "state_municipal": PLACEHOLDER,
        }[tier]
        out.append(Assumption(
            group="Replacement service life", name=f"Service life bands — {tier.replace('_', ' ')} ownership",
            config_path=f"replacement.service_life.ownership.{tier}",
            value=(f"{len(equip)} equipment types, "
                  f"{min(b['low'] for b in equip.values())}–{max(b['high'] for b in equip.values())}yr"
                  if equip else "—"),
            source_type=source_type,
            source_detail=f"{band.get('basis', '—')} — source: {band.get('source', 'not recorded')}",
            verified=verified,
            last_reviewed=("Stated 2026-08-07, one contractor field report — not re-verified since"
                          if tier == "federal" else None),
        ))

    scc = service_calls_coverage or {}
    total = scc.get("retrofit_buildings_total")
    reported = scc.get("retrofit_buildings_with_service_calls")
    out.append(Assumption(
        group="Replacement service life", name="Reported service frequency (overrides service-life proxy)",
        config_path=None,
        value=(f"{reported} of {total} retrofit_buildings rows populated"
              if total is not None else "not available on this page load"),
        source_type=STATED,
        source_detail="Manual entry only (`scout report-service-frequency`) — no scraper exists or will exist; "
                      "this lives inside contractors' FSM systems. Where populated, actual reported "
                      "service_calls_per_year replaces the YearBuilt-derived service-life proxy as the LEAD "
                      "ranking term for that building (see app.pipeline.retrofit:rank_buildings); where null, "
                      "ranking is unchanged. This is a hypothesis with exactly one data point as of 2026-08-11 "
                      "(a contractor reporting 20+ calls in a year on one unit) — deliberately not tuned beyond "
                      "\"more reported calls ranks higher\" until the sample is large enough to tune against.",
        verified=False,
        last_reviewed="Stated 2026-08-11, one contractor field report — not re-verified since",
    ))

    # ---- Accounts / line-card adjacency -----------------------------------

    vtd = cfg.get("accounts.value_tier_dollars", {})
    out.append(Assumption(
        group="Accounts", name="Value tier dollar bands (1–5)",
        config_path="accounts.value_tier_dollars",
        value=(f"tier 1: ${vtd.get(1, vtd.get('1', {})).get('low', 0):,}+ "
              f"→ tier 5: ${vtd.get(5, vtd.get('5', {})).get('high', 0):,} and under" if vtd else "—"),
        source_type=PLACEHOLDER,
        source_detail="\"ESTIMATED from typical DMG/ToroAire deal sizes — nobody has measured actual invoice "
                      "history against these tiers yet.\" Verbatim from config.yaml. Ranks coverage gaps by "
                      "dollar value rather than row count — the ranking LOGIC came from a named source (\"the "
                      "fix a Trane portfolio leader named directly: rank on unit value\"); the dollar figures "
                      "themselves did not.",
    ))

    edges = cfg.get("accounts.adjacency.edges", [])
    out.append(Assumption(
        group="Accounts", name="Category adjacency edges (line-card gap ranking)",
        config_path="accounts.adjacency.edges",
        value=f"{len(edges)} category-pair weights, 0.0–0.9 (symmetric)",
        source_type=PLACEHOLDER,
        source_detail="\"Not inference: every number here is a first-pass judgment call by category, meant "
                      "to be corrected in place by whoever actually reps these lines, not re-derived.\" "
                      "Verbatim from config.yaml. Full table lives in config.yaml, not reproduced here — too "
                      "large to quote a customer a single number from anyway; the register's job is to flag "
                      "that all ~85 of them are first-pass guesses, not that any individual edge is wrong.",
    ))

    atm = cfg.get("accounts.adjacency.account_type_modifier", {})
    out.append(Assumption(
        group="Accounts", name="Account type modifier",
        config_path="accounts.adjacency.account_type_modifier",
        value=f"{len(atm)} account types (engineer discounted hardest: ×0.3 default)",
        source_type=PLACEHOLDER,
        source_detail="\"ESTIMATED judgment calls, not measured — a wrong direction here is a small error on "
                      "every account of that type.\" Verbatim from config.yaml.",
    ))

    ncb = cfg.get("accounts.adjacency.no_coverage_base", 0.3)
    out.append(Assumption(
        group="Accounts", name="No-coverage base relevance",
        config_path="accounts.adjacency.no_coverage_base",
        value=f"{ncb:.2f}",
        source_type=PLACEHOLDER,
        source_detail="\"Not zero — an empty gap list on day one is useless — but well under most real "
                      "edges.\" Verbatim from config.yaml.",
    ))

    # ---- Retrofit ranking ------------------------------------------------------

    from app.pipeline.retrofit import AGE_CURVE_SHAPE
    out.append(Assumption(
        group="Retrofit ranking", name="Retrofit ranking: age-curve shape parameter",
        config_path=None,
        value=f"years-past-due fraction raised to the power {AGE_CURVE_SHAPE:.1f} (convex, was linear)",
        source_type=PLACEHOLDER,
        source_detail=(
            "Two separate claims, classified separately rather than blended into one: the CONVEX "
            "FORM is well-supported -- mechanical/HVAC equipment wear-out failure is commonly "
            "modeled with a Weibull hazard of shape > 1 in the reliability engineering literature, "
            "meaning replacement probability rises with age at an INCREASING rate, not a constant "
            "one, which a straight-line years-past term cannot represent. The SPECIFIC exponent "
            "(2.0, a plain square) is not that literature's fitted value -- it is not fit to "
            "anything, because no labeled failure/replacement-date data exists for this population "
            "to fit against, the same gap every other shape constant in app.pipeline.retrofit runs "
            "into (see the Contractor ranking and Replacement service life entries below). Replaces "
            "a straight-line normalization (years_past/100, capped) with that same capped fraction "
            "raised to this power -- see app.pipeline.retrofit:rank_buildings's own comment for the "
            "full reasoning. Reorders buildings WITHIN a service-life tier only: overdue still "
            "strictly outranks due, which still strictly outranks approaching, unchanged.",
        ),
    ))

    orc = ownership_recency_coverage or {}
    orc_total = orc.get("retrofit_buildings_total")
    orc_matched = orc.get("matched")
    orc_recent = orc.get("sold_last_24mo")
    out.append(Assumption(
        group="Retrofit ranking", name="Retrofit ranking: ownership-change recency",
        config_path="retrofit.ownership_recency_weight",
        value=(f"weight=0.0 (off) — {orc_matched:,} of {orc_total:,} rows have SOME recording date "
              f"({100*orc_matched/orc_total:.1f}%), but only {orc_recent:,} ({100*orc_recent/orc_total:.1f}%) "
              f"sold in the last 24 months" if orc_total else "not available on this page load"),
        source_type=PLACEHOLDER,
        source_detail=(
            "Shipped as a ranking term, then demoted to a filter + badge three commits later, once "
            "two problems became clear from the buildings that actually landed at the top of the "
            "board: (1) COVERAGE -- only ~2% of retrofit_buildings show a sale in the last 24 "
            "months (measured 2026-08-16, live count above), so weighting it board-wide meant a "
            "sparse column was doing most of the sorting for a large share of top-ranked rows, the "
            "same false-precision failure mode EBEWE's own coverage flag exists to name; and (2) "
            "VERIFICATION -- the Assessor's RecordingDate is a Prop 13 reassessment trigger, not a "
            "sale record: it fires on trust and family transfers and reassessment-triggering "
            "refinances as well as arms-length sales, and this system cannot currently tell those "
            "apart (no document type is available -- see the source note below). A column that "
            "fires on a refinance is not verified enough evidence to re-sort a board this many "
            "people read. Set to weight=0.0 in config.yaml (retrofit.ownership_recency_weight) -- "
            "app.pipeline.retrofit:rank_buildings borrows proportionally from magnitude/size only "
            "when this is above 0.0, so 0.0 restores the exact pre-recency ranking. Surfaced instead "
            "as /retrofit's 'Sold in last 24 months' filter and a per-row badge (LA County Assessor's "
            "RecordingDate, with the same trust/family/refinance caveat in its hover text) -- the "
            "same treatment EBEWE's own sparse column got. The underlying source itself (LA County "
            "Recorder deed records have no bulk/API access, confirmed 2026-08-16 -- an in-person "
            "visit or a paid per-document VitalChek request is the only path, same dead end as DIR; "
            "the Assessor's own RecordingDate field is used instead, not a workaround) is unchanged "
            "and still disclosed in full on the /retrofit filter callout."
        ),
        verified=True,
        last_reviewed="Live count as of this page load; weight set to 0.0 2026-08-16.",
    ))

    from app.pipeline.retrofit import BASE_MAGNITUDE_WEIGHT, BASE_SIZE_WEIGHT, RECENCY_HALFLIFE_MONTHS
    out.append(Assumption(
        group="Retrofit ranking", name="Ownership-change recency: decay half-life (if re-enabled)",
        config_path=None,
        value=f"{RECENCY_HALFLIFE_MONTHS:.0f}-month half-life; a re-enabled weight of W would borrow "
             f"proportionally from magnitude ({BASE_MAGNITUDE_WEIGHT}) and size ({BASE_SIZE_WEIGHT})",
        source_type=PLACEHOLDER,
        source_detail=(
            "Kept for the record now that the weight defaults to 0.0 (see the sibling entry above for "
            "why) -- this is the shape a re-enabled term would take, not something currently applied "
            "to the board. A judgment call, not fit to any labeled outcome -- no data exists linking "
            "a sale date to an actual subsequent equipment replacement to fit against. The original "
            "framing (new owners run capital plans and price deferred HVAC into offers on roughly a "
            "6-18 month lag) motivated a SHORT half-life so the term would concentrate weight on "
            "recent sales rather than still crediting one from a decade ago, but 24 months "
            "specifically is a round-number choice. Reuses the same exponential-decay shape "
            "scoring.recency_halflife_days already uses for signal recency, for consistency rather "
            "than inventing a new curve. Measured once at weight=0.22 (2026-08-16, before demotion): "
            "reordered the entire top of the board -- only 11 of the top 200 buildings survived from "
            "the pre-recency ranking -- because most already-overdue buildings cluster in a narrow "
            "years-past band where a full recency credit dominates the much smaller remaining "
            "magnitude spread. Worth knowing before choosing a re-enable value: 0.22 read as "
            "aggressive in practice, not just in principle.",
        ),
    ))

    from app.portfolios import PORTFOLIO_DATE_WINDOW_DAYS, PORTFOLIO_MAX_GROUP_SIZE, PORTFOLIO_RADIUS_MILES
    pfc = portfolio_coverage or {}
    buildings_grouped = pfc.get("buildings_grouped")
    n_groups = pfc.get("distinct_groups")
    n_same_block = pfc.get("groups_same_block")
    n_multi_block = pfc.get("groups_multi_block")
    out.append(Assumption(
        group="Retrofit ranking", name="Portfolio-transaction detection",
        config_path=None,
        value=(f"window={PORTFOLIO_DATE_WINDOW_DAYS}d, radius={PORTFOLIO_RADIUS_MILES}mi, "
              f"max group size={PORTFOLIO_MAX_GROUP_SIZE} — {buildings_grouped:,} buildings in "
              f"{n_groups:,} groups ({n_same_block:,} same-block/parcel-split, "
              f"{n_multi_block:,} multi-block/candidate-transaction)" if buildings_grouped is not None else
              f"window={PORTFOLIO_DATE_WINDOW_DAYS}d, radius={PORTFOLIO_RADIUS_MILES}mi, "
              f"max group size={PORTFOLIO_MAX_GROUP_SIZE} (not available on this page load)"),
        source_type=PLACEHOLDER,
        source_detail=(
            "Buildings sharing a recording date within a short window AND close geographic "
            "proximity are flagged as one transaction, not independent leads. Calibrated "
            "2026-08-17 against one real, confirmed example (five adjacent S Azusa Ave parcels in "
            "City of Industry, sold within an 11-day window, three of which the user identified "
            "directly) -- window and radius clear that example's measured gaps (max 0.149mi, 11 "
            "days) with margin, but are not fit against a labeled set of confirmed vs. rejected "
            "portfolios; no such set exists. PORTFOLIO_MAX_GROUP_SIZE exists because the FIRST run "
            "against real production data found a 44-building group -- an entire Glendale "
            "industrial park (Hazel St/Flower St/Paula Ave/Circle Seven Dr) all sharing the exact "
            "same 1997-06-17 recording date, far more consistent with a mass administrative event "
            "(a subdivision map recorded in one filing, or a bulk reassessment) than a single "
            "buyer's simultaneous purchase. Groups larger than the cap are excluded entirely "
            "(members revert to standalone rows) rather than truncated, so a shown group's "
            "membership is never partial.\n\n"
            "SPOT-CHECKED 2026-08-19 against a random sample of 20 of the (at the time) 5,718 "
            "groups, pulled with addresses/recording dates/sqft for each member: 17 of 20 (85%) "
            "read as one PHYSICAL PROPERTY recorded as multiple assessor parcels, not one buyer "
            "acquiring several distinct buildings -- the strongest tell, present in most of them, "
            "is members sharing the same APN book/page prefix (e.g. all of 3110-007-xxx) and often "
            "adjacent or identical street addresses. 1 of 20 read as a probable FALSE grouping: two "
            "unrelated properties on different streets, different assessor blocks, sale dates 14 "
            "days apart (the edge of the window) and wildly different sizes (5,000 vs 154,133 "
            "sqft) -- plausibly just close enough in space and time to trip the detector by "
            "coincidence. The remaining 2 of 20 were the only ones consistent with a real small "
            "multi-property buy: same street, same date, but different assessor blocks. Net: the "
            "original \"Portfolio: N buildings\" framing shown to a rep overstated what this "
            "feature mostly finds.\n\n"
            "MADE AUTOMATIC 2026-08-19, not left as a one-time spot-check: RetrofitBuilding."
            "portfolio_same_block (app/portfolios.py's APN_BLOCK_PREFIX_LEN=7, the same book/page "
            "slice the spot-check used by hand) is now computed live for every group on every "
            "rebuild -- True when every member shares an APN book/page (one property, multiple "
            "parcels), False when a group spans more than one block (a candidate genuine "
            "multi-property transaction). The board now shows two different labels accordingly: "
            "\"Co-recorded: N parcels\" (same block) vs \"Portfolio candidate: N buildings\" "
            "(multi-block) -- see retrofit_board.html. groups_same_block/groups_multi_block above "
            "are the live, current split, not the 20-group sample; both are worth checking against "
            "each other before trusting either in isolation. A detected group remains probabilistic "
            "evidence of one co-recorded transaction either way -- same_block=False raises the odds "
            "it is a real multi-property buy, it does not confirm one. Every group is still shown "
            "with its member addresses so a rep can judge it directly rather than trust the label.\n\n"
            "Same underlying limitation as ownership-change recency itself (see the sibling entry "
            "above): the Assessor's RecordingDate is not proof of an arms-length sale. Only runs "
            "among buildings with both a recording date AND a geocode. Confirmed 2026-08-18: "
            "replacement_candidate is ~96.4% geocoded; recently_active was 0% geocoded through "
            "2026-08-17 (buildings there were never passed through the geocoder -- a pipeline gap "
            "upstream of this feature, not a portfolio-detection bug), fixed 2026-08-18 -- "
            "recently_active is now ~94.7% geocoded and portfolio detection runs there too.",
        ),
        verified=True,
        last_reviewed="Live count as of this page load.",
    ))

    # ---- Contractor ranking --------------------------------------------------
    from app.contractors import URGENCY_YEARS_PAST_CAP, default_radius_miles, ranking_radius_miles

    out.append(Assumption(
        group="Contractor ranking", name="Ranking radius",
        config_path="contractors.ranking_radius_miles",
        value=f"{ranking_radius_miles(cfg):.0f} miles",
        source_type=PLACEHOLDER,
        source_detail="Distinct from contractors.default_radius_miles (15mi, the per-building 'nearest "
                      "contractors' dispatch list -- deliberately wide, a different question). "
                      "ranking_radius_miles exists because raw proximity count at 15mi didn't discriminate "
                      "at all (measured 2026-08-15: top 10 spanned 1,430-1,456, under 2%) -- in a dense "
                      "metro pocket, every contractor's 15mi catchment overlaps almost entirely with its "
                      "neighbors', so any sum-based aggregate over nearly-identical inputs converges "
                      "regardless of what it weights. Tested 1-5mi: spread ranged 18-29% at 1-3mi vs "
                      "1.4-10.8% at 5mi+. 3mi was picked as the tightest radius that still keeps a "
                      "substantial per-contractor sample (~135-380 buildings for the top contractors "
                      "tested) -- a judgment call about that tradeoff, not a further-optimized or "
                      "statistically derived value, so this stays a placeholder despite being informed by "
                      "real measurement.",
        numeric_value=ranking_radius_miles(cfg), plausible_low=1, plausible_high=5,
    ))
    out.append(Assumption(
        group="Contractor ranking", name="Urgency weighting formula",
        config_path=None,
        value=f"Σ clip(service_life_years_past, 0, {URGENCY_YEARS_PAST_CAP:.0f}) over nearby buildings; "
             f"Σ estimated-tons midpoint as tie-breaker only",
        source_type=PLACEHOLDER,
        source_detail="Hardcoded in app.contractors (_urgency_weight/_tons_mid), not config.yaml -- a "
                      "judgment call about HOW to aggregate, not a measured relationship between years-past "
                      "and actual callability. Deliberately mirrors app.pipeline.retrofit:rank_buildings' "
                      "own service_life_years_past cap (100yr) rather than inventing a second definition of "
                      "\"urgent\", and deliberately keeps tonnage a tie-breaker only (added only after "
                      "urgency ties), same discipline rank_buildings itself uses for building size: never "
                      "allowed to buy back urgency. Summing per-building years-past (rather than, say, "
                      "averaging, or counting only buildings past some threshold) was chosen because it's "
                      "the simplest formula that satisfies the one concrete test case this was built "
                      "against -- a small severely-overdue cluster should outrank a larger merely-old one "
                      "-- not validated against actual booked/lost deals, since none exist yet for this "
                      "board.",
    ))

    # ---- Replacement leads (owner-direct lane) -------------------------------

    out.append(Assumption(
        group="Replacement leads", name="Overdue-count radius",
        config_path=None,
        value=f"{default_radius_miles(cfg):.0f} miles for the COUNT (contractors.default_radius_miles) "
             f"-- deliberately NOT {ranking_radius_miles(cfg):.0f}mi, the radius the RANKING "
             f"(nearby_urgency_score) uses",
        source_type=MEASURED,
        source_detail="First version of this board used ranking_radius_miles (3mi) for the overdue "
                      "count too, on the reasoning that a wider radius makes cross-contractor "
                      "aggregates stop discriminating (the same problem Contractor ranking's own "
                      "'Ranking radius' entry documents) -- true for RANKING, but counting is a "
                      "different question. Measured against production: at 3mi (2026-08-24), 2,861 of "
                      "5,429 mechanical contractors (53%) showed zero overdue buildings nearby; at "
                      "15mi (2026-08-25, same contractors, same underlying data), that fell to 1,913 "
                      "(35%) -- roughly 950 contractors went from a real zero to a real, nonzero list "
                      "purely from widening the radius, confirming the tight radius was hiding genuine "
                      "prospects in less-dense territory, not just correctly reporting an empty one. "
                      "35% still show zero even at 15mi -- a real floor, not a radius artifact. "
                      "Ranking itself does not reorder: nearby_urgency_score stays at "
                      "ranking_radius_miles regardless (see app.contractors.replacement_leads), and "
                      "the top 20 contractors by urgency are byte-identical, same order, before and "
                      "after this change -- confirmed by comparing the top-100-by-urgency band against "
                      "the ~970 contractors that newly cleared min_overdue at 15mi: zero of them fall "
                      "in the top 1,000 by urgency, all land in the bottom half. Widening the count "
                      "radius changes who's ELIGIBLE for the list, never who's ranked highest on it. "
                      "A real cost: a dense-area contractor's count at 15mi can run into the thousands "
                      "(observed max 13,623), and the batched precompute takes several minutes against "
                      "the mechanical-only set -- acceptable now that it runs on a schedule (see "
                      "'match_contractors_overdue' in config.yaml's sources: block) rather than inside "
                      "a page request, which is what blocked 15mi in the first draft.",
        verified=True,
        last_reviewed="Measured 2026-08-24 (3mi) and 2026-08-25 (15mi) against real production data.",
    ))
    out.append(Assumption(
        group="Replacement leads", name="Minimum overdue buildings to list a contractor",
        config_path=None,
        value="5",
        source_type=PLACEHOLDER,
        source_detail="A judgment call about what's worth handing a rep, not a measured conversion "
                      "threshold -- no booked/lost outcome data exists yet for this board to fit "
                      "against. Set low enough not to hide a real, thin-but-real opportunity, high "
                      "enough that 'the list' means more than a couple of addresses. See "
                      "/replacement-leads' own distribution disclosure for how many contractors clear "
                      "this bar vs. lower ones -- if that distribution is thin, this number should move, "
                      "not be trusted as calibrated.",
    ))

    # ---- Competitor line card map --------------------------------------------

    from app.competitors import COMPETITOR_LINES, REP_FIRMS
    _confirmed = sum(1 for row in COMPETITOR_LINES if row[4] == "confirmed")
    _unconfirmed = sum(1 for row in COMPETITOR_LINES if row[4] == "unconfirmed")
    out.append(Assumption(
        group="Competitor line card map", name="Competitor line card map: coverage and confirmed vs. unconfirmed",
        config_path=None,
        value=f"{len(REP_FIRMS)} rep firms (+ Trane, factory-direct), {len(COMPETITOR_LINES)} line "
             f"assignments — {_confirmed} confirmed, {_unconfirmed} unconfirmed",
        source_type=MEASURED,
        source_detail=(
            "Every row sourced ONLY from that rep firm's own published line card page, or (for "
            "Greenheck) the manufacturer's own live 'find a rep' locator -- never a third-party "
            "directory, never a guess from reputation. Compliance checked before any fetch, "
            "2026-08-16: all seven rep-firm sites' robots.txt permit the pages read (standard "
            "WordPress/Squarespace admin/search/config paths only; wrightsales.net has no robots.txt "
            "at all, the default-allow case). Full research method, per-line role-classification "
            "reasoning, and both kinds of 'unconfirmed' (a genuine source conflict on Greenheck; "
            "four lines -- Twin City Fan, Panasonic, Soler & Palau, Airzone -- that also sit on "
            "DMG's OWN line card for the identical role, discovered by cross-checking manufacturer "
            "names against the seeded ProductLine table, not something this research set out to "
            "find) are documented in app/competitors.py's module docstring, not repeated here. 33 "
            "of the 66 rows carry no building_role -- accessories, tools, and components a rep firm "
            "also carries alongside its real equipment lines (gauges, brazing alloys, lineset "
            "covers, relays, insulation) that map onto none of the 13 roles -- recorded as real, "
            "sourced facts anyway, just excluded from the per-role competitive surfacing on "
            "/project/{id} and /line/{id}. Trane recorded factory-direct (2 rows, cooling_generation "
            "and air_handling) on an absence basis: no independent rep firm's card checked here "
            "lists Trane, and Trane operates its own branded Commercial Sales Office pages across "
            "California rather than routing through a third party -- disclosed as absence-based, "
            "not a positive locator-tool statement the way Greenheck's finding is."
        ),
        verified=True,
        last_reviewed="Researched and seeded 2026-08-16 (scout seed-competitors).",
    ))
    out.append(Assumption(
        group="Competitor line card map", name="Manual-reload staleness threshold",
        config_path="sources.competitor_lines.stale_hours",
        value=f"{cfg.get('sources.competitor_lines.stale_hours', 2160):.0f} hours "
             f"({cfg.get('sources.competitor_lines.stale_hours', 2160)/24:.0f} days)",
        source_type=PLACEHOLDER,
        source_detail="A judgment call, not a measured cadence -- unlike the EBEWE benchmarking "
                      "group's 'Weekly-fetch staleness threshold' entry (216h, derived from a real "
                      "Sundays-only automated schedule), this source has no fetch schedule at all to "
                      "measure: it's a hand-researched dataset with no run history, reloaded only "
                      "when someone re-researches the rep-firm line cards by hand (`scout "
                      "seed-competitors`). Same reasoning and same number as "
                      "sources.hcai_seismic_ratings.stale_hours (see the 'Hospital seismic "
                      "compliance' group's 'Manual-import staleness threshold' entry): every other "
                      "source in config.yaml's sources: block defaults to a 36-hour staleness window "
                      "(app.ops.doctor), which would flag a hand-maintained dataset as stale within "
                      "two days of any realistic manual re-research cadence. 90 days is long enough "
                      "not to nag on a dataset that only moves when someone re-does the research, "
                      "short enough to still catch a genuinely abandoned one.",
    ))

    # ---- Line card branch coverage -------------------------------------------

    from app.normalize import normalize_name
    _branch_cards = cfg.get("accounts.line_card_branches", []) or []
    _line_card_names = [e["name"] for e in cfg.get("accounts.line_card", []) or []]
    _line_card_total = len(_line_card_names)
    _covered_anywhere_norm = {
        normalize_name(n) for card in _branch_cards for n in card.get("covered", [])
    }
    _lines_on_any_card = sum(1 for n in _line_card_names if normalize_name(n) in _covered_anywhere_norm)
    _branches_with_a_card = [c["branch"] for c in _branch_cards]
    out.append(Assumption(
        group="Line card branch coverage",
        name="Which DMG/ToroAire branch actually carries each line",
        config_path="accounts.line_card_branches",
        value=f"{len(_branches_with_a_card)} of 8 DMG offices have a card on file; {_lines_on_any_card} "
             f"of {_line_card_total} lines appear on at least one of them, "
             f"{_line_card_total - _lines_on_any_card} appear on none",
        source_type=MEASURED,
        source_detail=(
            "Every field on the line card itself (category, tier, markets, eligibility) was "
            "researched against DMG's Southern California card -- see SOCAL_CARD_DISCLOSURE in "
            "app/accounts.py, surfaced on /lines, /line/{id}, a project's line-card-fit table, the "
            "pre-call brief payload, and the hospital board/brief/building pages. Which DMG/ToroAire "
            "branch actually carries a line is a separate fact, tracked in ProductLineBranch, and "
            "absence of a row means unknown -- never inferred as either covered or not covered. "
            "Seeded 2026-08-20 from Andy Tripicchio's 'Line Cards' email (sent 2026-08-20), which "
            "attached all 8 DMG offices' own line-card PDFs; 5 had a card attached -- "
            f"{', '.join(_branches_with_a_card)} -- saved verbatim under docs/line-cards/. Bay Area, "
            "Sacramento, and Reno had no card supplied and get zero rows, so every line stays unknown "
            "at those three. For each of the 5 branches with a card, every DMG- or both-firm line gets a "
            "row -- confirmed_covered if printed on that branch's card, confirmed_not_covered if the card "
            "exists and the line is not on it (a one-page DMG line card is a complete listing of what the "
            "branch carries under the DMG name, so an omission is read as evidence of non-coverage for a "
            "DMG/both line -- an inference from an enumerated first-party document, not a bare guess, and "
            "every confirmed_not_covered row's source_detail says so explicitly). A ToroAire-firm line "
            "only gets a row where it actually appears on a card: a DMG-branded card's silence about a "
            "ToroAire product is not evidence about ToroAire's own, separate card, which this seed has no "
            "copy of -- 20 of the 21 lines an earlier pass at this data marked 'confirmed on no card' "
            "turned out to be exactly this case, discovered by cross-checking against config.yaml's own "
            "`firm` field, and were corrected to unknown rather than confirmed_not_covered (Titus among "
            "them -- it never appears on any of the 5 DMG cards, and is ToroAire-firm, so it stays "
            "unknown at every branch, not confirmed absent). verified=True on every row: each comes "
            "directly from reading the branch's own published card, a first-party primary source, not a "
            "verbal recap. An earlier 2026-08-19 seed built from a verbal recap instead of the actual "
            "cards was wrong in a different way -- it guessed Marley/Recold backwards for Hawaii -- and "
            "was discarded in favor of this one. See config.yaml's accounts.line_card_branches "
            "entries for each branch's transcribed `covered` list and PDF filename/revision date."
        ),
        verified=True,
        last_reviewed="2026-08-20, read directly from each branch's own line-card PDF.",
    ))

    # ---- Union signatory (UA Local 250) --------------------------------------

    from app.pipeline.local250 import NAME_ONLY_THRESHOLD, NAME_WITH_CITY_THRESHOLD
    out.append(Assumption(
        group="Union signatory (UA Local 250)", name="UA Local 250 signatory join method and match rate",
        config_path=None,
        value="Name-fuzzy + city-corroboration join: 47 of 108 listed contractors matched (44%), "
             "4 excluded as ambiguous, 57 unmatched",
        source_type=MEASURED,
        source_detail=(
            "Measured 2026-08-16 against real production CSLB data (47,572 contractors in scope). "
            "UA Local 250's own public signatory list (socalhvacr.info/contractors) carries no "
            "license number, so the join is by company name (rapidfuzz token_sort_ratio on a "
            "legal-suffix-stripped name -- deliberately NOT app.normalize.normalize_name, which "
            "is tuned for project/SPE names and was caught silently reducing the real company "
            "\"Building Aire Inc.\" to an empty string, spuriously tying it against every other "
            "short/coded CSLB name -- see app/pipeline/local250.py's module comment), with city "
            "agreement as corroboration that lowers the acceptance bar. Multiple CSLB candidates "
            "tying at the same score are excluded as ambiguous rather than guessed at (4 cases: "
            "e.g. \"Master Cooling Corp.\" matches three distinct CSLB licenses named Master "
            "Cooling/Master Cooling Corp across two cities). Spot-checking the closest unmatched "
            "near-misses (0.91-0.95 similarity, just under threshold) shows several look like real "
            "matches missed on abbreviation/word-order differences or a different city on file "
            "(\"Critchfield Mechanical Inc. of So. Calif.\" in Irvine vs CSLB's \"Critchfield "
            "Mechanical Inc of Southern California\" in Huntington Beach, 86% similarity) -- but "
            "lowering the threshold to catch those also pulls in genuinely different companies at "
            "a similar or higher score (\"Brymax Construction Services\" vs \"Aramax Construction "
            "Services\", 93%). No single cutoff cleanly separates the two in this sample, so 44% "
            "is a FLOOR on the true match rate, not the true rate itself -- reported honestly "
            "rather than tuned past what unlabeled data can justify."
        ),
        verified=True,
        last_reviewed="Measured 2026-08-16 against live production Contractor data.",
    ))
    out.append(Assumption(
        group="Union signatory (UA Local 250)", name="Fuzzy match thresholds",
        config_path=None,
        value=f"{NAME_ONLY_THRESHOLD:.0f}/100 name-only, {NAME_WITH_CITY_THRESHOLD:.0f}/100 with city agreement",
        source_type=PLACEHOLDER,
        source_detail="A judgment call, not measured against labeled true/false outcomes -- no "
                      "confirmed-signatory ground truth exists to fit against. Set high because this "
                      "flag is written directly with no human review step, unlike app.voice_match's "
                      "MATCH_THRESHOLD=60, which only ever feeds a human-picked suggestion list. City "
                      "agreement is treated as real corroboration and earns a lower bar, but is not "
                      "reliably present even for genuine matches (see the sibling entry's spot-check) "
                      "-- a company can be listed at a different address on the two sources without "
                      "being a different company.",
    ))

    # ---- EBEWE benchmarking -------------------------------------------------

    out.append(Assumption(
        group="EBEWE benchmarking", name="Benchmark-to-building join method and coverage",
        config_path=None,
        value="Address-text join (normalize_address), ain_last3-checksum-confirmed: 4,790 buildings "
             "matched, of 11,497 EBEWE buildings that have ever filed and 60,166 total retrofit_buildings "
             "rows (~8% board-wide coverage)",
        source_type=MEASURED,
        source_detail=(
            "Measured 2026-08-16 against real production data before this shipped, per the user's explicit "
            "gate that nothing feeds a rank score until the join is verified. EBEWE's own `apn` field, which "
            "Socrata itself labels \"AIN\", is NOT usable as a join key -- every one of 96,211 rows is "
            "exactly 3 characters; the dataset's own description confirms it's \"the last 3 digits of the "
            "AIN\", not the AIN. Two real join strategies were measured and compared: (1) "
            "app.pipeline.retrofit:normalize_address address-text matching -- 4,983 clean 1:1 matches (both "
            "directions of ambiguity excluded: 113 EBEWE addresses hit >1 retrofit apn, 40 retrofit apns hit "
            ">1 EBEWE building_id, neither guessed at); (2) US Census batch-geocoding both sides and matching "
            "spatially (nearest-neighbor, grid-indexed) -- WORSE, not better: 2,740 clean matches at the "
            "best threshold tested (15m; 25m and 40m did worse still, 2,087 and 1,578 -- LA's parcels are "
            "dense enough that independent geocoding runs on each side create MORE ambiguity at wider "
            "radii, not less). The geocoded matches were 94.8% consistent with the text join where both "
            "found a match, and added only 255 new matches text alone missed -- the union of both methods "
            "(5,238) barely exceeds text alone. Conclusion: normalize_address was not the limiter; if "
            "anything it outperforms independent geocoding here. Shipped: text join alone, tightened further "
            "by ain_last3 as a free independent checksum (matched apn's own last 3 digits vs EBEWE's "
            "documented fragment) -- 193 of 4,983 (3.9%) disagreed and were dropped rather than trusted, "
            "for a final 4,790. See app/pipeline/ebewe.py's module docstring for the full method and "
            "app.pipeline.ebewe:ebewe_matches_by_normalized_address for the code. Reference point: the "
            "assessor-derived ebewe_candidate sqft-threshold SCOPE proxy (not measured performance) sits at "
            "18,301 true rows as of this measurement -- EBEWE's own universe of 11,497 ever-filed buildings "
            "cannot reach that regardless of join quality (real non-compliance, plus the proxy itself being "
            "inflated with non-LA-city parcels; see AssessorCandidate's docstring), so the shortfall is "
            "substantially real, not primarily a join defect. Even a hypothetically perfect join tops out "
            "around 11,497 of ~53,000 replacement-candidate rows, roughly 22% -- this is why "
            "RetrofitBuilding.ebewe_matched/energy figures are NOT a rank_buildings() term: a column this "
            "sparse blended board-wide is exactly the false-precision problem this register exists to flag. "
            "Used only as an in-subset tie-breaker on the /retrofit?has_ebewe=true view (sort: rank_score "
            "DESC, then ebewe_energy_star_score ASC -- a worse ENERGY STAR score breaks a tie toward the top "
            "only among buildings that already have EBEWE data), never board-wide. Coverage % is disclosed "
            "on the board itself, the same way the replacement-candidate population's false-positive "
            "direction already is."
        ),
        verified=True,
        last_reviewed="Measured 2026-08-16 against live production retrofit_buildings addresses.",
    ))

    out.append(Assumption(
        group="EBEWE benchmarking", name="A/RCx audit compliance cycle (LAMC Table 9708.2)",
        config_path=None,
        value="5-year cycle keyed to last digit of LADBS Building ID: 0/1->Dec 1 2021+5n, 2/3->2022+5n, "
             "4/5->2023+5n, 6/7->2024+5n, 8/9->2025+5n",
        source_type=RULE_OF_THUMB,
        source_detail=(
            "Not a judgment call like every other row in this register -- a verified legal compliance "
            "schedule, transcribed exactly from LADBS's own \"Audits and Retro-Commissioning FAQs\" PDF "
            "(dbs.lacity.gov/sites/default/files/efs/forms/pc17/EBEWE-ARCx-FAQs-FINAL.pdf, Last Updated "
            "04/30/2026), FAQ #2, which reproduces LAMC Table 9708.2 directly, cross-checked against that "
            "same document's FAQ #1 prose (\"the next A/RCx compliance due date for Building IDs ending in "
            "0 or 1 is still December 1, 2026... ending in 2 or 3 is still December 1, 2027\") -- both "
            "consistent with the table (2021+5=2026, 2022+5=2027). Listed in this register anyway, per "
            "instruction, as a new constant this task introduced -- see "
            "app.pipeline.regulatory:arcx_compliance_status and ARCX_INITIAL_COMPLIANCE_YEAR_BY_LAST_DIGIT. "
            "Only ever computed for a building that already has an ebewe_matched row (it needs a real LADBS "
            "Building ID, which only a successful EBEWE join provides) -- shown as its own flag "
            "(ebewe_arcx_due_this_year) regardless of the benchmark-data join's coverage, since a dated "
            "legal obligation is useful information on its own, independent of whether it feeds a score."
        ),
        verified=True,
        last_reviewed="Verified 2026-08-16 against LADBS's own FAQ PDF, cross-checked two ways within the "
                      "same document.",
    ))

    out.append(Assumption(
        group="EBEWE benchmarking", name="Weekly-fetch staleness threshold",
        config_path="sources.la_ebewe_benchmarking.stale_hours",
        value=f"{cfg.get('sources.la_ebewe_benchmarking.stale_hours', 36):.0f} hours "
             f"({cfg.get('sources.la_ebewe_benchmarking.stale_hours', 36)/24:.1f} days) -- same value on "
             f"sources.ua_local_250.stale_hours and sources.la_county_ownership.stale_hours",
        source_type=MEASURED,
        source_detail=(
            "Found and fixed 2026-08-19, starting from a digest false-positive on la_ebewe_benchmarking: "
            "this fetch runs WEEKLY, Sundays only (app.cli's pipeline command, RETROFIT_WEEKLY_WEEKDAY), "
            "but neither it nor app.ops.doctor's per-source stale_hours override existed for it, so both "
            "`scout doctor` and the daily digest's own staleness check used the 36-hour default -- "
            "flagging a source that runs cleanly every week as failing on 6 of every 7 days. Confirmed "
            "against the real run history: exactly one SourceRun ever recorded, 2026-08-16 (a Sunday), "
            "ok=True, 96,211 records imported -- matching this fetch's own docstring exactly, zero "
            "errors, not a real failure at all. Checking the other two sources scheduled the same day "
            "(app.cli's pipeline command runs fetch_ebewe_benchmarks_cmd, fetch_local250_cmd, and "
            "fetch_ownership_recency_cmd together, Sundays only) found the identical gap on both: "
            "ua_local_250 was 62.3h past its last (successful) run when checked, la_county_ownership "
            "61.3h -- same missing override, same false-positive shape, fixed the same way for all "
            "three rather than leaving two of them to surface the same false alarm on a later day. "
            "216 hours (9 days) is one full weekly cycle (168h) plus a day and a half of slack for a "
            "legitimately late run, not a pure judgment call the way hcai_seismic_ratings' 90-day "
            "threshold is (see the sibling entry above) -- this one is close to a measured value, "
            "since the cadence itself is a scheduled fact, not a guess. Separately: the digest's own "
            "check (app.pipeline.notify._stale_sources) had ALSO hardcoded 36 hours independently of "
            "app.ops.doctor's override mechanism -- the two could disagree about the same source's "
            "health. Both now call one shared function, app.ops.stale_cutoff, so they can't drift "
            "apart again the way they already had."
        ),
        verified=True,
        last_reviewed="2026-08-19, against the real run history of all three weekly sources.",
    ))

    # ---- AB 802 statewide benchmarking ---------------------------------------

    out.append(Assumption(
        group="AB 802 statewide benchmarking", name="Access classification (Phase A research)",
        config_path=None,
        value="robots.txt-clean, public domain, no automated-access restriction -- same statewide "
             "ca.gov Conditions of Use CAEATFA already relies on",
        source_type=MEASURED,
        source_detail=(
            "Checked directly 2026-09-06 before any fetch was built. energy.ca.gov/robots.txt "
            "disallows /admin/, /comment/reply/, /filter/tips, /node/add/, /search/, /search?, "
            "/user/{register,password,login,logout}, /media/oembed -- NOT /media/{id} (the page "
            "linking each year's file) and NOT /sites/default/files/ (where the actual xlsx lives), "
            "so both the discovery page and the direct file download are robots.txt-clear. Site-wide "
            "terms: energy.ca.gov is a ca.gov/CDT property under the same statewide Conditions of Use "
            "CAEATFA's own entry already quotes verbatim (ca.gov/legal/conditions-of-use/, fetched "
            "directly, word-for-word identical text) -- 'information presented on this website...is "
            "considered in the public domain. It may be distributed or copied as permitted by law,' "
            "with the only 'unauthorized use' language scoped to defeating security controls, not "
            "automated or bulk access. No CEC-specific terms page distinct from this statewide policy "
            "was found. See app/pipeline/ab802.py's module docstring for the full citation."
        ),
        verified=True,
        last_reviewed="Checked 2026-09-06 directly against energy.ca.gov/robots.txt and the ca.gov "
                      "Conditions of Use page.",
    ))

    out.append(Assumption(
        group="AB 802 statewide benchmarking", name="Assessor/EBEWE join method and measured rate",
        config_path=None,
        value="Lat/long (30m) then normalized-address, LA County only: 2,843 of 6,659 (42.7%) matched "
             "for 2023, 2,728 of 6,437 (42.4%) for 2024 -- address text accounts for the large majority "
             "of matches in both years (2,529 of 2,843 in 2023; 2,437 of 2,728 in 2024)",
        source_type=MEASURED,
        source_detail=(
            "Measured 2026-09-07 as a dry run against real production RetrofitBuilding/EbeweBenchmark "
            "data before this shipped -- transient Ab802Building objects run through the real "
            "match_to_retrofit/match_to_ebewe functions, never added or committed to the database, so "
            "this measurement wrote nothing. Lat/long is tried first (RetrofitBuilding.latitude/"
            "longitude, itself rejoined from RetrofitGeocode -- see that model's docstring) because, "
            "unlike EBEWE's own join (app/pipeline/ebewe.py's module docstring), neither side needed a "
            "NEW geocoding run here: AB 802's own Portfolio Manager coordinates are already in the "
            "file, and RetrofitGeocode's Census geocoding is already built. The result was still "
            "address-dominated in practice, not lat/long-dominated as that reasoning alone might "
            "suggest: of 2,843 matches in 2023, only 314 (11%) were lat/long, the remaining 2,529 (89%) "
            "fell back to normalized_address -- 2024 was the same shape (291 of 2,728, 11%). The most "
            "likely explanation, consistent with EBEWE's own finding that independent geocoding "
            "introduces jitter in dense LA parcels: RetrofitGeocode's coverage is partial (null until a "
            "building has actually been geocoded), and even where both sides ARE geocoded, two "
            "independently-produced coordinate pairs for the same real building (rooftop centroid vs. "
            "address point, different geocoders) can easily disagree by more than 30m, while the "
            "address text itself does not depend on either geocoder agreeing. Recorded here rather "
            "than assumed: lat/long was tried first on sound reasoning, but this is the second time in "
            "this codebase normalized-address text has outperformed a coordinate-based join for this "
            "exact LA-parcel-density problem -- worth remembering before reaching for geocoding as the "
            "default answer to a future join like this one. Both directions of ambiguity are excluded "
            "throughout (a retrofit building or AB 802 row claimed by more than one match on the other "
            "side is dropped, never guessed at), same discipline as app.pipeline.ebewe's own join. "
            "Benchmarking-filer (EbeweBenchmark.organization, address-only match) coverage was 1,657 of "
            "6,659 (24.9%) for 2023 and 1,711 of 6,437 (26.6%) for 2024 -- see Ab802Building's own "
            "docstring for why this is never labeled 'owner': no free, bulk-queryable LA County "
            "assessor owner-name source exists at all (RetrofitBuilding's own verified finding), so "
            "every row outside this filer match shows 'no owner data available,' permanently, not "
            "'not yet resolved.'"
        ),
        verified=True,
        last_reviewed="Measured 2026-09-07 against live production RetrofitBuilding/EbeweBenchmark data.",
    ))

    out.append(Assumption(
        group="AB 802 statewide benchmarking", name="AB 802 owner hints",
        config_path="ab802_owner_hints.tokens",
        value="13 config-driven rules (12 plain-substring, 1 regex), first match wins -- see "
             "config.yaml's ab802_owner_hints for the editable list",
        source_type=MEASURED,
        source_detail=(
            "The one regex rule (`^(LAX|INE|SDG|ONT)\\d{5}` -> Prologis, Prologis's own internal "
            "facility-code prefix convention) was verified 2026-09-08, per the user's explicit "
            "instruction to check three real matches against Prologis's own public property search "
            "before shipping the rule, or drop it if it didn't hold. Three in-territory property names "
            "matching the pattern were checked:\n"
            "  1. 'lax10201 - van nuys dis ctr 1', 16640 Stagg Street, Los Angeles (Van Nuys), CA 91406 "
            "-- confirmed directly: PropertyShark lists the parcel (APN 2205-009-078) as owned by "
            "'Prologis, L.P.', mailing address 2141 Rosecrans Avenue Suite #1151, El Segundo, CA -- "
            "Prologis's own known Southern California office address.\n"
            "  2. 'lax07412 - commerce business park 12', 5636 East 61st Street, Commerce, CA 90040 -- "
            "confirmed by pattern family: Prologis's own property-search site (prologis.com/"
            "industrial-properties/building/lax07401-...) publishes 'Commerce Business Park 1' at code "
            "lax07401, and independently-listed 'Commerce Business Center 10/11' at lax07410/lax07411, "
            "all on the same street/complex as the lax07412 target -- consecutive codes in the same "
            "named complex, not a coincidental pattern match.\n"
            "  3. 'lax06516 - south bay ind ctr 16', 355 West Carob Street, Compton, CA 90220 -- "
            "confirmed by pattern family the same way: Prologis's own site publishes 'South Bay "
            "Industrial Center 52' at code lax06552, and CBRE/PropertyShark/Point2 list multiple other "
            "addresses on the SAME West Carob Street explicitly as 'Prologis South Bay Industrial "
            "Center' (601, 700, 200, 255 West Carob Street) -- the target address sits among these.\n"
            "All three held; the rule shipped as specified rather than being dropped. 265 in-territory "
            "rows (2024) match the code pattern board-wide -- see the board's own per-token count, "
            "computed live at /replacement-leads?view=ab802, not hand-copied here since it changes "
            "every time a new year's file loads. The 12 plain-substring rules (Rexford, BMR-, Kilroy, "
            "Irvine Company, Prologis, Link Logistics, IDI, CenterPoint, Duke, Majestic, Watson Land, "
            "Sares Regis) were specified directly by the user, not independently verified -- same "
            "matching discipline as app.call_target._matches_standards_owner (plain substring, "
            "case-insensitive, first match in list order wins), and same non-claim as every other "
            "owner-facing field on this board: a match is shown as 'owner hint: {name}', never 'owner'."
        ),
        verified=True,
        last_reviewed="Prologis code-pattern rule verified 2026-09-08 against Prologis's own public "
                      "property search and third-party commercial listings for three real addresses.",
    ))

    # ---- OPSC School Facility Program ----------------------------------------

    out.append(Assumption(
        group="OPSC School Facility Program", name="Access classification (Phase A research)",
        config_path=None,
        value="Bulk CSV download only, never datastore_search on a schedule -- CAEATFA-shaped on "
             "the download path used, CHHS-shaped on the API path deliberately avoided",
        source_type=MEASURED,
        source_detail=(
            "Checked 2026-09-06, re-confirmed 2026-09-08, before any fetch was built: "
            "data.ca.gov/robots.txt disallows /api/ and /datastore/* for User-agent: * -- the "
            "same CKAN-standard block CHHS's own robots.txt uses (app/pipeline/hcai.py's module "
            "docstring), which is why this fetcher never calls datastore_search on a schedule "
            "(a single one-off datastore_search call, limit=5, was used only during Phase A "
            "research to read real field names -- never on a recurring schedule). The plain bulk "
            "CSV resource-download URL (.../download/school-facility-program-funding.csv) is NOT "
            "in the disallow list -- only /dataset/activity/*, /dataset/groups/*, "
            "/dataset/showcases/*, and /dataset/*/issues/* are blocked under /dataset/, none of "
            "which match a resource download URL. The same statewide ca.gov Conditions of Use "
            "CAEATFA's own entry already quotes (public domain, no automated-access restriction) "
            "applies here too -- data.ca.gov is a CDT property under the identical policy. See "
            "app/pipeline/opsc.py's module docstring for the full citation."
        ),
        verified=True,
        last_reviewed="Checked 2026-09-06 and re-confirmed 2026-09-08 directly against "
                      "data.ca.gov/robots.txt.",
    ))

    out.append(Assumption(
        group="OPSC School Facility Program", name="Workload-list PDF parse rate",
        config_path=None,
        value="99.4% (New Construction), 99.8% (Modernization) -- measured against the live SAB "
             "workload PDFs before this shipped, comfortably above the 90% floor below which a "
             "program's load is skipped rather than shipped with silent data loss",
        source_type=MEASURED,
        source_detail=(
            "app.pipeline.opsc.parse_workload_text anchors each Application_Number line to the "
            "LAST (rightmost) whole-word California county name preceding it, not the first -- CA "
            "district names frequently repeat their own county's name (e.g. 'Riverside Unified "
            "Riverside John W. North High'), so taking the first match mis-splits district from "
            "school name. Switching from first-match to rightmost-match fixed 38 of 40 initial "
            "failures, raising New Construction from 88.3% to 99.4% and Modernization to 99.8%. "
            "Below WORKLOAD_MIN_PARSE_RATE (0.90) for a given program, that program's load is "
            "skipped entirely (existing opsc_workload rows for it are left untouched) and reported "
            "as a failure rather than loading a silently-degraded table -- the funded dataset "
            "(opsc_projects) ships regardless, since it does not depend on PDF parsing at all."
        ),
        verified=True,
        last_reviewed="Measured 2026-09-08 against the live SAB-NC and SAB-MOD workload PDFs.",
    ))

    # ---- DIR PWC-100 public works registrations ------------------------------

    out.append(Assumption(
        group="DIR PWC-100 public works registrations", name="Access classification (Phase A research)",
        config_path=None,
        value="Blocked entirely, PlanetBids-shaped -- no bulk export, no data.ca.gov mirror, and the "
             "HTML search itself is robots.txt-disallowed in full. Not built; PRA request only.",
        source_type=MEASURED,
        source_detail=(
            "Checked 2026-09-07, Phase A research, no code written. No PWC-100 dataset exists on "
            "data.ca.gov or any third-party open-data mirror (searched directly). The PWC-100 award "
            "search and DIR's own public-works contractor-registration lookup both resolve to "
            "services.dir.ca.gov (every www.dir.ca.gov/pwc100ext/* path 307-redirects there, as does "
            "the contractor-lookup link on dir.ca.gov/Public-Works/Contractors.html). That host's own "
            "robots.txt is 'User-agent: *' / 'Disallow: /' -- a full block, verbatim, no exceptions -- "
            "unlike CHHS's own robots.txt (blocks only /api/ and /datastore/*, leaving a bulk file "
            "reachable) or CAEATFA/AB802/OPSC's clean robots.txt plus public-domain ToU. This is the "
            "PlanetBids pattern instead: blocked regardless of method, including a single one-off "
            "sample fetch -- none was made. www.dir.ca.gov itself has no PWC-100-specific robots.txt "
            "disallow, but that host only redirects; it never serves the data itself, so there is no "
            "daylight to fetch through. DIR is also actively retiring PWC-100/PWCR/eCPR for a new "
            "platform (dir.ca.gov/Public-Works/Public-Works-Website-Services.html, no docs live yet), "
            "so this classification should be re-checked before ever revisiting the question, not "
            "assumed to still hold. The only path to this data is a Public Records Act request to DIR "
            "-- not a fetcher, not on any deploy cadence. See RUNBOOK.md's deferred-sources list."
        ),
        verified=True,
        last_reviewed="Checked 2026-09-07 directly against services.dir.ca.gov/robots.txt.",
    ))

    # ---- SCAQMD facility grain -------------------------------------------------

    out.append(Assumption(
        group="SCAQMD facility grain", name="Access classification and sources (Phase A + build)",
        config_path="sources.scaqmd_facility",
        value="FIND and Public Document Search both blocked entirely (PlanetBids-shaped); loaded "
             "instead from TWO sources -- South Coast AQMD's own un-blocked bulk facility-"
             "notification XLSX, and CARB's Facility Search Tool via a one-off Playwright pull -- "
             "facility grain only, no equipment, no permit number, no capacity",
        source_type=MEASURED,
        source_detail=(
            "Checked 2026-09-08. FIND (South Coast AQMD's facility/equipment lookup) resolves to "
            "xappprod.aqmd.gov, whose robots.txt is 'User-agent: *' / 'Disallow: /' -- verbatim, full "
            "block, no exceptions. Public Document Search (permits to operate, permit public notices) "
            "resolves to onbase-pub.aqmd.gov, same full block. Neither was ever queried, not even "
            "once. www.aqmd.gov itself (the informational site) carries only a narrow, unrelated "
            "robots.txt (App_Data/, bin/, MVC/, Services/, a few file extensions) and its own Terms of "
            "Use asserts ordinary copyright over documents ('may be protected under the U.S. and "
            "Foreign Copyright Laws') rather than the ca.gov statewide public-domain language CAEATFA/"
            "AB802/OPSC could rely on -- moot here since the application hosts' robots.txt settles it "
            "first anyway. Source 1: 'Facilities Notified' Annual Emissions Reporting XLSX, linked "
            "directly from aqmd.gov/home/rules-compliance/compliance/annual-emission-reporting (not "
            "the blocked application host) -- 3,091 facilities as of the 2025 list, columns Facility "
            "ID/Name/Address/City/Zip plus AB2588/CTR/Rule-317.1 flags. The source's own Notes sheet "
            "states this list is 'not comprehensive' -- facilities notified to report under Rule "
            "301(e)/CTR, not every permitted facility in South Coast AQMD's jurisdiction.\n"
            "Source 2, corrected after an initial Phase A miss: CARB's own Facility Search Tool "
            "(ww2.arb.ca.gov/facility-search-tool) was first reported as an unreachable JS SPA with no "
            "static download or API. That was wrong -- the tool's landing page is a JS shell, but the "
            "actual search FORM lives in a plain HTML iframe "
            "(www.arb.ca.gov/app/emsinv/iframe/facinfo/facinfo.php), confirmed directly by inspecting "
            "the rendered page's frames, not just its raw HTML (a static curl/WebFetch of the landing "
            "page alone does not show it, which is what produced the original miss). That host's own "
            "robots.txt is clean (Allow: /, 2s crawl-delay). Driven once with Playwright, District='SC' "
            "(South Coast AQMD only, matching the AER population's scope), to trigger the tool's own "
            "'Download this data as a Comma Separated Value text file' export -- 5,569 facilities, "
            "columns FACID/FNAME/FSTREET/FCITY/FZIP plus per-pollutant emissions tonnage (not stored, "
            "out of scope for a facility-grain table). Saved as a static file under docs/carb/ and "
            "loaded from disk (app.pipeline.scaqmd.load_carb_facilities) -- not a live fetcher, same "
            "'hand-pulled, statically stored' precedent as app/pipeline/ab869.py's own PDF corpus. "
            "One real operational lesson from this pull: rapid repeated Playwright requests against "
            "www.arb.ca.gov during interactive exploration triggered a temporary CloudFront rate-limit "
            "block (403) despite the permissive robots.txt -- a ~15 minute cooldown cleared it. Future "
            "re-pulls (tools/pull_carb_facilities.py) should run as a single, deliberate trigger, not "
            "iterated against during debugging.\n"
            "No equipment, permit number, or capacity field exists in EITHER source -- that data lives "
            "only behind the two blocked hosts above; a Public Records Act request is the only path to "
            "it (see RUNBOOK.md's deferred-sources list, updated to remove the CARB entry now that it "
            "loads)."
        ),
        verified=True,
        last_reviewed="Checked 2026-09-08 directly against xappprod.aqmd.gov/robots.txt, "
                      "onbase-pub.aqmd.gov/robots.txt, and www.arb.ca.gov/robots.txt; CARB pull "
                      "performed and verified the same day.",
    ))

    out.append(Assumption(
        group="SCAQMD facility grain", name="Air permit join to AB 802 and AB 869",
        config_path=None,
        value="AB 802: normalized-address text match, denormalized onto Ab802Building. AB 869: "
             "normalized-facility-NAME match (same city) -- AB 869/HospitalBuilding carry no street "
             "address field at all, so an address join was not possible as originally scoped.",
        source_type=MEASURED,
        source_detail=(
            "AB 802 join: app.pipeline.scaqmd._link_ab802, normalized-address text only (neither side "
            "has lat/long here, so unlike AB 802's own RetrofitBuilding join there is no lat/long-"
            "first step) -- ambiguous on either side (two ScaqmdFacility rows or two Ab802Building "
            "rows sharing one normalized address) is dropped, never guessed at, same discipline as "
            "every prior join in this codebase. Recomputed fresh across ALL years of Ab802Building on "
            "file on every scaqmd_facility load, not just the latest year shown on the board -- a real "
            "building's address doesn't change year to year. Collapses same-facility duplicates "
            "across the two ScaqmdFacility sources (AER and CARB) by facility_id before checking "
            "ambiguity -- fixed 2026-09-08 after adding the CARB source caused a real regression: "
            "the same real facility now legitimately has one row per source, and the pre-fix version "
            "read two rows for one facility as two DIFFERENT facilities sharing an address and "
            "dropped the match. AB 869 join: inspected Ab869Plan, "
            "Ab869Building, and HospitalBuilding directly (2026-09-08) and confirmed none of the three "
            "carries a street address field -- only city/county and, on HospitalBuilding only, lat/"
            "long. A text-address join was therefore not possible as literally scoped; the best "
            "available loose join is normalized facility name (app.normalize.normalize_name), "
            "restricted to the same city to keep an unrelated same-named facility in a different city "
            "from colliding -- computed live in app.pipeline.ab869.ab869_board_rows (cheap, ~200 "
            "facilities), never stored, since there is no clean column to denormalize onto: Ab869Plan "
            "doesn't exist for the ~11 facilities with NO_PLAN_ON_FILE, and HospitalBuilding is "
            "building-, not facility-, grain. Disclosed everywhere it surfaces (model docstrings, "
            "module docstring, board UI copy, and here) as a name match, never presented as address-"
            "based. Same two-source regression applied here too, fixed the same day with a union-find "
            "collapse (app.pipeline.scaqmd._collapse_same_facility): candidates sharing a (name, city) "
            "key that also share a facility_id, OR whose normalized address matches, collapse to one "
            "before the ambiguity check -- abstain only survives for a genuinely different second "
            "facility. Measured directly against production data 2026-09-08: 28 facilities flagged "
            "before the CARB load (AER only) -> 33 after the fix (AER+CARB, collapsed). Exactly one "
            "facility that was flagged before is NOT flagged after: perm_id 12012, St. Mary Medical "
            "Center, Long Beach -- CARB's own export reveals a SECOND, genuinely distinct SCAQMD "
            "facility registration under the same name and city (facility_id 108235, '1043 Elm Ave', "
            "vs the AER-sourced facility_id 108234 at '1045 Atlantic Ave' -- different facility_id AND "
            "different street), which the pre-CARB dataset simply didn't reveal. This is the fix "
            "working as intended, not a defect: two real, distinct SCAQMD facility records with "
            "nothing in common but a name and a city is exactly the case that should abstain. "
            "Separately, a hand-eyeballed precision sample of the AB 802 join (2026-09-08, seed 42): 25 "
            "random rows matched only via a CARB-sourced facility and 25 matched via an AER-sourced "
            "facility, each showing the AB 802 address/name against the matched facility's address/name "
            "and a rapidfuzz similarity score (reported for eyeballing only -- the actual match rule is "
            "exact string equality after normalize_address, not a fuzzy threshold). Result: 0 of 25 "
            "CARB-only rows judged a false match (1 marked unsure -- 'Park Place I,' an Irvine office "
            "complex, matched to a facility named 'Park Place Apartments' at the identical street "
            "number/name; plausibly the same site under a generic SCAQMD-side name, not independently "
            "confirmed), 0 of 25 AER rows judged false. Both well under the 10% threshold that would "
            "have required tightening the match to street-number-plus-name-exact with city-exact -- no "
            "code change made."
        ),
        verified=True,
        last_reviewed="Measured 2026-09-08 against real production Ab802Building/HospitalBuilding "
                      "data.",
    ))

    out.append(Assumption(
        group="SCAQMD facility grain", name="CARB crawl-delay",
        config_path=None,
        value="2 seconds",
        source_type=MEASURED,
        source_detail=(
            "www.arb.ca.gov/robots.txt (the host actually serving the Facility Search Tool's search "
            "form, www.arb.ca.gov/app/emsinv/iframe/facinfo/facinfo.php) states 'Crawl-delay: 2' under "
            "'User-agent: *', alongside 'Allow: /'. Checked directly 2026-09-08. tools/"
            "pull_carb_facilities.py's own one-off Playwright pull respects this in spirit (a single "
            "trigger, not a scheduled or repeated fetch) but repeated INTERACTIVE requests during "
            "debugging that session -- well-paced individually, but far more frequent in aggregate "
            "than a human using the tool by hand -- tripped a CloudFront rate-limit block (403) "
            "despite the permissive robots.txt. See RUNBOOK.md's Data posture section for the rule "
            "this produced: interactive debugging against a live host must respect the same pacing "
            "discipline as production, and a CDN 403 counts as a blocked host until its own cooldown "
            "clears, not just until robots.txt says otherwise."
        ),
        verified=True,
        last_reviewed="Checked 2026-09-08 directly against www.arb.ca.gov/robots.txt.",
    ))

    # ---- BPELSG mechanical engineer roster ------------------------------------

    out.append(Assumption(
        group="BPELSG mechanical engineer roster", name="Access, file shape, and cadence",
        config_path="sources.bpelsg_mechanical_roster",
        value="Free monthly DCA Box file, robots.txt-clean on every host involved; file dated "
             "2026-09-01, 119,766 rows statewide, tab-delimited despite the .xls extension, "
             "refreshed 'at the beginning of each month' per DCA's own stated cadence",
        source_type=MEASURED,
        source_detail=(
            "Checked 2026-09-08/09. www.dca.ca.gov/robots.txt: 'User-agent: *' disallows only "
            "/images, /js, /styles, /ssi, /css, /javascript, /webapplications/ (with "
            "/webapplications/apps/*/ carved back open), and /maintenance/* -- nothing touching "
            "/consumers/public_info, which embeds a Box shared folder "
            "(dca.box.com/s/oss6hf8jys2bmgxqd2gdz7w4oepm2il9). box.com/robots.txt disallows "
            "everything for User-agent: * EXCEPT an explicit carve-out including 'Allow: /s/' and "
            "'Allow: /shared/', with Box's own comment: \"the 'noindex' robots directive is only "
            "respected if these pages are crawlable\" -- i.e. Box deliberately permits fetching a "
            "shared-link path and gates search-engine INDEXING via a noindex meta tag instead, not "
            "via robots.txt. This shared folder's URL is exactly that /s/ pattern, so it is not a "
            "blocked host. Per the explicit build instruction: ONE Playwright visit (navigate in, "
            "select the BPELSG subfolder's two files, trigger Box's own bulk-download), never polled "
            "again -- the resulting zip (both files bundled together, confirmed after the fact; "
            "Box's shared-folder download button is a bulk/zip action regardless of selection count) "
            "was unzipped once by hand into docs/bpelsg/.\n"
            "File shape: despite the .xls extension, `file` reports 'ASCII text' -- this is a plain "
            "tab-delimited text file, confirmed by inspection. Columns, verbatim: Agency Name, "
            "License Type, Speciality Code, License Number, Indiv/Org, Org/Last Name, First Name, "
            "Middle Name, Suffix, Address Line 1, Address Line 2, City, County, State, Zip, Country, "
            "Original Issue Date, Expiration Date, School, Year Graduated, Degree, License Status. "
            "119,766 data rows, 24 distinct license types (Mechanical Engineer among them, 17,742 "
            "statewide across all counties/states/statuses). Only two License Status values appear "
            "anywhere in the file: Active, Delinquent.\n"
            "NO FIRM FIELD, measured directly, not assumed: Indiv/Org is 'I' for all 119,766 rows, "
            "every license type, statewide -- zero organizational/firm-held licenses exist in this "
            "file at all. This changes what the table can be for exactly as anticipated: it supports "
            "a normalized-PERSON-name join only, never a firm-name join (see app.models.BpelsgEngineer "
            "and app.pipeline.bpelsg.match_bpelsg_for_project).\n"
            "In-territory Mechanical Engineer count (config.yaml's 7-county territories.california."
            "counties -- Los Angeles, Orange, San Bernardino, Riverside, San Diego, Imperial, Kern): "
            "5,347 of 17,742 statewide (4,769 Active, 578 Delinquent). By county: Los Angeles 2,168, "
            "Orange 1,426, San Diego 1,114, Riverside 261, San Bernardino 244, Kern 127, Imperial 7. "
            "Firm-named vs. personal-name-only: 0 vs. 5,347 -- every single one, since the file "
            "carries no organizational licenses at all.\n"
            "Cadence: DCA's Public Information page states, verbatim, 'Data is refreshed "
            "automatically at the beginning of each month.' The pulled file was dated 2026-09-01 in "
            "both the Box folder listing and the file's own mtime."
        ),
        verified=True,
        last_reviewed="Pulled and measured directly 2026-09-08/09 against the live DCA Box file "
                      "(2026-09-01 edition).",
    ))

    # ---- HCAI Facilities Development Division project reports (Phase A) -------

    out.append(Assumption(
        group="HCAI Facilities Development Division project reports", name="Access classification",
        config_path=None,
        value="Clean -- no robots.txt exists on report.hcai.ca.gov, hcai.ca.gov, or esp.hcai.ca.gov "
             "(all 404), and hcai.ca.gov's own Conditions of Use states public-domain, no automated-"
             "access restriction",
        source_type=MEASURED,
        source_detail=(
            "Checked 2026-09-08/09. report.hcai.ca.gov/robots.txt, hcai.ca.gov/robots.txt, and "
            "esp.hcai.ca.gov/robots.txt all return 404 -- no file exists at any of the three hosts, "
            "which by robots.txt convention means no crawl restriction is declared (an absent file is "
            "not a block). hcai.ca.gov/home/conditions-of-use/ (the same Conditions of Use esp.hcai.ca.gov "
            "itself links to) states, verbatim: 'information presented on this website...is considered "
            "in the public domain. It may be distributed or copied as permitted by law' -- the same "
            "CAEATFA-shaped public-domain language already relied on elsewhere in this codebase, not a "
            "confidential-data DUA (that separate, stricter process governs patient-level data requests, "
            "not this public project-status report). One Playwright session pulled 'Projects by County' "
            "for all 7 territory counties (report.hcai.ca.gov, division OSHPD, report id 38), Status="
            "'All' (the pre-run filter only offers Open/All/Closed, not the finer per-project status "
            "values, which are a column IN the results, not a filter option) -- exported to Excel and "
            "CSV under docs/sources-pilot/hcai-reports/. 45,132 project records across 1,095 facilities, "
            "1986-12-17 to 2026-09-08. esp.hcai.ca.gov's own 'Search Projects' tool was NOT queried, per "
            "instruction -- confirmed via search results only that it takes a Facility ID, HCAI Project "
            "Number, or Professional License Number (no public account required, no name-based browse). "
            "A second report on the SAME already-clean report.hcai.ca.gov host, 'Project List by "
            "Professional' (report id 3), was inspected (parameter panel only, never run) and confirmed "
            "the same shape: a free-text 'License Number with Prefix' field, no browsable list -- "
            "corroborating evidence for the same manual-per-engineer conclusion without touching "
            "esp.hcai.ca.gov at all. Facility ID join key verified directly against production, not "
            "assumed: 216 of 422 HospitalBuilding.perm_id values overlap exactly with this report's own "
            "facility IDs."
        ),
        verified=True,
        last_reviewed="Checked and pulled 2026-09-08/09 directly against report.hcai.ca.gov, "
                      "hcai.ca.gov, and esp.hcai.ca.gov.",
    ))

    out.append(Assumption(
        group="HCAI Facilities Development Division project reports",
        name="Status-to-stage mapping (Phase B build)",
        config_path=None,
        value="HCAI's own 23 raw Status values collapse to 5 buckets (plan_review, pending_start, "
             "in_construction, closed, other) -- app.pipeline.hcai_projects.STAGE_MAP, status_raw kept "
             "verbatim alongside stage, never replaced",
        source_type=MEASURED,
        source_detail=(
            "This application's own classification of HCAI's own Status values, not an HCAI-published "
            "field -- a judgment call this codebase made, verified complete against real data rather "
            "than left partial. All 23 values seen in "
            "the 2026-09-08 pull (45,132 rows, confirmed complete against that pull -- zero unmapped) "
            "are mapped explicitly:\n"
            "closed (41,035 rows): Closed, Closed Inactive, Cancelled, Closed Non-CAC Compliant, "
            "Closed Non-CBSC Compliant, Withdrawn, Void, Closed Administrative, Expired -- every value "
            "meaning 'no longer an active pipeline item,' regardless of why it stopped.\n"
            "plan_review (1,016 rows): Open - Remarked, Active - Plan Review, Open - Returned, Open, "
            "Active - Triage -- design/application still under review; bare 'Open' grouped here because "
            "its two qualified siblings ('Open - Remarked', 'Open - Returned') both belong to this "
            "review-cycle family.\n"
            "pending_start (1,604 rows): Pending Construction Start, Approved, Pending Field "
            "Operations, Issued -- design approved or permit issued, construction not yet begun. "
            "'Approved' placed here rather than plan_review because in HCAI's own workflow it precedes "
            "Pending Construction Start, not the reverse.\n"
            "in_construction (1,162 rows): Field Operations in Progress, Field Operations Suspended, "
            "Field Operations Resumed -- Suspended kept here rather than a separate bucket because the "
            "project IS under construction, just paused, not back in design review.\n"
            "other (315 rows): Pending, Active -- both too ambiguous standing alone (pending WHAT? "
            "active in WHAT sense?) to place in a specific stage without guessing; deliberately not "
            "forced toward pending_start or plan_review.\n"
            "A status this map has never seen (a new HCAI status introduced after this pull) falls to "
            "'other' via STAGE_MAP.get(status, 'other') -- never dropped, never guessed toward a more "
            "specific bucket."
        ),
        verified=True,
        last_reviewed="Built and verified against the full 2026-09-08 pull (45,132 rows, all 23 "
                      "statuses, zero unmapped) 2026-09-09.",
    ))

    out.append(Assumption(
        group="HCAI Facilities Development Division project reports",
        name="is_mechanical keyword regex (Phase B build)",
        config_path=None,
        value="app.pipeline.hcai_projects.MECHANICAL_RE -- word-boundary, case-insensitive match on "
             "HVAC, mechanical, chiller, boiler, cooling tower, central plant, air handler, AHU, RTU, "
             "VRF, heat pump, exhaust against scope_text",
        source_type=MEASURED,
        source_detail=(
            "Same keyword set used in the 2026-09-08 Phase A scope-of-work scan, now a registered "
            "regex: r'\\\\b(HVAC|mechanical|chiller|boiler|cooling tower|central plant|air handler|AHU|"
            "RTU|VRF|heat pump|exhaust)\\\\b', re.IGNORECASE. Measured against the full 45,132-row pull: "
            "2,782 rows match (269 of those in an OPEN stage -- 75 plan_review, 107 pending_start, 87 "
            "in_construction). False is 'the keyword set didn't match,' never 'confirmed non-"
            "mechanical' -- e.g. a scope reading only 'Emergency Generator Replacement' would not "
            "match despite being real mechanical-adjacent equipment work. A 15-row random eyeball "
            "sample (seed 7) of is_mechanical=True rows found 15 of 15 plausible true positives (real "
            "HVAC/chiller/air-handler/cooling-tower scope text, e.g. 'KP FMC PH3 & PH5 BUILDINGS AHU'S "
            "UPGRADE FROM CAV TO VAV,' 'COOLING TOWER REPLACEMENT') -- no code change made, since the "
            "sample showed no false-positive problem to fix."
        ),
        verified=True,
        last_reviewed="Measured 2026-09-09 against the full 2026-09-08 pull; 15-row precision sample "
                      "eyeballed the same day.",
    ))

    # ---- Five-year CIP / facilities master plan jurisdiction list (Phase A) ---

    out.append(Assumption(
        group="CIP jurisdiction list", name="Total jurisdiction count and access findings",
        config_path=None,
        value="295 jurisdictions in scope (201 cities + 7 counties from app/geo.py's territory table, "
             "34 community college districts, ~39-40 top-enrollment K-12 districts, 14 UC/CSU campuses) "
             "-- 2 of the first 15 hosts checked explicitly disallow ClaudeBot by name",
        source_type=MEASURED,
        source_detail=(
            "Researched 2026-09-08/09, not asserted from memory. Cities/counties: app/geo.py's own "
            "_CITY_TO_COUNTY table, 201 entries (LA 79, Orange 34, San Bernardino 24, Riverside 28, San "
            "Diego 18, Kern 11, Imperial 7) plus the 7 counties themselves = 208. Community college "
            "districts: checked against CCCCO's own district list (73 statewide) for any serving at "
            "least one of the 7 territory counties = 34. K-12: CDE's own largest-district list cross-"
            "referenced against app/geo.py's county table found 39 of a targeted 40 confirmed in-"
            "territory from a top-60-statewide source; the marginal 40th sits just below that source's "
            "rank 60 and was not individually confirmed -- reported as 39, not guessed to 40. UC/CSU: "
            "verified via Wikipedia's own campus-location table, not the vendor sites (calstate.edu "
            "blocked WebFetch with a 403 bot-challenge) -- 4 UC (LA, Irvine, Riverside, San Diego) + 10 "
            "CSU (LA, Long Beach, Fullerton, Northridge, Dominguez Hills, Pomona, San Bernardino, "
            "Bakersfield, San Marcos, San Diego State) = 14.\n"
            "Access, first 15 hosts checked (7 counties + 8 largest cities by population): 9 of 15 "
            "clean/generic CMS boilerplate (LA County, Orange County, San Diego County, Imperial County, "
            "LA city, San Diego city, Anaheim, Santa Ana, San Bernardino city -- the last of which "
            "additionally publishes an 'LLM-Policy: /llms.txt' directive, a newer convention not seen "
            "elsewhere in this codebase's research); 2 of 15 explicitly disallow AI crawlers by name "
            "via a Cloudflare-managed 'Content-Signal' block naming ClaudeBot, GPTBot, CCBot, Google-"
            "Extended, and others (Riverside COUNTY at rivco.gov and Riverside CITY at riversideca.gov, "
            "both verbatim, checked directly) -- the first time in this codebase's research that a host "
            "has named Claude specifically, and per this app's own 'never alter the user-agent to evade "
            "a block' rule, both downgrade to MANUAL; 1 of 15 (Kern County, kerncounty.com) returns an "
            "Akamai edge 'Access Denied' on the /robots.txt request itself, a stronger, infrastructure-"
            "level block, also MANUAL; 3 of 15 (San Bernardino County, Long Beach, Bakersfield) gave "
            "redirects or non-standard responses this pass didn't fully resolve -- inconclusive, not "
            "counted as either clean or blocked.\n"
            "Coverage disclosure: only these 15 hosts plus the 5 jurisdictions pulled for the hand-"
            "sample (2c) were actually checked for a real CIP URL this pass -- the remaining ~275 of 295 "
            "jurisdictions were NOT searched. This is a disclosed gap, not a completed list; see "
            "RUNBOOK.md."
        ),
        verified=True,
        last_reviewed="Researched 2026-09-08/09; jurisdiction counts and the 15-host robots.txt sample "
                      "both checked directly against live sources.",
    ))

    # ---- Project delivery method --------------------------------------------

    dmc = delivery_method_coverage or {}
    active_total = dmc.get("active_total")
    active_with = dmc.get("active_with_delivery_method")
    out.append(Assumption(
        group="Project delivery method", name="Coverage: active projects stating a delivery method",
        config_path=None,
        value=(f"{active_with} of {active_total} active projects "
              f"({100 * active_with / active_total:.0f}%)" if active_total else "not available on this page load"),
        source_type=MEASURED,
        source_detail="Live count, checked at every page load — not a config constant. Extracted only when a "
                      "filing itself states the delivery method (app.llm.EXTRACT_SYSTEM's delivery_method "
                      "section); never inferred from project type, owner, or agency. Expect this to be low: "
                      "most public filings (CEQA documents, board packets, permits) simply don't discuss "
                      "procurement structure — that scarcity is the honest finding, not a defect in the "
                      "extraction. Low coverage means the field is a real-but-occasional signal, not a "
                      "board-wide ranking term — it isn't used in scoring.",
        verified=True,
        last_reviewed="Live count as of this page load.",
    ))

    out.append(Assumption(
        group="Project delivery method", name="Who selects equipment, by delivery method",
        config_path=None,
        value=f"{len(DELIVERY_METHOD_LABELS)} methods mapped: design-bid-build, design-build, design-assist, "
             f"CM at risk, progressive design-build",
        source_type=RULE_OF_THUMB,
        source_detail="General AIA/DBIA project-delivery definitions, not measured against this territory's "
                      "own outcomes: under design-bid-build the engineer of record writes Division 23 and "
                      "names a basis of design, so the right call is to the MEP firm; under design-build and "
                      "design-assist the mechanical contractor typically selects equipment, often before a "
                      "specification exists, so the right call is to the contractor. CM at risk and "
                      "progressive design-build are mixed cases (see app/delivery.py's "
                      "DELIVERY_METHOD_NOTES) — who owns Division 23 depends on how that specific project's "
                      "trade packages are structured, and the note says so rather than picking a side.",
    ))

    # ---- Model routing ------------------------------------------------------
    # 2026-08-19: per-stage LLM model choice, verified rather than assumed --
    # see config.yaml's llm: block for the live per-stage values and the
    # policy comment above them ("route by task: fixed-schema extraction ->
    # Haiku, synthesis/research/judgment -> Sonnet"). Both decisions below
    # were tested against hand-verified ground truth before being applied,
    # per the standard this app holds itself to: a routing change that costs
    # measurable accuracy doesn't ship, even if it costs less.
    out.append(Assumption(
        group="Model routing", name="extract_model: kept on Sonnet, not moved to Haiku",
        config_path="llm.extract_model",
        value=f"{cfg.get('llm.extract_model')} (unchanged)",
        source_type=MEASURED,
        source_detail=(
            "extract() is a fixed-schema extraction task -- the routing policy's own "
            "Haiku candidate shape -- but a 12-document hand-verified comparison "
            "(2026-08-19, evals/golden.jsonl, spanning ceqanet/goed sources) found Haiku "
            "meaningfully worse specifically on jurisdiction fields: state precision 0.45 vs "
            "Sonnet 4.6's 1.00 (6 of 11 wrong -- a field that should be nearly unambiguous), "
            "county 0.58 vs 0.92 (5 of 12 wrong vs 1). cooling_type also fabricated twice "
            "under Haiku (zero under Sonnet). Some fields favored Haiku (project_name 0.67 "
            "vs 0.42 precision; named_people fabricated less, 15 vs 29) -- this was not a "
            "clean sweep either direction, but county/state are load-bearing for this app's "
            "own territory and scoring logic, and losing them meaningfully is exactly the "
            "case this app's own standard says keep Sonnet and say so. sam_gov_extract "
            "shares this same config key and is therefore unchanged for the identical "
            "reason, not a separately measured decision. Per-document ground truth and both "
            "models' rerun output are recorded in evals/golden.jsonl (verified: true "
            "entries, model_haiku/model_sonnet keys)."
        ),
        verified=True,
        last_reviewed="Measured 2026-08-19 against 12 hand-verified documents, 24 real API calls.",
    ))
    out.append(Assumption(
        group="Model routing", name="voice_capture_model: moved from Sonnet 5 to Haiku",
        config_path="llm.voice_capture_model",
        value=f"{cfg.get('llm.voice_capture_model')} (was claude-sonnet-5)",
        source_type=MEASURED,
        source_detail=(
            "extract_voice_capture() fills a fixed schema (OutreachCallExtraction) from a "
            "call transcript -- the routing policy's Haiku candidate shape, and unlike "
            "extract_model above, verification found no meaningful loss. A 5-transcript "
            "hand-written comparison (2026-08-19, covering a clean case, a deliberately odd "
            "name spelling, a case with an explicit stated date, a case where the correct "
            "answer is a null date rather than a guess, and a low-information/garbled case) "
            "scored Haiku 24/25 field-level exact matches against Sonnet 4.6's 25/25. The one "
            "miss (case 2, stage='interested' vs the correct null) is a stage-classification "
            "nuance, not a name or a date -- contact_name, firm_name, and next_action_date "
            "each scored 5/5 for BOTH models, including the two null-date cases (proving "
            "neither model guesses a date that was never stated). This is exactly the two "
            "field types this app's own standard names (\"if Haiku loses meaningfully on "
            "names or dates, keep Sonnet\") -- it does not lose on either, so the move is "
            "made. Haiku also runs ~3x cheaper per call at current prices (see llm.prices)."
        ),
        verified=True,
        last_reviewed="Measured 2026-08-19 against 5 hand-written transcripts, 10 real API calls.",
    ))

    # ---- Voice capture -----------------------------------------------------

    import app.pipeline.voice_capture as vc
    from app.voice_match import MATCH_THRESHOLD as _VM_THRESHOLD
    out.append(Assumption(
        group="Voice capture", name="Fuzzy name-match threshold (app.voice_match)",
        config_path=None,
        value=f"{_VM_THRESHOLD:.0f} / 100 (rapidfuzz token_sort_ratio), top 5 candidates shown",
        source_type=PLACEHOLDER,
        source_detail="A judgment call, not measured against labeled outcomes -- no confirmed/rejected "
                      "capture history exists yet to fit against. Matches the floor "
                      "app/importers/accounts_csv.py already uses for its own fuzzy account-name "
                      "matching, for the same reasoning: below this, a suggestion trains the reviewer "
                      "to stop reading the candidate list. Never auto-selects a match regardless of "
                      "score -- every candidate is a suggestion a human picks from, at /captures/{id}.",
    ))

    out.append(Assumption(
        group="Voice capture", name="Whisper transcription cost ($/minute)",
        config_path=None,
        value=f"${vc.WHISPER_USD_PER_MINUTE:.3f}/min ({vc.WHISPER_MODEL})",
        source_type=MEASURED,
        source_detail="OpenAI's own published rate for the whisper-1 endpoint as of 2026-08-16 -- "
                      "not this system's own measurement, but a stated third-party price, not a "
                      "guess. Duration-proportional (verbose_json's own reported duration), no "
                      "per-request minimum. gpt-4o-mini-transcribe is cheaper ($0.003/min) but is a "
                      "different, newer model this task did not ask for -- left as a future swap, "
                      "not made here.",
    ))

    # ---- Hospital seismic compliance --------------------------------------

    from app.pipeline.hcai import NPC5_DEADLINE_YEAR, SPC1_DEADLINE_YEAR, SPC2_DEADLINE_YEAR
    out.append(Assumption(
        group="Hospital seismic compliance", name="SB 1953 SPC/NPC deadline schedule",
        config_path=None,
        value=f"SPC-1 -> {SPC1_DEADLINE_YEAR}, SPC-2 -> {SPC2_DEADLINE_YEAR}, "
             f"'meets 2030 standard' = SPC>=3 AND NPC=5 (by {NPC5_DEADLINE_YEAR})",
        source_type=STATED,
        source_detail=(
            "Health & Safety Code section 130000 et seq. (the Alfred E. Alquist Hospital "
            "Facilities Seismic Safety Act), read directly from HCAI's own published pages "
            "2026-08-17 (hcai.ca.gov/facilities/building-safety/seismic-compliance-and-safety/ "
            "and its program-overview and seismic-performance-ratings subpages): 'State law "
            "requires all SPC 1 buildings to be removed from providing general acute care "
            "services by January 1, 2020, unless an approved extension has been granted, and "
            "all SPC 2 buildings to be removed from providing general acute care services by "
            "January 1, 2030... A hospital facility meets the January 1, 2030 seismic safety "
            "standards if all the general acute care buildings on campus are SPC 3, 4, 4D, or "
            "5 and NPC 5.' These are the BASE statutory dates only. HCAI separately publishes "
            "a 'Seismic Deadline Extensions Granted for California Hospitals' dataset (six "
            "extension bills: SB 306, AB 523, SB 1661/AB 2557/AB 81, SB 499, SB 90, AB2190, "
            "each with its own scope and inconsistent per-column date formats) -- "
            "app.models.HospitalBuilding.has_filed_extension flags whether ANY such record "
            "exists for a building (1,625 of 4,666 buildings do, measured 2026-08-17) but "
            "deliberately does NOT compute an extended deadline date from those columns: "
            "picking the governing bill and parsing its date correctly for every building is "
            "exactly the false-precision this system abstains from elsewhere (see the "
            "masked-APN permit exclusion and EBEWE's ain_last3 checksum for the same "
            "discipline applied to other sources). A building's spc_deadline_year/"
            "npc_deadline_year shown on the hospital board is therefore the BASE schedule, "
            "and has_filed_extension is the signal that the real deadline may be later -- read "
            "HCAI's own extension record before treating either date as final."
        ),
        verified=True,
        last_reviewed="2026-08-17, read directly from hcai.ca.gov.",
    ))

    hc = hospital_coverage or {}
    hc_total = hc.get("total_buildings")
    hc_territory = hc.get("in_territory")
    hc_2020 = hc.get("spc_2020_deadline")
    hc_2030 = hc.get("spc_2030_deadline")
    out.append(Assumption(
        group="Hospital seismic compliance", name="HCAI seismic-ratings coverage",
        config_path=None,
        value=(f"{hc_territory:,} of {hc_total:,} buildings in Scout's territory counties "
              f"({100*hc_territory/hc_total:.1f}%); {hc_2020:,} carry the (likely already "
              f"overdue) SPC-1/2020 deadline, {hc_2030:,} carry the SPC-2/2030 deadline"
              if hc_total else "not available on this page load"),
        source_type=MEASURED,
        source_detail="Live count from HospitalBuilding as of the last `scout import-hcai-seismic` "
                      "run -- see source_health for when that was. 'In territory' means the "
                      "building's county is one of the CA counties in config.yaml's territories "
                      "block (Los Angeles, Orange, San Bernardino, Riverside, San Diego, Imperial, "
                      "Kern); HCAI's dataset itself covers all 58 CA counties statewide, most of "
                      "which are out of Scout's scope.",
    ))

    cg = hospital_capability_gaps or {}
    cg_chillers_n = cg.get("chillers_checked")
    cg_fans_n = cg.get("fans_checked")
    out.append(Assumption(
        group="Hospital seismic compliance", name="DMG's HCAI OSP capability gap (all 13 roles)",
        config_path=None,
        value=(f"{cg.get('roles_with_gap')} of {cg.get('roles_checked')} building-systems roles have "
              f"zero lines with a confirmed current OSP -- every line in every one of those roles is "
              f"individually researched, not guessed: 35 lines checked, 2 expired (renewal ask), 33 "
              f"not listed (new-application-or-accept-the-gap ask), 0 current. Chillers: 0 of "
              f"{cg_chillers_n} DMG lines hold a confirmed current OSP "
              f"({cg.get('chillers_confirmed_expired', 0)} confirmed expired, rest not listed); "
              f"fans: 0 of {cg_fans_n} lines hold one ({cg.get('fans_confirmed_expired', 0)} confirmed "
              f"expired, rest not listed)"
              if cg_chillers_n is not None else "not available on this page load"),
        source_type=MEASURED,
        source_detail=(
            "Computed LIVE from ProductLine.oshpd_osp every time this loads (app.pipeline.hcai."
            "hospital_capability_gaps), never hardcoded -- so this self-corrects the moment a "
            "line wins a new OSP instead of silently going stale on a board people read to "
            "decide whether to call. Generalized 2026-08-17 from chillers/fans only to all 13 "
            "roles, then COMPLETED 2026-08-19: every line in every gapped role checked directly "
            "against HCAI's live OSP directory and its 25-category historical listing, the same "
            "method the fan lines were checked with -- the full per-role table is on /hospitals "
            "itself, not just this register. One real find: Marley (SPX Cooling Tech's Marley NC "
            "cooling tower, OSP-0171) is confirmed CURRENT, verified against its own HCAI "
            "application PDF -- approved 2026-06-02, expires 2032-06-02 -- closing heat_rejection "
            "as a gapped role (8 of 13 gapped now, down from 9). Chillers are DMG's "
            "category='chillers_cooling' lines specifically (DB, ClimaCool, Geoclima, Hecoclima) "
            "-- deliberately NOT the broader 'cooling_generation' building_role, which also "
            "contains category='vrf_split' (fan-coil/PTAC/VRF terminal equipment; Nailor's "
            "'Engineered Comfort' brand sits here and DOES hold a current OSP for its fan coil "
            "units, OSP-0772) -- rolling that up as 'chillers covered' would be exactly the "
            "false-precision this register exists to flag. One near-miss worth naming: DMG's IEC "
            "line (indirect/direct evaporative cooling) is recorded not_listed rather than "
            "expired, despite HCAI listing an 'IEC' OSP-0211 (Expired after 12/31/2016) under Fan "
            "Coil Units -- almost certainly a different company (International Environmental "
            "Corporation, Oklahoma City, whose own catalog is fan coil units, not evaporative "
            "cooling) sharing the same short name, not a confirmed match; treating a name "
            "coincidence as a hit would be the same false-precision risk as the chillers/"
            "vrf_split conflation, just in the other direction. Every individual line's own "
            "research trail (HCAI directory/PDF citations, retrieval date) is in config.yaml's "
            "oshpd_osp_basis field for lines transcribed there, or app.pipeline.hcai."
            "HOSPITAL_BRIEF_OSP_FACTS for lines checked 2026-08-19 not yet transcribed into "
            "config.yaml -- this entry is the aggregate read across all of them, not a new "
            "independent finding. Air handling, heat rejection, air distribution/terminal, "
            "controls/valves, and humidification each have at least one confirmed current OSP "
            "and are not part of this gap."
        ),
        verified=True,
        last_reviewed="Live count as of this page load.",
    ))

    out.append(Assumption(
        group="Hospital seismic compliance", name="CHHS Open Data Portal — terms of use",
        config_path=None,
        value="Manual download required; automated fetch not clearly sanctioned",
        source_type=STATED,
        source_detail=(
            "data.chhs.ca.gov/pages/terms, read directly 2026-08-17 (last modified per the page "
            "itself: January 27, 2023). Two separate findings: "
            "(1) AUTOMATED ACCESS -- the 'Public Participation' section states 'You must not "
            "create accounts with the CalHHS Open Data Portal or access the site through "
            "unauthorized means, including but not limited to, by using an automated device, "
            "script, bot, spider, crawler or scraper.' That sentence is textually scoped to the "
            "portal's participatory features (accounts, comment forums, uploads) — its own "
            "section header is 'Public Participation' — not plainly to an unauthenticated file "
            "download, so this is genuinely ambiguous rather than a flat ban on downloading the "
            "CSV. robots.txt separately disallows only /api/ and /datastore/* (confirmed by direct "
            "fetch), not the plain resource-download URL a browser uses. Scout does not rely on "
            "either ambiguous reading either way: `scout import-hcai-seismic` requires a file "
            "already downloaded by hand, matching the precedent app.pipeline.iepr.py already set "
            "for a different reason (a TN filing number is not a stable feed endpoint). "
            "(2) REDISTRIBUTION/USE -- the 'Intellectual Property' section: the State grants a "
            "'non-exclusive, non-transferable, revocable license to use and distribute the "
            "Content... in a manner consistent with the Terms of Use'; HCAI's own datasets are "
            "State-owned content, so the separate sublicense-from-a-third-party clause on that "
            "same page does not apply. Reuse/redistribution 'requires attribution of credit to "
            "the CalHHS department or office providing such Content and a citation to the "
            "webpage and date of publication of the material cited' -- Scout's own hospital "
            "board and detail pages carry this attribution + a link to the source dataset page. "
            "Separately: 'If you modify the Content for your own purposes in any way, you may "
            "not claim the data is \"official government data\" and must clearly indicate that "
            "the data... has been modified' -- this is why spc_deadline_year/npc_deadline_year/ "
            "meets_2030_standard (Scout's own derived reading of HCAI's raw SPC/NPC codes) are "
            "always shown alongside, never in place of, the raw HCAI-published codes. This "
            "finding governs deployment beyond a single user: if this tool is ever run by "
            "someone else, both the manual-download requirement and the attribution/labeling "
            "obligations above travel with it, not just with this session."
        ),
        verified=True,
        last_reviewed="2026-08-17, read directly from data.chhs.ca.gov/pages/terms.",
    ))

    # ---- CAEATFA sales-tax exclusion approvals -------------------------------

    out.append(Assumption(
        group="CAEATFA sales-tax exclusion approvals", name="treasurer.ca.gov — terms of use and robots.txt",
        config_path=None,
        value="Automated bulk download sanctioned; no restriction found",
        source_type=STATED,
        source_detail=(
            "Two checks, both read directly 2026-08-20. (1) ROBOTS.TXT -- fetched "
            "treasurer.ca.gov/robots.txt directly (200 OK, standard Drupal boilerplate): it "
            "disallows /core/, /profiles/, /admin/, /search/, /user/*, /comment/reply/, "
            "/node/add/, /media/oembed, and a short list of named README/config files. It does "
            "NOT disallow /caeatfa/ or /sites/default/files/, where sources.caeatfa.awards_url "
            "lives -- confirmed by direct fetch of that exact path, and PoliteClient enforces "
            "the same robots.txt check again at request time regardless. (2) CONDITIONS OF USE "
            "-- ca.gov/legal/conditions-of-use/ (the site-wide policy treasurer.ca.gov operates "
            "under) imposes no restriction on automated or bulk access; its only 'unauthorized "
            "use' language concerns defeating security controls, and it states public "
            "information 'may be distributed or copied as permitted by law.' Separately, "
            "CAEATFA's own STE program page (treasurer.ca.gov/caeatfa/ste/index.asp) publishes "
            "the exact file this source downloads as a self-service link titled 'Sales and Use "
            "Tax Exclusion Program Awards' -- the sanctioned intended use, same standing as "
            "CSLB's bulk 'License Master' download (see the 'Line card branch coverage' group's "
            "sibling precedent app.pipeline.cslb.py), not a scrape of a search UI meant for "
            "one-record-at-a-time lookup. See app/sources/caeatfa.py's module docstring for the "
            "full account, including the two sheets ('Apps Denied', 'Apps up for Consideration') "
            "deliberately not read."
        ),
        verified=True,
        last_reviewed="2026-08-20, read directly from treasurer.ca.gov/robots.txt and ca.gov/legal/conditions-of-use/.",
    ))

    out.append(Assumption(
        group="CAEATFA sales-tax exclusion approvals",
        name="“Still pre-construction” window",
        config_path="sources.caeatfa.purchase_window_years",
        value=f"{cfg.get('sources.caeatfa.purchase_window_years', 5)} years from board approval",
        source_type=STATED,
        source_detail=(
            "treasurer.ca.gov/caeatfa/ste/faq, read directly 2026-08-20: 'All Qualified Property "
            "purchases must be completed within five years of Application approval,' with an "
            "intermediate milestone requiring at least 15% purchased or on executed purchase "
            "order within two years of approval or the award holder is ineligible for a new "
            "award for two years. This is CAEATFA's own stated deadline for when an approved "
            "project's equipment buying is done, not a lookback window invented for this system "
            "-- an award still inside the 5-year window has not necessarily reported all its "
            "purchases yet (Signal.raw_text carries the '% Reported' figure from the same row so "
            "a rep can see how far along a given award actually is), and an award past the "
            "5-year mark is assumed to have completed its buildout even if % Reported is stale "
            "or blank in the published workbook."
        ),
        verified=True,
        last_reviewed="2026-08-20, read directly from treasurer.ca.gov/caeatfa/ste/faq.",
    ))

    out.append(Assumption(
        group="Hospital seismic compliance", name="Manual-import staleness threshold",
        config_path="sources.hcai_seismic_ratings.stale_hours",
        value=f"{cfg.get('sources.hcai_seismic_ratings.stale_hours', 2160):.0f} hours "
             f"({cfg.get('sources.hcai_seismic_ratings.stale_hours', 2160)/24:.0f} days)",
        source_type=PLACEHOLDER,
        source_detail="A judgment call, not a measured cadence -- CHHS's own dataset page shows "
                      "irregular (not fixed-schedule) updates, and this is a MANUAL import besides "
                      "(a person has to notice, download, and run `scout import-hcai-seismic`). "
                      "Every other source in config.yaml's sources: block defaults to a 36-hour "
                      "staleness window (app.ops.doctor), which would flag this stale within two "
                      "days of any realistic manual re-import cadence -- see "
                      "app.ops.doctor's per-source stale_hours override, added for this source.",
    ))

    # ---- AB 869 seismic compliance plan roster -------------------------------

    out.append(Assumption(
        group="AB 869 seismic compliance plan roster",
        name="hcai.ca.gov / tab.hcai.ca.gov — robots.txt, X-Robots-Tag, meta robots",
        config_path=None,
        value="No automated-access restriction found; manual import chosen for a practical reason, not a legal one",
        source_type=STATED,
        source_detail=(
            "Three checks, all read directly 2026-09-02 against the actual roster URL "
            "(tab.hcai.ca.gov/t/OSHPD_PUBLIC/views/CompliancePlanWebsite/CompliancePlan), re-run after "
            "an independent fetcher reported a robots-disallowed error for this same URL -- that report "
            "did not reproduce here. (1) ROBOTS.TXT -- `curl -D -` against "
            "https://tab.hcai.ca.gov/robots.txt returns a genuine HTTP 404, served by Tableau Server's "
            "own generic error page (title 'Page Not Found', Server: Tableau header present, "
            "X-Tableau: Tableau Server header present) -- not a redirect, not a WAF block page, not an "
            "empty/malformed response that could be mistaken for one. No robots.txt file exists on this "
            "host, same 'no file = default permissive' status CSLB's domain already has elsewhere in "
            "this codebase. (2) X-ROBOTS-TAG -- absent from both the robots.txt response headers and "
            "the view URL's own response headers (full header dump captured both times; grepped "
            "case-insensitively, no match). (3) META ROBOTS -- absent from the view URL's HTML body "
            "(grepped case-insensitively for any <meta ... robots ...> tag; no match. The HTML returned "
            "is a genuine 200, Content-Length 5053, a pure Tableau/VizQL JS bootstrap shell with no "
            "server-rendered data). Separately, hcai.ca.gov's own Conditions of Use "
            "(hcai.ca.gov/home/conditions-of-use/, read directly) carries no automated-access/bot/"
            "scraper restriction at all -- it states site information 'is considered in the public "
            "domain' and 'may be distributed or copied as permitted by law', the CAEATFA "
            "(treasurer.ca.gov) profile, not the CHHS Open Data Portal profile that put HCAI seismic "
            "ratings and IEPR on manual import. All three signals are clean: nothing here forbids "
            "automated access. The reason this source still comes in manually is NOT a terms question "
            "-- it is that the roster is a live Tableau Server VizQL application requiring real browser "
            "JS execution to populate (the bootstrap shell above proves this: no data exists in the "
            "plain-HTTP response to scrape even if fully permitted to). Building a VizQL session client "
            "for a one-source pilot was judged not worth it; a human reading the filtered view and "
            "supplying the results by hand is the chosen path, matching the HCAI-seismic precedent's "
            "OWN import shape (`scout import-*` from a file already obtained) even though the "
            "underlying legal reasoning differs from that precedent's CHHS-terms basis. The independent "
            "fetcher's earlier 'robots-disallowed' report is unexplained by anything found here -- "
            "possibly a stale/cached robots.txt read, a cross-host robots.txt check against hcai.ca.gov "
            "instead of tab.hcai.ca.gov, or a rate-limit/WAF response that fetcher's own error handling "
            "mislabeled -- but is not reproduced by a direct, fresh fetch."
        ),
        verified=True,
        last_reviewed="2026-09-02, read directly from tab.hcai.ca.gov/robots.txt (headers + body), the "
                      "CompliancePlanWebsite view URL's own response headers and HTML body, and "
                      "hcai.ca.gov/home/conditions-of-use/.",
    ))

    # ---- Engineering safety rules ---------------------------------------

    out.append(Assumption(
        group="Engineering safety rules",
        name="Write-path verification runs against a local restore, never production",
        config_path=None,
        value="Enforced: apply_review_decision/merge_projects/run_dc_news_enrichment refuse to run "
             "when SCOUT_VERIFYING_AGAINST_PROD is set",
        source_type=STATED,
        source_detail=(
            "2026-09-06: fixing the RATCHET BUG (Project.score/window not recomputed by three "
            "off-cycle fact-write paths) required verifying apply_review_decision(candidate #88) "
            "against production to confirm the fix. The verification was wrapped in "
            "session.begin()/session.rollback(), intended as a read-only simulation. That does not "
            "work: apply_review_decision now calls run_size_score(), which commits internally, so "
            "the outer rollback had nothing left to undo. The merge was applied for real -- "
            "projects id 708 (stage permitting->construction, window IN_BOD->POST_BOD, score "
            "0.2423->0.0693, last_signal_at 2026-06-03->2026-08-17), match_candidates id 88 "
            "(status pending->merged, resolved_at set), plus a new project_signals row (id 1389) "
            "and a new stage_observations row (id 498). developer_aliases was unaffected -- the "
            "alias _learn_alias() would have written already existed (id 1, seeded 2026-08-05). "
            "Reverted the same day: all four rows restored to their exact pre-merge values in one "
            "transaction, read back and confirmed identical to the values captured earlier in the "
            "same session before anything was touched. Full account, including the exact SQL run "
            "both ways, in CHANGELOG.md's 2026-09-06 entry. "
            "THE RULE this incident produced: verification of any write path that mutates Project/"
            "Signal facts -- apply_review_decision, merge_projects, run_dc_news_enrichment, and any "
            "future import that does the same -- runs against a local Postgres restored from a "
            "production dump, never against production, full stop. A wrapping transaction is not "
            "a sufficient safeguard on its own, since a called function's own internal commit (here, "
            "run_size_score's) can defeat it silently. Enforced mechanically, not just by policy: "
            "those three functions call app.runguard.refuse_if_verifying_against_prod() first and "
            "refuse outright when SCOUT_VERIFYING_AGAINST_PROD is set in the environment -- see "
            "that function's own docstring for exactly when to set the flag (before any check "
            "against a database not yet confirmed to be a local restore) and why there is "
            "deliberately no override."
        ),
        verified=True,
        last_reviewed="2026-09-06, incident and revert both performed and verified directly in this session.",
    ))

    return out


def assumptions_by_group(cfg: Config, service_calls_coverage: dict | None = None,
                         delivery_method_coverage: dict | None = None,
                         ownership_recency_coverage: dict | None = None,
                         portfolio_coverage: dict | None = None,
                         hospital_coverage: dict | None = None,
                         hospital_capability_gaps: dict | None = None) -> dict[str, list[Assumption]]:
    grouped: dict[str, list[Assumption]] = {}
    for a in load_assumptions(cfg, service_calls_coverage=service_calls_coverage,
                              delivery_method_coverage=delivery_method_coverage,
                              ownership_recency_coverage=ownership_recency_coverage,
                              portfolio_coverage=portfolio_coverage,
                              hospital_coverage=hospital_coverage,
                              hospital_capability_gaps=hospital_capability_gaps):
        grouped.setdefault(a.group, []).append(a)
    return grouped


def source_tally(assumptions: list[Assumption]) -> list[tuple[str, str, int]]:
    """(source_type, label, count), in SOURCE_ORDER — the honesty scorecard
    at the top of the page. If most rows land in PLACEHOLDER, that is the
    finding, and this makes it visible at a glance instead of requiring
    someone to read every row to notice."""
    counts: dict[str, int] = {}
    for a in assumptions:
        counts[a.source_type] = counts.get(a.source_type, 0) + 1
    return [(t, SOURCE_LABELS[t], counts.get(t, 0)) for t in SOURCE_ORDER if counts.get(t, 0)]
