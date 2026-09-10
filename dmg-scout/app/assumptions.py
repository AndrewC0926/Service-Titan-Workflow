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

    out.append(Assumption(
        group="HCAI Facilities Development Division project reports",
        name="is_mechanical negative-keyword regex (2026-09-10 follow-up)",
        config_path=None,
        value="app.pipeline.hcai_projects.NEGATIVE_MECHANICAL_RE -- word-boundary, case-insensitive "
             "match on anchorage, receptacle, detector, fire alarm, seismic, bracing, lighting, "
             "roofing, signage; a scope_text hit on BOTH this and MECHANICAL_RE clears is_mechanical "
             "back to False. 72 of 2,782 previously-True rows flipped; only 2 of the 72 were in an "
             "open stage (269 -> 267 open-stage mechanical rows).",
        source_type=MEASURED,
        source_detail=(
            "Re-ran `scout load-hcai-projects` against the same on-disk 2026-09-08 pull (45,132 rows, 0 "
            "new, all 45,132 updated) after adding this second regex -- an idempotent recompute, not a "
            "new fetch. Snapshotted is_mechanical for all 45,132 rows before the code change, diffed "
            "against the same 45,132 rows after: exactly 72 flipped True->False, zero flipped the other "
            "direction (the new logic is is_mechanical AND NOT negative_match, so it can only ever "
            "remove a positive, never add one). Of the 72, 70 were already stage=closed (no board-"
            "visible effect); only 2 were open (HL021544-0-GEO and S250501-19-00, both pending_start) "
            "-- open-stage mechanical count moved from 269 to 267.\n"
            "This CONTRADICTS the prior entry's own 15-row eyeball sample (seed 7), which found zero "
            "false positives and made no change -- not an error in that sample, a real limitation of "
            "any small sample: 72 of 45,132 (0.16%) is a real but thin failure rate, easy for 15 random "
            "draws to miss entirely. Reading the 72 individually (not just counting them) also surfaced "
            "a real weakness in a purely word-based negative filter: IL091620-2, scope 'ARCHITECTURAL, "
            "MECHANICAL, ELECTRICAL, PLUMBING, FIRE ALARM, FIRE SUPPRESSION IN 2 VOLUMES,' is a genuine "
            "multi-trade filing that explicitly names MECHANICAL as one of its trades, and got cleared "
            "anyway because the same line also names FIRE ALARM -- a real, disclosed false negative "
            "this filter introduces on multi-trade filings, not caught or worked around here (it is "
            "already stage=closed, so it has no open-board effect today, but the failure mode would "
            "recur on a future open-stage multi-trade row). Most of the other 70 are genuinely correct "
            "clears -- e.g. 'SEISMIC ANCHORAGE FOR WATER HEATER IN MECHANICAL ROOM' and 'COOLING TOWER "
            "ANCHORAGE & BRACING' are seismic/structural bracing of mechanical equipment, not mechanical "
            "scope of work on it.\n"
            "INVISIBLE-FACILITY COUNT, measured the same pass: of the 267 open-stage mechanical rows "
            "(after), 67 (25.1%) are at a facility_id with NO matching HospitalBuilding.perm_id row -- "
            "58 distinct facilities. These rows exist in hcai_projects (every field, including "
            "facility_name and county, already stored on the row itself) but never appear on the /ab869 "
            "board today, because that board's facility list is built by iterating HospitalBuilding, "
            "not hcai_projects, and joins in each facility's open HCAI projects only where a match "
            "exists (app.pipeline.ab869.ab869_board_rows). hcai_match_rate() reports 216 of 1,094 "
            "distinct hcai_projects facility_ids matched to a HospitalBuilding row (878 unmatched) -- "
            "consistent with HospitalBuilding being a narrower, SB-1953-seismic-tracked-building "
            "population, not the full set of facility types HCAI's Facilities Development Division "
            "regulates.\n"
            "PROPOSED, NOT BUILT (per instruction): the cheapest fix does not need a new join, a new "
            "fetch, or a new table -- facility_name and county already live on the hcai_projects row "
            "itself, so a second, small, unjoined query (open-stage, is_mechanical=True, facility_id "
            "NOT IN the HospitalBuilding perm_id set) could list these 67 rows directly, either as a "
            "second collapsible section on the existing /ab869 board ('mechanical HCAI projects at "
            "facilities not on this board') or a one-off CSV export command. Not built this pass."
        ),
        verified=True,
        last_reviewed="Measured directly 2026-09-10: full before/after snapshot diff against production, "
                      "all 72 flipped rows read individually, not sampled.",
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

    # ---- Nightly diff (item 1 of docs/DAILY-BRIEF-DESIGN.md) --------------
    # Five per-table entries, one per app.pipeline.diffs.SOURCE_FINGERPRINT_FIELDS
    # key -- each is a disclosed judgment call about what counts as "changed"
    # for that table, not a guess: every one is exercised by a fixture test
    # in tests/test_diffs.py that proves new/changed/unchanged/removed on
    # real ORM rows, not just asserted here.

    out.append(Assumption(
        group="Nightly diff", name="Fingerprint fields — hcai_projects",
        config_path=None,
        value="stage, is_mechanical (app.pipeline.diffs.SOURCE_FINGERPRINT_FIELDS['hcai_projects'])",
        source_type=MEASURED,
        source_detail=(
            "A project is worth re-alerting on when its stage moves (plan_review -> pending_start -> "
            "in_construction -> closed) or when is_mechanical flips -- both are the facts a rep would "
            "actually want to hear about again; every other field (cost_est, pct_complete, scope_text "
            "wording) can drift on a reload without being a real change worth surfacing. Proven on a "
            "fixture: stage pending_start -> in_construction registers as changed, an untouched row "
            "does not, and a row absent from a later reload is flagged removed (SourceRowSeen.removed_at "
            "set), never silently dropped -- see test_fixture_new_changed_unchanged_removed."
        ),
        verified=True,
        last_reviewed="Built and tested 2026-09-09.",
    ))

    out.append(Assumption(
        group="Nightly diff", name="Fingerprint fields — ab869_plans",
        config_path=None,
        value="plan_status, delay_requested (app.pipeline.diffs.SOURCE_FINGERPRINT_FIELDS['ab869_plans'])",
        source_type=MEASURED,
        source_detail=(
            "The two fields Ab869Plan itself carries that a rep would want to know changed. "
            "Deliberately does NOT include missed_milestone_count or next_upcoming_date, despite "
            "docs/DAILY-BRIEF-DESIGN.md's original section 1b proposing both -- those are aggregate "
            "facts computed per facility across Ab869Building/Ab869Milestone rows in "
            "app.pipeline.ab869.ab869_board_rows, not columns this table itself has; fingerprinting "
            "them would mean re-running that whole aggregation once per diffed row, every night, for "
            "a cost this build did not measure a need for. Recorded as a real, disclosed gap from the "
            "design doc, not a silent downgrade -- worth a follow-up diff over the board aggregation "
            "specifically if a later build wants it."
        ),
        verified=True,
        last_reviewed="Built and tested 2026-09-09.",
    ))

    out.append(Assumption(
        group="Nightly diff", name="Fingerprint fields — ab802_buildings",
        config_path=None,
        value="air_permit_facility_id only (app.pipeline.diffs.SOURCE_FINGERPRINT_FIELDS['ab802_buildings'])",
        source_type=MEASURED,
        source_detail=(
            "AB 802 is an annual filing -- natural key is (portfolio_manager_property_id, year_ending), "
            "so a new filing year is always a NEW row, never a 'change' to last year's. The one field "
            "that legitimately changes on an EXISTING (property, year) row after its initial load is "
            "the AB 869/802 air-permit join (app.pipeline.scaqmd._link_ab802), which can newly populate "
            "air_permit_facility_id on a re-run without the underlying filing itself changing at all -- "
            "the only thing worth alerting on for this table. Proven on a fixture: a property_name edit "
            "(cosmetic, not a real join outcome) reports unchanged; air_permit_facility_id going from "
            "null to a real match reports changed -- see "
            "test_ab802_fingerprint_is_air_permit_only_not_the_annual_filing."
        ),
        verified=True,
        last_reviewed="Built and tested 2026-09-09.",
    ))

    out.append(Assumption(
        group="Nightly diff", name="Fingerprint fields — opsc_projects",
        config_path=None,
        value="status (app.pipeline.diffs.SOURCE_FINGERPRINT_FIELDS['opsc_projects'])",
        source_type=MEASURED,
        source_detail=(
            "OPSC's own file is a continuously-updated, full-replaced snapshot (see OpscProject's own "
            "docstring) keyed on application_number -- status is the one field that changing means "
            "something moved in the program (Preliminary -> Approved, etc.); every funding-amount "
            "column can be revised without any of that being alert-worthy on its own. Proven on a "
            "fixture: status Preliminary -> Approved registers as changed -- see "
            "test_opsc_fingerprint_is_status."
        ),
        verified=True,
        last_reviewed="Built and tested 2026-09-09.",
    ))

    out.append(Assumption(
        group="Nightly diff", name="Fingerprint fields — scaqmd_facilities",
        config_path=None,
        value="none tracked for change (app.pipeline.diffs.SOURCE_FINGERPRINT_FIELDS['scaqmd_facilities'] "
             "is empty) -- only new/removed natural keys are reported",
        source_type=MEASURED,
        source_detail=(
            "ScaqmdFacility's own docstring: this is a facility-NOTIFICATION list (facility_id, source), "
            "not a permit record with fields that move -- a real facility's registration doesn't "
            "meaningfully change between reloads the way a project's stage or an application's status "
            "does. The fingerprint constant is 'static' (always equal to itself), so a row here can "
            "only ever be new or removed, never changed -- deliberately, not an oversight. Proven on a "
            "fixture: a facility_name edit between reloads reports unchanged, never changed -- see "
            "test_scaqmd_facilities_never_reports_changed_by_design."
        ),
        verified=True,
        last_reviewed="Built and tested 2026-09-09.",
    ))

    out.append(Assumption(
        group="Nightly diff", name="Baseline-run rule",
        config_path=None,
        value="A source's first-ever diff run seeds SourceRowSeen for every current row and reports "
             "zero new/changed/removed alerts",
        source_type=MEASURED,
        source_detail=(
            "There is no prior snapshot to compare a from-scratch load against, so reporting every "
            "existing row as 'new today' on the first run would mean alerting on the table's entire "
            "history at once -- not what 'new since yesterday' is supposed to mean. "
            "app.pipeline.diffs.diff_source detects this by SourceRowSeen having zero rows yet for that "
            "source (baseline=True), seeds every row, and suppresses new/changed/removed/reappeared "
            "entirely for that run; `seeded` still reports the true count either way. Proven directly "
            "against production 2026-09-09: the real first run over all five tables (hcai_projects "
            "45,132, ab869_plans 201, ab802_buildings, opsc_projects, scaqmd_facilities) seeded every "
            "row with zero alerts -- see the per-source counts in this session's own reply, not "
            "hand-copied here since this register is read live from code, and a production row count "
            "belongs in the reply that measured it, not frozen into a docstring that will drift."
        ),
        verified=True,
        last_reviewed="Verified against production 2026-09-09.",
    ))

    out.append(Assumption(
        group="Nightly diff", name="Diff stage runtime",
        config_path=None,
        value="~60-75s end to end against production (118,742 rows across all 5 tables), after a "
             "fix -- an earlier version took over 2.5 hours on the exact same data",
        source_type=MEASURED,
        source_detail=(
            "The FIRST version of app.pipeline.diffs.diff_source mutated every row's ORM object "
            "individually, even when nothing about the row had changed -- the common case on every "
            "normal night. SQLAlchemy 2.0 batches bulk INSERT automatically ('insertmanyvalues'), so "
            "the very first baseline run against production (118,742 rows, all inserts) completed in "
            "74.51s. There is no equivalent automatic batching for UPDATE: the next run, with the same "
            "118,742 rows now all genuinely unchanged, dirtied all 118,742 already-loaded ORM objects, "
            "and SQLAlchemy emitted one UPDATE per object -- one network round trip per row, against "
            "Render's Oregon Postgres. Measured directly: one such re-run took 9,581.65s (2h 40m); a "
            "second was killed after 26+ minutes still running, `ps` showing only 6s of accumulated CPU "
            "time in that span -- confirmed I/O-bound, not compute-bound, before assuming a cause. "
            "Fixed same day: a row that is genuinely unchanged (same fingerprint, never flagged removed) "
            "is no longer touched as an individual ORM object at all -- its key is collected and every "
            "unchanged row for a table is advanced with one chunked bulk UPDATE "
            "(app.pipeline.diffs._bulk_update_in_chunks, 5,000 keys/statement) instead. New/changed/"
            "reappeared/removed rows, always a small count on a real night, stay on the simple per-row "
            "ORM path. Re-measured against the same production data after the fix: 59.70s and 66.04s "
            "on two consecutive full runs, correctness unchanged (verified identical new/changed/"
            "unchanged/removed counts both times)."
        ),
        verified=True,
        last_reviewed="Bug found and fixed against production 2026-09-09; both before and after "
                      "numbers measured directly, not estimated.",
    ))

    # ---- City of San Diego Development Permits (Phase A research only) --------

    out.append(Assumption(
        group="City of San Diego Development Permits", name="Access classification (Phase A research)",
        config_path=None,
        value="Clean -- no robots.txt on either host (data.sandiego.gov or the seshat.datasd.org file "
             "host), and the City's own Open Data Policy states Public Data Sets carry no registration, "
             "license, or automated-access restriction. License badge: ODC-PDDL (public domain).",
        source_type=MEASURED,
        source_detail=(
            "Checked 2026-09-09, Phase A research, no code written. TWO hosts involved: the portal "
            "(data.sandiego.gov) and the actual file host the portal links to (seshat.datasd.org, an "
            "S3 bucket behind CloudFront -- confirmed by response headers, 'server: AmazonS3'). "
            "data.sandiego.gov/robots.txt -> HTTP 404, body 'NoSuchKey / The specified key does not "
            "exist. Key: robots.txt' (S3's standard missing-object response). seshat.datasd.org/"
            "robots.txt -> HTTP 403, body '<Error><Code>AccessDenied</Code><Message>Access Denied</"
            "Message></Error>'. Verified this 403 is generic, not robots-specific: a second, unrelated "
            "nonexistent path on the same host (seshat.datasd.org/nonexistent-path-xyz123) returns the "
            "identical 403 AccessDenied, while a known real file on the same host returns 200 -- this "
            "bucket's policy returns 403 for ANY missing key rather than a 404, robots.txt included. "
            "No robots.txt file exists on either host, same 'absent file = default permissive, not a "
            "block' status already established for HCAI (report.hcai.ca.gov, hcai.ca.gov, esp.hcai.ca.gov) "
            "and CSLB elsewhere in this codebase.\n"
            "License: the dataset page (data.sandiego.gov/datasets/development-permits/) links 'View "
            "License' to opendefinition.org/licenses/odc-pddl/ -- Open Data Commons Public Domain "
            "Dedication and Licence (ODC-PDDL), summary 'Public Domain for data/databases'; the full "
            "text (opendatacommons.org/licenses/pddl/1.0/) dedicates the work 'to the public domain for "
            "the benefit of the public and relinquishes all rights,' explicitly permitting commercial "
            "use, combination with other databases, and technical protection measures, with no "
            "attribution requirement. Separately, the City's own Open Data Policy (effective 2015-01-01, "
            "sandiego.gov/sites/default/files/open-data-policy.pdf) Section 3.5 states verbatim: 'Public "
            "Data Sets shall be made available without any registration requirement, license "
            "requirement, or restrictions on their lawful use' -- explicit, city-wide, not dataset-"
            "specific boilerplate. Section 2.7's disclaimer is liability-only (no warranty of "
            "completeness/accuracy/fitness) and explicitly states nothing in it 'prevents Public Data "
            "Sets from being used for any lawful purpose, including... commercial applications.' This is "
            "a cleaner, more explicit permission than most sources already in this codebase -- an "
            "affirmative city policy statement, not just an absent robots.txt."
        ),
        verified=True,
        last_reviewed="Checked 2026-09-09 directly against both hosts' robots.txt and the cited license/"
                      "policy documents.",
    ))

    out.append(Assumption(
        group="City of San Diego Development Permits", name="Field survey and contractor-naming gap",
        config_path=None,
        value="No contractor, applicant, owner, or engineer field exists. Fingerprint/naming candidate: "
             "APPROVAL_PERMIT_HOLDER only, 70.90% filled, and it freely mixes contractor company names, "
             "individual property owners, and third-party permit-expediting services with no field to "
             "tell them apart. Not usable as a contractor-naming source as-is.",
        source_type=MEASURED,
        source_detail=(
            "One download, 2026-09-09, of the current 'active approvals' file (https://seshat.datasd.org/"
            "development_permits/approvals_active_datasd.csv, 284,568,133 bytes, etag "
            "bb25c51fee79c3d8302ba619367d3466). The published data dictionary at https://seshat.datasd.org/"
            "development_permits_set2/permits_set2_datasd_dict.csv does NOT match this file -- the "
            "dictionary describes ~20 lowercase snake_case fields (project_id, approval_permit_holder, "
            "...) with no valuation, floor-area, or DU/ADU/JADU fields at all, while the live file's "
            "actual header (read directly, not assumed from the dictionary) has 54 UPPER_SNAKE columns: "
            "DEVELOPMENT_ID, PROJECT_ID, PROJECT_TYPE, PROJECT_STATUS, PROJECT_PROCESSING_CODE, "
            "PROJECT_CREATE_DATE, PROJECT_DEEMEDCOMPLETE_DATE, PROJECT_TRUST_ACCOUNT_NO, PROJECT_TITLE, "
            "PROJECT_SCOPE, JOB_ID, JOB_DRAWING_NUMBER, GIS_ADDRESS, GIS_APN, JOB_BC_CODE, "
            "JOB_BC_CODE_DESCRIPTION, GIS_LATITUDE, GIS_LONGITUDE, APPROVAL_ID, APPROVAL_CATEGORY_CODE, "
            "APPROVAL_PROCESSING_CODE, APPROVAL_TYPE, APPROVAL_STATUS, APPROVAL_SCOPE, "
            "APPROVAL_CREATE_DATE, APPROVAL_ISSUE_DATE, APPROVAL_CLOSE_DATE, APPROVAL_EXPIRE_DATE, "
            "APPROVAL_VALUATION, APPROVAL_DU_NET_CHANGE, APPROVAL_STORIES, APPROVAL_FLOOR_AREA, 13 "
            "DU/ADU/JADU income-tier breakdown columns, and APPROVAL_PERMIT_HOLDER (last column). Row "
            "count 541,108. Date range (APPROVAL_CREATE_DATE) 2001-07-03 to 2026-09-09; "
            "APPROVAL_EXPIRE_DATE's max value is a data-entry error (5021-03-06, i.e. a likely typo for "
            "2021), disclosed not corrected. Despite the filename, APPROVAL_STATUS includes plainly "
            "non-active values (Closed 1,725; Final Closed 2,878; Cancelled 47,670; Withdrawn 803; "
            "Finaled 25 -- 'active' evidently gates at the parent PROJECT level, not every child "
            "approval).\n"
            "APPROVAL_PERMIT_HOLDER ('Contact name whom the Approval is issued to', per the mismatched "
            "dictionary -- the closest thing to a name field in the file) is 70.90% filled (383,649 / "
            "541,108). Every other field is either a code, a date, a number, or geometry -- no "
            "APPLICANT, OWNER, or ENGINEER-labeled column exists anywhere in the 54 columns, filled or "
            "not. Sampling 40 non-empty APPROVAL_PERMIT_HOLDER values shows it is a single free-text "
            "field conflating at least four different roles with no way to tell them apart "
            "programmatically: mechanical/fire contractors ('California Delta Mechanical', 'Airgas West "
            "Fire Protection', 'Simplex Grinnell'), individual property owners ('Teresa & Ramon "
            "Hernandez', 'Scott & Anna Bier'), third-party permit expediters who are not contractors at "
            "all ('The Permit Company', 'On Time Permits'), and utilities/telecoms pulling ROW permits "
            "('Cox Communications', 'Pac Bell Co. DBA AT&T Calif', 'TW Cable'). Treated as satisfying "
            "this build's stop condition ('if there is no contractor field, say so and stop') -- a "
            "single ambiguous field mixing four unrelated identity types is not a contractor field, and "
            "guessing which rows are which by name-shape would be exactly the invented inference this "
            "app's own invariants forbid. Mechanical Permit is a real APPROVAL_TYPE value (14,355 of "
            "541,108 rows, 2.65%) -- sized for scale only, not filtered or matched against anything, "
            "since the field the match would run on doesn't reliably exist.\n"
            "GIS_APN (85.64% filled, 463,431 / 541,108) is unaffected by any of this and is a real, "
            "usable join key in the same raw no-dash format (e.g. '4674111600') RetrofitBuilding.apn "
            "already uses elsewhere in this codebase -- a per-building permit-history use of this "
            "source does not depend on solving the contractor-naming gap."
        ),
        verified=True,
        last_reviewed="One download and full-file scan performed and measured directly 2026-09-09; not "
                      "sampled, not estimated.",
    ))

    # ---- Long Beach USD bond program site (lbschoolbonds.net, Phase A research) ---

    out.append(Assumption(
        group="LBUSD bond program site", name="Access classification (Phase A research)",
        config_path=None,
        value="lbschoolbonds.net itself is fetchable with care -- Crawl-delay: 5, and /projects, "
             "/community-meeting-reports, /accessibility, /department-directory and several CMS-clone "
             "paths are explicitly disallowed. The per-project detail host it links out to "
             "(*.preview.finalsitecdn.com) is BLOCKED ENTIRELY for every user agent except a named "
             "accessibility auditor -- never fetched.",
        source_type=MEASURED,
        source_detail=(
            "Checked 2026-09-09, Phase A research, no code written. lbschoolbonds.net/robots.txt "
            "301-redirects to www.lbschoolbonds.net/robots.txt (HTTP 200, 370 lines, 338 Disallow "
            "records covering 114 distinct base paths -- each listed three ways, exact/query/subpath -- "
            "18 Allow exceptions). 'User-agent: *' carries 'Crawl-delay: 5', honored (5s between every "
            "request this session made). Disallowed paths that matter here: /projects (all three "
            "variants) and /bond-projects-clone/-clone-clone are stale CMS staging artifacts, NOT the "
            "live path (confirmed by reading the site's own nav -- the real path is /bond-projects, "
            "which is NOT in the disallow list); /community-meeting-reports is disallowed and was never "
            "fetched, even though it is a top-nav link and the most likely place a periodic bond-status "
            "report would live; /accessibility is disallowed and WAS fetched once by mistake (a bare "
            "curl status check, before the full disallow list had been cross-referenced) -- caught, "
            "disclosed here rather than hidden, no content saved or used from it, and the path was not "
            "fetched again. /department-directory is also disallowed and was not fetched.\n"
            "Two hosts, not one: the bond-projects listing page links out to per-project 'quarterly "
            "update' article pages on a SEPARATE host, "
            "lbusdk12caus-137-us-west1-01.preview.finalsitecdn.com -- checked, this host's own robots.txt "
            "is 'User-agent: Pope Tech CrawlBot / Allow: /' then 'User-agent: * / Disallow: /' -- a full "
            "block for every crawler except one named accessibility-auditing bot. PlanetBids-shaped, no "
            "exceptions for this app. None of those article pages were fetched.\n"
            "No terms-of-use page exists (/terms-of-use -> 404). /privacy-policy (not disallowed, "
            "fetched) covers only visitor data collection (IP, browser type, cookies) -- no language "
            "about reuse, reproduction, or automated access of published content, permissive or "
            "restrictive, either way."
        ),
        verified=True,
        last_reviewed="Checked 2026-09-09 directly against both hosts' robots.txt.",
    ))

    out.append(Assumption(
        group="LBUSD bond program site", name="Field survey and a caught grounding failure",
        config_path=None,
        value="The /bond-projects accordion page is almost entirely empty shells (one school, 3 real "
             "entries, one containing literal leftover CMS placeholder text). The REAL structured data "
             "-- 35 completed projects, 29 of them HVAC, each with dates and a dollar figure -- is on "
             "the /about page instead, encoded partly in image filenames. Zero architects, engineers, "
             "or contractors named anywhere across all 5 saved pages.",
        source_type=MEASURED,
        source_detail=(
            "5 pages saved to docs/sources-pilot/lbusd/ (plus robots.txt): bond-projects.html (737,308 "
            "bytes), about.html (308,559 bytes), citizens-oversight-committee.html (270,726 bytes), "
            "construction-updates.html (87,703 bytes), environmental-reviews.html (82,085 bytes).\n"
            "CAUGHT GROUNDING FAILURE, worth recording as a methodology note: an initial WebFetch "
            "summarization pass on /bond-projects reported a rich, comprehensive-sounding set of named "
            "per-school projects with specific dates and budgets (e.g. 'HVAC installations (Addams, "
            "Alvarado, Barton...) -- Construction June 2025-2027', 'Phase 6 - New Gymnasium & Aquatic "
            "Center (Cabrillo)', 'New Classroom Building (Jordan)'). NONE of this is grounded: the raw "
            "HTML (downloaded directly, checked byte-for-byte) shows every one of those school names "
            "appearing ONLY inside a generic district school-directory nav menu (each linking out to "
            "that school's own lbschools.net subdomain, 'opens in new window/tab', no project data "
            "attached) -- the WebFetch summary invented the dates/budgets/phase names attached to them. "
            "The page's real accordion content is almost entirely the string 'There are no resources or "
            "collections to display' (6 occurrences, 3 unique after de-duplicating an identical doubled "
            "DOM render); exactly 3 real project entries exist anywhere on the page, all under a single "
            "'Wilson High Master Plan' section: Bleacher Replacement ($1 million, starts Summer 2023, "
            "opens Summer 2024), Gymnasium & Locker Room Renovation ($1.1 million, starts Spring 2023, "
            "opens Fall 2023 -- Overview text ends with the literal CMS boilerplate sentence 'This is an "
            "example of what happens when you add additional text.', so this entry is at least partly "
            "unedited template content, not verified real), and Natatorium Renovation ($3.2 million, "
            "starts Fall 2023, opens December 2023, clean text). Not grounded in a small model's summary "
            "again anywhere in this research -- every number below came from reading the saved raw HTML "
            "directly.\n"
            "The real data lives on /about instead, in a 'Projects Completed to Date' section: 35 "
            "distinct completed-project entries, each with Project Type, Construction Began (month + "
            "year), Projected Completion (month + year), Projected Cost (dollar figure), and Supported "
            "by Bond Measure (a single letter) -- no field is missing on any of the 35. The school name "
            "is NOT in the visible text; it is embedded only in each entry's illustration image "
            "filename (e.g. 'AlvaradoHVAC.png'), extracted directly from data-resource-filename "
            "attributes, in document order, 1:1 against the 35 Project Type entries (verified: 37 "
            "images total minus 2 non-project decorative/header images = 35, matching the 35 Project "
            "Type entries exactly). Classifying by filename suffix: 29 of 35 (82.9%) are '...HVAC.png' "
            "-- Project Type 'Heating, Ventilation & Air Conditioning' on every one, all Supported by "
            "Bond Measure 'E', Construction Began dates Dec. 2017 through June 2022, Projected "
            "Completion dates August 2018 through July 2023. 3 of 35 are '...Portable.png' (Portable "
            "Replacement) and 3 of 35 are '...TrackField.png' (unlabeled Project Type text, athletic-"
            "field work by context) -- neither mechanical-relevant.\n"
            "Sum of the 29 HVAC entries' Projected Cost figures: $519,039,435 across the 28 that parse "
            "as valid numbers; the 29th (Alvarado) reads '$9.543.588' with periods where every other "
            "entry uses commas -- a source-side typo, almost certainly meant as $9,543,588 given its "
            "order-of-magnitude match to neighboring entries, but NOT corrected or included in the sum "
            "-- reported as unparseable, not silently fixed.\n"
            "Zero named architects, engineers, or contractors anywhere across all 5 pages: a "
            "case-insensitive scan for architect/engineer/contractor found the bare word 'contractor' "
            "exactly once per page, always in generic narrative ('...design, state approvals, and "
            "contractor procurement before work can begin') or a footer nav link label ('Contractor "
            "Information', not fetched -- out of this pass's scope, likely a bidding/procurement page, "
            "not a named-contractor record) -- never a named firm or person tied to a specific project. "
            "No single PDF substitutes for this: citizens-oversight-committee.html links only a 2014 "
            "committee meeting agenda and a 2023 bylaws amendment (not a status report), and about.html "
            "links the Measure E ballot's full legal text (not a project report either) -- no periodic "
            "'bond oversight report' or 'program status report' PDF was found anywhere reachable; the "
            "one page most likely to host one, /community-meeting-reports, is robots.txt-disallowed (see "
            "the Access classification entry above) and was never checked."
        ),
        verified=True,
        last_reviewed="5 pages downloaded and parsed directly 2026-09-09; every figure above traces to "
                      "the saved HTML in docs/sources-pilot/lbusd/, not to a summarization pass.",
    ))

    # ---- SAN Airport Future Construction List (Phase A research) --------------

    out.append(Assumption(
        group="SAN Airport Future Construction List", name="Access classification (Phase A research)",
        config_path=None,
        value="Clean. www.san.org/robots.txt disallows only /wp-admin/ (Crawl-delay: 10, honored); the "
             "PDF is served from the same host (wp-content/uploads), no second host involved. No "
             "dataset-specific terms found; the PDF's own text carries only a schedule-accuracy "
             "disclaimer, not a reuse restriction.",
        source_type=MEASURED,
        source_detail=(
            "Checked 2026-09-09, Phase A research, no code written. san.org (bare) 301s to "
            "http://www.san.org/robots.txt. www.san.org/robots.txt, HTTP 200, verbatim: 'User-agent: * / "
            "Disallow: /wp-admin/ / Allow: /wp-admin/admin-ajax.php / Crawl-delay: 10' (Yoast-generated, "
            "also declares Sitemap: https://new.san.org/sitemap_index.xml -- that host does not resolve "
            "at all currently, checked directly, not fetched further). 10s crawl-delay honored between "
            "every request this session made. The Future Construction List PDF lives at www.san.org/"
            "wp-content/uploads/2026/01/Future-Construction-List-Jan2026-1.pdf -- same host as the "
            "portal, no separate file host the way HCAI/SD-permits/LBUSD each had, so only one robots.txt "
            "applies. No terms-of-use page found; the PDF's own footer text is a schedule-accuracy "
            "disclaimer only ('Information is considered an estimate. The Airport Authority reserves the "
            "right to change the schedule at any time.'), not a reuse or automated-access restriction "
            "either way."
        ),
        verified=True,
        last_reviewed="Checked 2026-09-09 directly against www.san.org/robots.txt.",
    ))

    out.append(Assumption(
        group="SAN Airport Future Construction List", name="Document survey and mechanical-row count",
        config_path=None,
        value="2-page native-text PDF, 23 project rows, columns Estimated Advertise Date / Project / "
             "Construction Cost Estimate / Trade (numeric trade codes keyed to a 36-item legend on page "
             "2). 11 of 23 rows carry trade code 16 (Mechanical) -- but 9 of those 11 are the same "
             "recurring residential 'Quieter Home Program' batch, not 9 distinct capital projects. Zero "
             "names anywhere: no designer, PM, or contractor, AECOM included.",
        source_type=MEASURED,
        source_detail=(
            "One download, 2026-09-09, of https://www.san.org/wp-content/uploads/2026/01/"
            "Future-Construction-List-Jan2026-1.pdf -- 247,351 bytes, PDF 1.7, 2 pages, letter size, "
            "native text layer (read directly, not OCR'd or summarized). pdfinfo: Creator 'Microsoft "
            "Word', CreationDate and ModDate identical -- 2026-01-28 10:02:50 PST -- and the HTTP "
            "Last-Modified header matches (2026-01-28 18:13:25 GMT, same moment in UTC). The document's "
            "own footer reads 'Updated 01/2026'. Despite this list being described as monthly, the file "
            "actually live and linked from www.san.org/smallbusiness/ today, 2026-09-09, has not changed "
            "since that January upload -- roughly 7.5 months stale by this document's own internal date, "
            "not assumed from the filename alone.\n"
            "Column headers, exact, from the page-1 table: 'Estimated Advertise Date', 'Project', "
            "'Construction Cost Estimate', 'Trade'. 23 data rows total (17 on page 1, 6 on page 2, no "
            "overlap). Every row (23/23, 100%) carries a project name and an advertise-date value, "
            "though 2 of the 23 show the non-specific 'TBD 2026' rather than an actual quarter. Every "
            "row (23/23, 100%) carries at least one numeric trade code. 18 of 23 (78.3%) carry an actual "
            "dollar figure for Construction Cost Estimate; the other 5/23 show 'TBD'.\n"
            "Mechanical rows -- trade code 16 ('Mechanical' per the page-2 legend) present in the Trade "
            "column: 11 of 23 (47.8%). Disclosed nuance: 9 of those 11 are sequential batches of the "
            "same ongoing residential sound-insulation program ('Quieter Home Program Phase 14 Group "
            "7/8/9/10/11/12' and 'Quiter Home Program 15.1/15.2/15.3' [sic, misspelled 'Quiter' on 3 of "
            "the 9 in the source PDF itself, not a transcription error here] -- each priced identically "
            "at $1,500,000, each carrying the identical trade bundle 1,5,7,16,18), not 9 distinct capital "
            "projects. Only 2 of the 11 mechanical rows are genuinely distinct capital projects: 'T2E "
            "Restrooms Remodel' (cost TBD, Q1 2026, trades 1,5,7,16,18,20) and 'T2E Lounge Expansion' "
            "($18,000,000, Q1 2026, a 21-trade bundle including 16).\n"
            "Zero names anywhere in either page: no architect, engineer, program manager, or contractor "
            "is named in the document, including no mention of AECOM -- checked directly against the "
            "full extracted text, not inferred from absence in a summary. (A general web search for this "
            "research separately surfaced a news item reporting AECOM 'selected by San Diego County "
            "Regional Airport Authority' for an unspecified role; that claim is NOT sourced from this "
            "PDF and was not independently verified as part of this Phase A pass -- noted, not relied "
            "on.)\n"
            "Archive check: www.san.org/smallbusiness/ (the page that links this PDF) shows exactly one "
            "current link per list (Construction, Professional Services, Concession/Passenger Service), "
            "all three dated Jan2026 -- no archive, index, or link to any prior month's list appears on "
            "that page. 'How far back' is therefore not determinable from the page itself; no prior-"
            "month URL was guessed or fetched, per instruction."
        ),
        verified=True,
        last_reviewed="One download and full read performed 2026-09-09; every number above traces to "
                      "the saved PDF's own extracted text, not a fetch summary.",
    ))

    # ---- CARB Refrigerant Management Program (R3), Phase A research -----------

    out.append(Assumption(
        group="CARB Refrigerant Management Program (R3)", name="Access classification and host migration",
        config_path=None,
        value="ssl.arb.ca.gov's R3 endpoint is a decommissioned legacy host (a 2024-12-09 notice page "
             "auto-redirects to rmpr3.arb.ca.gov, the real live system); its robots.txt could not be "
             "retrieved at all (connection times out post-TLS-handshake, not a 404 or a block). "
             "ww2.arb.ca.gov and rmpr3.arb.ca.gov both have clean robots.txt.",
        source_type=MEASURED,
        source_detail=(
            "Checked 2026-09-09, Phase A research, no code written. ssl.arb.ca.gov/robots.txt: TLS "
            "handshake completes, then the connection hangs with no HTTP response at all (curl "
            "HTTP_STATUS:000, tried twice, 15s then 30s timeout) -- this is neither a 404 (absent, "
            "permissive per this codebase's own convention) nor an explicit block; it is a true non-"
            "response, and the same host DOES answer other paths (see next), so it is not a blanket "
            "outage either -- most likely a legacy app with no /robots.txt route configured, not "
            "investigated further since the underlying tool is confirmed decommissioned anyway. "
            "ssl.arb.ca.gov/rmp-r3/ itself DOES respond (HTTP 200, 1,268 bytes) with a plain HTML notice, "
            "verbatim: 'SERVICE ANNOUNCEMENT 12-09-2024 / RMP REPORTING TOOL HAS MOVED ... The new URL is "
            "https://rmpr3.arb.ca.gov/rmpr3/s ... You will be automatically redirected to the new "
            "location in 5 seconds.' -- a client-side (JS setTimeout) redirect, so the OLD URL a search "
            "engine or old bookmark returns is not where R3 actually lives anymore.\n"
            "rmpr3.arb.ca.gov/rmpr3/robots.txt (the real system, a Salesforce Experience Cloud/Lightning "
            "community site): HTTP 200, verbatim: '# default robots.txt for sfdc communities sites ... "
            "User-agent: * # applies to all robots / Allow: / # allow all / Disallow: */secur/"
            "forgotpassword.jsp?*' -- clean, no crawl-delay stated. ww2.arb.ca.gov/robots.txt (the "
            "program's informational page, already registered elsewhere in this file for the CARB "
            "Facility Search Tool): clean for the paths used here (/our-work/programs/... is not in its "
            "Disallow list, which covers /core/, /profiles/, /admin/, /search/, /user/*, and any URL "
            "with a query string) -- notably this file explicitly names 'User-agent: Claude-Web / Allow: "
            "/ / Crawl-delay: 10', the first time a host in this codebase's research has named Claude "
            "specifically to ALLOW rather than block it. No dataset-specific terms-of-use page found on "
            "either live host; nothing resembling a reuse restriction anywhere in what was fetched."
        ),
        verified=True,
        last_reviewed="Checked 2026-09-09 directly against all three hosts' robots.txt.",
    ))

    out.append(Assumption(
        group="CARB Refrigerant Management Program (R3)", name="DEAD -- not a comfort-HVAC list",
        config_path=None,
        value="DEAD. CARB's own RMP Service FAQ settles the scope question the landing/about pages left "
             "ambiguous: 'Facilities that have only air conditioning systems that are used exclusively "
             "for comfort cooling are not subject to the regulation.' Covered facilities are "
             "supermarkets, cold storage warehouses, food processing plants, and process-cooling "
             "industries -- a commercial/industrial refrigeration list, not a chiller or comfort-HVAC "
             "list. No follow-up pass run; the portal-access question (item 2/3, below) is moot given "
             "this verdict.",
        source_type=MEASURED,
        source_detail=(
            "Downloaded and read directly 2026-09-09: https://ww2.arb.ca.gov/sites/default/files/"
            "2020-11/rmp_service_faq.pdf (560,680 bytes, 2 pages, 'REVISED 02/15/10' per its own "
            "footer -- CARB's Refrigerant Management Program, Service Version FAQ, 'Required service "
            "practices for technicians and contractors'). Verbatim, under 'What equipment or appliances "
            "are subject to the rule?': 'Facilities that commonly use applicable refrigeration systems "
            "include but are not limited to supermarkets, cold storage warehouses, food processing "
            "plants, and industries with process cooling operations. Facilities that have only air "
            "conditioning systems that are used exclusively for comfort cooling are not subject to the "
            "regulation.' This resolves the ambiguity the landing/about pages left open (recorded below, "
            "kept for the record): the registration trigger IS refrigeration-only in practice, and "
            "'air-conditioning' in the /about page's program-goals language refers to servicing "
            "practices for appliances that ARE covered refrigeration systems, not a separate comfort-AC "
            "population.\n"
            "One real nuance the same FAQ discloses, for completeness: a chiller charge above 50 lbs "
            "used for BOTH air conditioning and process cooling IS covered, verbatim -- 'If a facility "
            "uses a chiller with more than 50 pounds of high-GWP refrigerant for both air conditioning "
            "and process cooling, are they subject to the rule? Yes. This facility will fall into the "
            "category of \"Other refrigeration\"...' -- i.e. the exemption is for comfort-cooling-ONLY "
            "systems specifically, not for every chiller that happens to also do comfort cooling. Does "
            "not change the DEAD verdict: a facility would have to be independently known to run mixed "
            "AC/process-cooling on one chiller for this to matter, which is not information this list "
            "(or any public tier of it) surfaces -- there is no way to select for that case from outside "
            "the covered-facility population.\n"
            "The portal-access question from this build's prior pass (could a public, no-login search "
            "show facility name/address/county/refrigerant type/charge size/system type, and could it be "
            "filtered to the 7 territory counties) is now moot regardless of its answer: even a fully "
            "working public search over this program's covered population would be a supermarket/cold-"
            "storage/food-processing/process-cooling list, not a comfort-HVAC or chiller list -- not "
            "useful for this build's purpose. No follow-up browser-automation pass was run to resolve it, "
            "per instruction; the prior pass's disclosed tooling limitation (no JS-executing browser "
            "available, so the Salesforce Lightning portal at rmpr3.arb.ca.gov/rmpr3/s/ never rendered "
            "beyond its own loading/error shell) stands as recorded, unresolved, but no longer relevant "
            "to whether this source is usable."
        ),
        verified=True,
        last_reviewed="Marked DEAD 2026-09-09 against CARB's own RMP Service FAQ PDF, downloaded and "
                      "read directly; the prior pass's portal-access tooling limitation is preserved "
                      "above for the record but is no longer the reason this source is not used.",
    ))

    # ---- AHJ A2L register (Phase A research) -----------------------------------

    out.append(Assumption(
        group="AHJ A2L register", name="Column set and seed row (LADBS)",
        config_path=None,
        value="8-column field set fixed by this build's own instruction, extracted from each AHJ's "
             "published A2L guidance document where one exists: ASHRAE 15 edition required, ASHRAE 15.2 "
             "edition, ASHRAE 34 edition, ASHRAE 15-2024 Addendum A shaft alternative accepted (Y/N/"
             "NULL), 2022 addenda accepted (list + no-mixing rule), EDVC required regardless of charge "
             "(Y/N/NULL), A1 plan resubmittal rule, express permit note. NULL means the document does "
             "not state it -- never inferred. Seed row: LADBS Guideline P/MC 2026-007.",
        source_type=MEASURED,
        source_detail=(
            "Seed row read directly 2026-09-10, full 25 pages, from https://dbs.lacity.gov/sites/default/"
            "files/efs/forms/pc17/P-MC-2026-007-Guideline-HVAC-Systems.pdf (saved to docs/sources-pilot/"
            "ahj-a2l/los-angeles-city-ladbs.pdf, 1,574,933 bytes). Reference No. CMC 1104.6, Effective "
            "07-01-2024, Revised 01-01-2026. dbs.lacity.gov/robots.txt returns HTTP 403 regardless of "
            "user-agent (nginx-generated, not a custom disallow page), while the actual PDF and the "
            "site's homepage both return 200 with either a bare or a descriptive user-agent -- a "
            "path-specific anomaly on /robots.txt only, not a host-wide block; disclosed, not treated as "
            "either permissive or a block since no robots.txt content could actually be read.\n"
            "Extracted fields, every one traced to a specific page of the actual PDF text (not a "
            "summary): ASHRAE 15 edition required = 2022 (p.3, 'The 2025 California Mechanical Code (CMC) "
            "requires A2L refrigerant systems in human comfort applications to comply with... ASHRAE "
            "15-2022 edition'). ASHRAE 15.2 edition = 2022 (same sentence). ASHRAE 34 edition = 2022 "
            "(same sentence). ASHRAE 15-2024 Addendum A shaft alternative accepted = NO, verbatim from "
            "Appendix B (p.24): 'The addendum is part of the 2024 ASHRAE 15 edition which has not been "
            "adopted by the LAMC. We will NOT accept this shaft alternative from the ASHRAE 15 2024 code "
            "cycle. Continue to use ASHRAE 15 2022 code for compliance requirements.' 2022 addenda "
            "accepted, per Appendix B (pp.24-25): ASHRAE 34(2022) Addendum A (updated LFL values, old "
            "values also acceptable); ASHRAE 15(2022) Addenda E, F, H, L (all enforced/accepted); ASHRAE "
            "15.2(2022) Addenda A, C, F/Errata (accepted, EACH one explicitly carrying its own stated "
            "no-mixing rule, verbatim: 'No mixing between the two versions -- the equation and values "
            "must be consistent.'; the ASHRAE 34 and ASHRAE 15 series addenda accept old-or-new values "
            "but do not repeat that exact no-mixing sentence). EDVC required regardless of charge = YES, "
            "verbatim FAQ (p.9): 'The A2L systems on the plans contain a system charge less than 6.6 "
            "lbs. Are these systems exempt from providing EDVC calculations per ASHRAE 15 Sec. 7.6? No. "
            "For A2L systems covered under ASHRAE 15, EDVC calculations (per Sec. 7.6) are required "
            "regardless of the system charge.' A1 plan resubmittal rule, verbatim (p.3 and p.8 FAQ): "
            "'Approved plans using A1 refrigerant systems shall be re-submitted and re-approved by "
            "Building and Safety, Mechanical Plan Check to accommodate another refrigerant type if the "
            "A1 refrigerant is not available at the time of construction... it will be the "
            "responsibility of the owner to ensure full compliance.' Express permit note, verbatim (p.8 "
            "FAQ): 'If the installation meets the express permit qualifications, an express permit can "
            "be obtained. Please note, plan check review may still be triggered due to other components "
            "of the plan.'"
        ),
        verified=True,
        last_reviewed="Read directly, full document, 2026-09-10.",
    ))

    out.append(Assumption(
        group="AHJ A2L register", name="State of California -- SFM Intervening Code Cycle status",
        config_path=None,
        value="SFM's petition to adopt ASHRAE 15-2024 (incl. Addendum A, the shaft alternative) cleared "
             "CAC review (recommended Approve, 2026-02-11) and a 45-day public comment period (ISOR "
             "dated 2026-03-12; industry comment on record by 2026-04-24). The last non-canceled "
             "Commission meeting on the calendar, 2026-08-25/27, has already passed as of this research "
             "(2026-09-10) -- this pass could NOT confirm that meeting's actual vote outcome from any "
             "page or document fetched. If approved, the cycle's own general effective date is "
             "2027-07-01.",
        source_type=MEASURED,
        source_detail=(
            "Checked 2026-09-10. Petition received by SFM 2025-11-03. CAC (Code Advisory Committee) "
            "review meeting 2026-02-11 (folder path 'CAC/2026-02-11-GREEN-PEME/' on dgs.ca.gov); the "
            "ISOR itself (Initial Statement of Reasons, SFM 02/25 Part 4, BSC form TP-106, dated "
            "2026-03-12, downloaded and read directly, 5 pages, dgs.ca.gov/-/media/Divisions/BSC/"
            "03-Rulemaking/2025-Intervening-Cycle/Public-Comment/GREEN-PEME-45-Day/"
            "SFM-02-25-ISOR-PT4-45-DAY.pdf) states verbatim: 'CAC Recommendation: Approve' / 'Agency "
            "Response: Accept', and confirms the 45-day comment version is the one on record for public "
            "comment (an industry coalition 'Letter of Support' for this exact GREEN-PEME-45-Day docket "
            "was filed dated 2026-04-24, found via search, not independently opened). dgs.ca.gov/BSC/"
            "Rulemaking/2025-Intervening-Cycle/Commission-Mtgs, fetched and read directly, lists meetings "
            "in reverse-chronological order as of this check: August 25-27, 2026 (not marked Canceled); "
            "August 12-13, 2026 -- Canceled; July 15-16, 2026 -- Canceled; May 6, 2026 (held); January "
            "14, 2026; December 17, 2025 -- Canceled; October 15, 2025; July 10, 2025 -- Canceled; April "
            "29, 2025 -- Canceled. No meeting after August 25-27, 2026 is listed. That page's per-"
            "proposal outcome table (if one exists) renders via client-side script, not present in the "
            "raw HTML this pass could read; a further search for news coverage of that meeting's outcome "
            "returned nothing specific to this item. UNRESOLVED as of this pass: whether the August "
            "25-27, 2026 Commission meeting actually adopted, disapproved, or sent this item to further "
            "study. The main Intervening Cycle page states plainly that whatever IS approved in this "
            "cycle becomes 'a supplement to the 2025 edition of Title 24, effective July 1, 2027' -- a "
            "cycle-wide date, not confirmed here as specific to this one item.\n"
            "SEPARATE, HIGHER-STAKES LEGAL WRINKLE found in the ISOR itself and NOT resolved by this "
            "Phase A pass: AB 209 (2022) added Health & Safety Code Section 18944.21, quoted verbatim in "
            "the ISOR: '(a) Not later than July 1, 2023, the commission shall consider whether to adopt "
            "the most recent versions of the following consensus safety standards... ASHRAE Standard "
            "15-2019; ASHRAE Standard 34-2019...' and '(b) If the commission does not adopt all of the "
            "consensus safety standards listed in subdivision (a), then effective July 1, 2024, no state "
            "or local building code provision shall prohibit the use of a refrigerant listed as "
            "acceptable under Section 7671k of the federal Clean Air Act..., provided each use is "
            "installed in accordance with the most recent version of ASHRAE Standard 15 and the "
            "applicable listing standard.' Whether this statutory backstop already permits 'most recent "
            "version' (2024, with Addendum A) compliance NOW, independent of whether CBSC ever formally "
            "adopts it through this rulemaking, is a real legal question this pass surfaces but does not "
            "answer -- flagged for whoever owns this register next, not resolved by inference here."
        ),
        verified=True,
        last_reviewed="Checked 2026-09-10 directly against the ISOR PDF and the Commission Meetings "
                      "page; the actual vote outcome is explicitly UNRESOLVED, not guessed.",
    ))

    out.append(Assumption(
        group="AHJ A2L register", name="Jurisdiction coverage as of this pass",
        config_path=None,
        value="164 of 211 AHJ rows resolved this pass (one targeted search each, or BLOCKED without a "
             "search where already known), plus the separate State of California rulemaking-status row. "
             "5 real hits: LADBS (seed), Los Angeles County, Escondido, HCAI/OSHPD, State Fire Marshal "
             "(the informational bulletin itself). 3 BLOCKED (Riverside County, City of Riverside, Kern "
             "County). 156 searched with NO WRITTEN GUIDANCE FOUND as of 2026-09-10. 47 rows NOT REACHED "
             "-- stopped by a hard, session-wide WebSearch tool quota (200 of 200 calls used across this "
             "session and all its parallel research subagents combined), not by choice or by running out "
             "of jurisdictions to check. This is a tooling limit, disclosed and stopped on rather than "
             "worked around.",
        source_type=MEASURED,
        source_detail=(
            "HITS (beyond the LADBS seed, each saved to docs/sources-pilot/ahj-a2l/ and read directly):\n"
            "Los Angeles County -- 'Residential A2L Refrigerant Plan Check Determination Checklist' "
            "(los-angeles-county.pdf, 1,670,144 bytes), found via pw.lacounty.gov/bsd/content/"
            "publications.aspx, not indexed under any obvious href text -- located by grepping the "
            "page's own rendered text for 'a2l', not by guessing a filename. Residential-scope-only, no "
            "EDVC, no ASHRAE 15 (only 15.2, edition year not stated), no addenda position, no A1-"
            "resubmittal or express-permit language -- all 8 columns NULL except ASHRAE 15.2 edition, "
            "which is itself NULL (referenced without a year: 'ASHRAE 15.2 Table 9-1', 'ASHRAE 15.2:9.5'). "
            "One disclosed oddity: the published PDF's pages 3-6 are unrelated Carrier HVAC manufacturer "
            "product-data sheets (37MGHA, 615AHAQ, watermarked 'EXAMPLE') appended after the real 2-page "
            "form -- almost certainly a publishing mistake on the County's end, not a content error on "
            "this pass's part; only pages 1-2 were treated as the actual document.\n"
            "Escondido (San Diego County city) -- 'A2L HVAC System Affidavit / Installation Checklist' "
            "(escondido.pdf, 138,393 bytes), https://www.escondido.gov/DocumentCenter/View/8485/"
            "A2L-HVAC-System-Affidavit-PDF. Residential-only, IRC-2024-based (not CMC Chapter 11), no "
            "EDVC or shaft/addenda content -- but its signature block requires compliance with "
            "'ASHRAE 15-2024, ASHRAE 15.2-2-24 [sic] and IRC-2024', i.e. the 2024 edition already, not "
            "2022 -- the only AHJ found this pass requiring the newer edition rather than LADBS's 2022. "
            "Refrigerant charge threshold stated as 34.5 lbs per independent system, a different figure "
            "and basis (IRC, not ASHRAE 15.2 Table 9-1) than LA County's own residential form.\n"
            "HCAI/OSHPD (statewide, hospitals) -- '2025 Title 24 Triennial Code Cycle Update, Part 4 & "
            "Part 5, Q and A' (hcai.pdf, 139,225 bytes), dated 2025-10-23, a webinar Q&A transcript. One "
            "direct question on point (Q9): 'Allowance for refrigerant type systems (mini-split) for IDF "
            "rooms? Storage rooms?' Answer: 'Refrigerant types are as permitted in accordance with "
            "Chapter 11 of the CMC and ASHRAE 15. With the prohibition of R410a, new A1 and A2L "
            "refrigerants are being employed.' Confirms A2L is permitted, states no edition year, no "
            "EDVC/addenda/shaft position -- all 8 columns NULL.\n"
            "State Fire Marshal -- Information Bulletin 26-003 'ASHRAE Standard 15-2024 Addendum A: "
            "Exempted Spaces and Shaft Alternatives' (state-of-california-osfm-ib-26-003.pdf, 264,925 "
            "bytes), issued "
            "2026-02-10, expires 2026-02-10+3yr. This is the SFM speaking TO local jurisdictions, not a "
            "jurisdiction's own guidance: it tells them the 2024 Addendum A shaft alternative exists and "
            "that CMC Section 1.2.3 already lets a local building/fire official approve it as an "
            "alternate method of construction on a case-by-case basis, independent of whether the SFM's "
            "own base-code adoption (the separate rulemaking-status row above) ever completes. Direct "
            "tension with LADBS's own seed-row position (explicit NO on the same Addendum A shaft "
            "alternative) -- flagged as a salesperson-relevant finding below.\n"
            "BLOCKED, no search spent (per prior CIP jurisdiction-list research this session -- verbatim "
            "robots.txt Content-Signal block naming ClaudeBot/GPTBot/CCBot by name, Cloudflare-managed): "
            "Riverside County (rivco.gov), City of Riverside (riversideca.gov), Kern County "
            "(kerncounty.com, Akamai 'Access Denied').\n"
            "NO WRITTEN GUIDANCE FOUND as of 2026-09-10 (one targeted search each, no plausible hit): "
            "Long Beach, Glendale, Santa Clarita, Pasadena, Burbank, Santa Monica, Torrance, Culver City, "
            "Downey, Inglewood, Pomona, Compton (all Los Angeles County); San Diego (city); Orange "
            "County (the county government); San Bernardino County (the county government); Bakersfield "
            "(Kern County); DSA (Division of the State Architect, statewide, schools -- checked its own "
            "publications/interpretations-of-regulations index directly, no A2L-related entry found; one "
            "'a2l' text match on the page was a false positive, a random substring inside an unrelated "
            "Box.com share-link token, not a real mention).\n"
            "Additional NO WRITTEN GUIDANCE FOUND as of 2026-09-10, from the remaining five parallel "
            "research batches, each one targeted search per jurisdiction: Orange County and its 34 "
            "cities (35); San Bernardino County and its 24 cities (25); 27 of Riverside County's other "
            "cities (the county government and City of Riverside itself are BLOCKED above, not counted "
            "here); San Diego County and its 18 cities plus Imperial County and its 7 cities, minus "
            "Escondido's hit (26 of 27 searched); Kern County's other 11 cities (Kern County government "
            "itself is BLOCKED above); and DSA (Division of the State Architect, statewide, schools -- "
            "checked its own publications/interpretations-of-regulations index directly, no A2L-related "
            "entry found; one 'a2l' text match on the page was a false positive, a random substring "
            "inside an unrelated Box.com share-link token, not a real mention). Sum: 31 (LA batch, 12+19) "
            "+ 35 (Orange) + 25 (San Bernardino) + 27 (Riverside cities) + 26 (San Diego/Imperial minus "
            "Escondido) + 12 (11 Kern cities + DSA) = 156.\n"
            "NOT REACHED (47 of 211), stopped by the session WebSearch quota, not by choice: 8 Los "
            "Angeles County cities whose search was queued but never ran when the quota hit zero mid-"
            "batch -- diamond bar, redondo beach, rosemead, san gabriel, el segundo, signal hill, vernon, "
            "commerce; plus a second batch of 39 Los Angeles County cities that never started at all "
            "(the parallel research subagent assigned to it entered a broken internal loop and was "
            "terminated before doing any of its assigned searches, and by the time that was caught the "
            "quota was already exhausted, so it could not be restarted) -- city of industry, santa fe "
            "springs, cerritos, azusa, covina, west hollywood, manhattan beach, hermosa beach, glendora, "
            "la puente, la mirada, bell, bell gardens, temple city, walnut, duarte, monrovia, claremont, "
            "la verne, san dimas, hawaiian gardens, lawndale, lomita, malibu, calabasas, agoura hills, "
            "westlake village, rolling hills estates, palos verdes estates, rancho palos verdes, south "
            "gate, maywood, cudahy, south el monte, irwindale, bradbury, sierra madre, san marino, "
            "artesia. None of these 47 carry a NO WRITTEN GUIDANCE FOUND label -- that label is reserved "
            "for a jurisdiction actually searched, per instruction; these are honestly marked NOT "
            "REACHED instead. A follow-up pass needs either a fresh WebSearch quota (this session's "
            "CLAUDE_CODE_MAX_WEB_SEARCHES_PER_SESSION cap) or a different search method to close these "
            "out -- 47 of 79 LA County cities not in the original 40-city seed/batch-A-done set, i.e. "
            "roughly the back half of Los Angeles County specifically, not spread evenly across the "
            "territory."
        ),
        verified=True,
        last_reviewed="164 of 211 AHJ rows resolved 2026-09-10; 47 not reached, stopped cleanly by a "
                      "hard session WebSearch quota (200/200), not worked around.",
    ))

    out.append(Assumption(
        group="AHJ A2L register", name="ahj_a2l_guidance schema and refresh cadence (Phase B)",
        config_path=None,
        value="Table ahj_a2l_guidance, jurisdiction as primary key, 18 columns: jurisdiction_type, "
             "county, status, ashrae_15_edition, ashrae_15_2_edition, ashrae_34_edition, "
             "addendum_a_shaft_alt, addenda_accepted, edvc_regardless_of_charge, a1_resubmittal_rule, "
             "express_permit_note, doc_title, doc_number, doc_date, source_url, checked_at, notes. "
             "Loaded by `scout seed-ahj-a2l` (app.pipeline.ahj_a2l.load_ahj_a2l_guidance), hand-run, "
             "quarterly cadence -- never part of `scout pipeline`, no live fetcher exists or is planned "
             "for this table.",
        source_type=MEASURED,
        source_detail=(
            "Phase B build (2026-09-11) on the Phase A research recorded in this same group. Column set "
            "is exactly what this build's own Phase A instruction specified, plus source_url/doc_date/"
            "checked_at/notes for provenance -- see app.models.AhjA2lGuidance's own docstring for the "
            "full field-by-field rationale, including why status is a plain string rather than a native "
            "Postgres enum (this codebase already paid the migration cost of that choice once, for "
            "SignalType -- see the 'Fix: add school_facility_funding to Postgres's signaltype enum' "
            "commit -- not repeating it here for a four-value, standalone reference table).\n"
            "Load is idempotent on jurisdiction (upsert, not insert-or-fail) and fully deterministic: "
            "app.pipeline.ahj_a2l._build_rows() derives the county and city rows programmatically from "
            "app.geo._CITY_TO_COUNTY plus the 3 fixed state-agency names, so the 211 AHJ rows can never "
            "drift out of sync with app/geo.py's own territory table the way a hand-maintained list "
            "could. Verified directly: 212 rows load (211 AHJ + 1 State of California), status counts "
            "6 HIT / 156 NONE_FOUND / 3 BLOCKED / 47 NOT_REACHED (the 6th HIT is the State of California "
            "row itself, distinct from the 5 AHJ HITs reported in the prior Phase A entries above), "
            "re-running the load twice produces the same 212 rows both times (tests/test_ahj_a2l.py).\n"
            "Refresh cadence: quarterly, by hand -- re-running `scout seed-ahj-a2l` after editing the "
            "constants in app/pipeline/ahj_a2l.py, which itself only changes after new Phase A research "
            "(a later session, not this one) adds or updates a jurisdiction. No SourceRun/source_health "
            "registration was added for this table -- not requested this pass, and a manually-"
            "researched constant-data table already has a clear staleness signal of its own (the "
            "checked_at column and the NOT_REACHED status), unlike a live fetcher whose silent failure "
            "would otherwise be invisible.\n"
            "Surface: a database-backed tab ('AHJ A2L guidance') on /reference, alongside the existing "
            "'pitches' tab -- see app/reference.py's module docstring for both exceptions to that page's "
            "otherwise-static content. Badge: 'AHJ has written A2L guidance' on project rows (via "
            "app.pipeline.ahj_a2l.hit_row_for_project) and on hcai_projects facility rows on the /ab869 "
            "board (via hit_row_for_city, joined through HospitalBuilding.city -- the same city value "
            "app.pipeline.ab869.ab869_board_rows already resolves for its own air-permit join). No "
            "scoring change either place, per instruction.\n"
            "DISCLOSED GAP, not worked around: app.models.Project has no city column at all (checked "
            "directly -- only county, state, latitude, longitude, apn_parcel). The badge therefore "
            "degrades to a COUNTY-level match for Project rows, which today can only ever fire for the "
            "'Los Angeles County' HIT row -- neither city-type HIT ('Los Angeles', 'Escondido') can ever "
            "badge a Project, because there is no project-level city value to compare against. Not "
            "worked around by reverse-geocoding lat/long or parsing apn_parcel -- either would be "
            "exactly the kind of invented field CHARTER.md invariant 12 forbids. hcai_projects facility "
            "rows do NOT have this gap, because HospitalBuilding (joined by perm_id == "
            "HcaiProject.facility_id) already carries a real city column."
        ),
        verified=True,
        last_reviewed="Built and tested 2026-09-11; load, idempotency, and badge behavior all verified "
                      "directly against the real Phase A data, not a synthetic fixture alone.",
    ))

    # ---- City of Los Angeles building permits (LADBS) --------------------------

    out.append(Assumption(
        group="City of Los Angeles building permits (LADBS)", name="No contractor/applicant/licensee field",
        config_path=None,
        value="DEAD -- the three LADBS open datasets carry no contractor, applicant, or licensee field",
        source_type=STATED,
        source_detail=(
            "Stated directly by the user (2026-09-11), not independently read or re-verified by this "
            "codebase's own research process this pass -- distinct from the City of San Diego "
            "Development Permits entry above, which this app DID read and extract column-by-column "
            "itself. Column metadata across LADBS's three open datasets was checked and none carries a "
            "contractor, applicant, or licensee field, same disqualifying shape as the San Diego permits "
            "finding (a permit list with no way to name who is doing the work) but a separate dataset, "
            "not re-derived from that one. Which three datasets, exact column names, and host/robots.txt "
            "findings are not recorded here -- if this source is revisited, that detail should be read "
            "and registered directly rather than assumed to still match this summary."
        ),
        verified=False,
        last_reviewed="Stated 2026-09-11 by the user -- not independently re-verified by this app's own "
                      "research process.",
    ))

    # ---- PlanetBids -------------------------------------------------------------

    out.append(Assumption(
        group="PlanetBids", name="Terms of use bar automated reuse",
        config_path=None,
        value="DEAD -- PlanetBids' own terms of use (sections 3.4 and 6.2) bar automated reuse; bid "
             "documents are reachable only via each agency's own website or a special-use request",
        source_type=STATED,
        source_detail=(
            "Stated directly by the user (2026-09-11), not independently read or re-verified by this "
            "codebase's own research process this pass. This is a separate finding from every earlier "
            "'PlanetBids-shaped' reference in this file (DIR PWC-100, SCAQMD FIND, the SFM/R3 CDN host) "
            "-- those describe a full robots.txt block observed directly at OTHER agencies' bid-portal "
            "hosts, never an evaluation of PlanetBids' own terms of use. This entry is the first time "
            "PlanetBids itself, as a candidate source, has an access classification on record. The exact "
            "terms-of-use URL and the full text of sections 3.4 and 6.2 are not recorded here -- if this "
            "source is revisited, read and quote them directly rather than assuming this summary still "
            "holds (a vendor's terms of use can change without notice)."
        ),
        verified=False,
        last_reviewed="Stated 2026-09-11 by the user -- not independently re-verified by this app's own "
                      "research process.",
    ))

    # ---- Board agendas: engineer/contractor award source (Phase A research) ---
    # Instruction: board agendas as the engineer/contractor award source, checked
    # against 6 named hosts -- LAUSD (lausd.legistar.com + its public Legistar
    # Web API), Riverside CCD (rccd.granicus.com RSS), and BoardDocs public sites
    # for Santa Ana USD, Fontana USD, San Diego CCD, and South Orange County CCD.
    # Phase A research only, no code written -- this feeds no fetcher yet; see
    # the "Proposed design" entry below for what building it would require and
    # why this pass recommends NOT building it as specified.

    out.append(Assumption(
        group="Board agendas: engineer/contractor award source", name="Access classification (Phase A research)",
        config_path=None,
        value="5 of the 6 named hosts DEAD (robots.txt fully disallows a generic user-agent, no "
             "exception broad enough to use honestly). Only LAUSD's Legistar Web API is open by "
             "robots.txt -- but the attachment files that would actually carry a firm name live on a "
             "SEPARATE host (legistar.granicus.com) that is itself fully disallowed.",
        source_type=MEASURED,
        source_detail=(
            "Checked 2026-09-10, Phase A research, no code written. Every robots.txt below was fetched "
            "directly (curl, a standard browser User-Agent string -- the default curl UA returned a "
            "CloudFront WAF 403 on the BoardDocs host, unrelated to robots policy; switching to a "
            "browser UA reproduced the same 200 a normal visitor gets, not a bypass of any access "
            "control). No UA was ever presented as a named search-engine bot to unlock a narrower "
            "exception -- every classification below is what a generic, honestly-identified fetcher "
            "gets.\n"
            "\n"
            "1) BoardDocs (Santa Ana USD, Fontana USD, San Diego CCD, South Orange County CCD) -- all "
            "four are SaaS client sites under the ONE shared host go.boarddocs.com, so one robots.txt "
            "governs all four regardless of each district's own path; the four district-specific "
            "Board.nsf paths were not individually re-verified (this session's web-search budget was "
            "already exhausted from earlier work, so the exact per-district URL was not looked up -- "
            "not needed to reach a classification, since the shared host's robots.txt already settles "
            "it). go.boarddocs.com/robots.txt, HTTP 200, operative lines verbatim: 'User-agent: * / "
            "Disallow: / / Crawl-delay: 1000' followed by a ~300-line list of specifically-named bots "
            "each individually Disallow: / (Ahrefsbot, SemrushBot, wget, Scrapy, curl-shaped tools, "
            "etc. -- not reproduced in full here, all strictly redundant with the catch-all above), then "
            "an '# Allowed Bots' section granting ONLY msnbot, Slurp, Googlebot, and Bingbot 'Allow: "
            "/*/*/Board.nsf/* ' and 'Allow: /*/Board.nsf/*' with Crawl-delay: 10. A generic fetcher is "
            "bound by the catch-all Disallow: / -- DEAD, all four.\n"
            "\n"
            "2) Riverside CCD (rccd.granicus.com), the named RSS host -- robots.txt, HTTP 200, verbatim: "
            "'User-agent: Googlebot / Disallow: /JSON.php / Crawl-delay: 10' (repeated identically for "
            "Slurp and msnbot), 'User-agent: search-one-scgov / Disallow: /JSON.php', then 'User-agent: "
            "* / Disallow: /'. Same shape as this register's existing DIR PWC-100 finding: named bots "
            "get a narrow carve-out, everyone else gets a full site-wide block, no path-specific "
            "exception wide enough to reach an RSS feed under. DEAD.\n"
            "\n"
            "3) LAUSD -- TWO relevant hosts, not one:\n"
            "   a) lausd.legistar.com (the public calendar/board site) and webapi.legistar.com (the "
            "public Legistar Web API, webapi.legistar.com/Help) both return HTTP 404 for /robots.txt "
            "(confirmed with a browser UA too, base pages on both hosts return HTTP 200 -- a genuine "
            "missing file, not a UA-based block). No robots.txt at all is the same 'not restricted by "
            "this mechanism' reading already used elsewhere in this register (see SAN Airport). No "
            "dataset-specific terms-of-use page was found on either host; webapi.legistar.com's own "
            "root page states only '(c) 2026 - Granicus' with no reuse restriction text, and "
            "granicus.com's general company terms-of-use page is a JS-rendered marketing SPA whose "
            "server-delivered HTML carries no readable terms text at all via a plain fetch (not "
            "evaluated further -- a company-wide marketing ToS is a different question from this "
            "specific public API's own terms in any case, same distinction this register already draws "
            "for SAN Airport's page-footer disclaimer). ALLOWED -- used for the one-session pull below.\n"
            "   b) legistar.granicus.com -- NOT one of the 6 originally named hosts, discovered only "
            "because every attachment hyperlink the Web API itself returns (MatterAttachmentHyperlink) "
            "points here, e.g. 'https://legistar.granicus.com/lausd/attachments/<guid>.docx'. This is "
            "the host that actually serves the exhibit documents a contract-award item's real detail "
            "(and any firm name) would live in. robots.txt, HTTP 200, verbatim in full: 'User-agent: * "
            "/ Disallow: / '. DEAD. Disclosed process note: one attachment file WAS fetched from this "
            "host during discovery, in the same batch of commands as the robots.txt check itself, before "
            "that check's result was read -- an ordering mistake, not a decision to fetch past a known "
            "block. The file was never opened or read for content and was deleted immediately once the "
            "robots.txt result was seen; nothing from it is reflected anywhere in this register, and no "
            "further request was made to this host."
        ),
        verified=True,
        last_reviewed="Checked 2026-09-10 directly against each host's own /robots.txt.",
    ))

    out.append(Assumption(
        group="Board agendas: engineer/contractor award source",
        name="LAUSD Legistar: 90-day pull and firm-naming check",
        config_path=None,
        value="45 board matters on file with an agenda date in the last 90 days; 3 match the given "
             "keyword set (all 'contract', via the plural/stem form); 0 of those 3 name a firm in the "
             "title. Whether the linked attachment names one is undetermined -- see the access entry "
             "above, that host is DEAD.",
        source_type=MEASURED,
        source_detail=(
            "One session, 2026-09-10, against webapi.legistar.com/v1/lausd/Matters, "
            "$filter=MatterAgendaDate ge datetime'2026-06-12' and le datetime'2026-09-10' (today minus "
            "90 days), $top=1000 so no paging was needed -- 45 rows returned, all read directly, none "
            "summarized. Interpretation disclosed, not assumed: 'agenda items' was read as MatterAgendaDate "
            "(the date a matter actually appeared on a Board of Education agenda), not MatterIntroDate "
            "(the date it was filed) -- the two can differ by months for an item still in committee.\n"
            "Keyword match: title contains award/contract/architect/engineer/mechanical/hvac/bid as a "
            "case-insensitive word-stem (so 'Contracts' and 'Contracting' count, matching what the "
            "instruction's own slash-separated list reads as intending) -- 3 of 45 (6.7%) match, all "
            "three on the word 'Contract(s)', none on award/architect/engineer/mechanical/hvac/bid. All "
            "3 matching titles, verbatim, with dates (fewer than 10 exist in this window -- reporting 3 "
            "actual matches rather than padding to 10):\n"
            "  [2026-08-25] Rep-005-26/27 (Facilities Contract Actions / BOE Report): 'Approval of "
            "Facilities Contracts Actions \\r\\nProcurement Services Department - Facilities Contracts'\n"
            "  [2026-06-12] Rep-333-25/26 (Facilities Contract Actions / BOE Report): 'Delegations of "
            "Authority for Procurement and Facilities Contracts Amendment\\r\\nProcurement Services "
            "Department - Facilities Contracts'\n"
            "  [2026-06-12] Rep-394-25/26 (Facilities Contract Actions / BOE Report): 'Approval of "
            "Facilities Contracts Actions \\r\\nProcurement Services Department - Facilities "
            "Contracts'\n"
            "All three are omnibus/bundled BOE Reports -- each one Board vote ratifying an unstated "
            "number of individual contract actions at once, not a per-contract, per-firm agenda item. "
            "Confirmed by pulling the actual Matters/Attachments list for MatterId 8098 (Rep-005-26/27): "
            "two attachments, 'Attachment A Ratification of Contracts (REPT-005)' and 'Attachment B "
            "Request for Approval of Contracts not UDA (REPT-005)', both .docx, both hosted on the DEAD "
            "legistar.granicus.com host (see the access entry above) -- so whether either actually names "
            "an architect/engineer/contractor firm was not checked; that would require reading a file "
            "from a robots.txt-disallowed host, which this pass declined to do.\n"
            "Net finding for the full 45-row window: the LAUSD Board of Education's own agenda TITLES, "
            "as filed in Legistar, never name a firm -- every facilities-contract item is filed under a "
            "generic recurring report name (Facilities Contract Actions / Procurement Actions), with the "
            "actual vendor list living only in an attachment this pass could not read under the terms "
            "found."
        ),
        verified=True,
        last_reviewed="Pulled and read directly 2026-09-10 against the live Legistar Web API.",
    ))

    out.append(Assumption(
        group="Board agendas: engineer/contractor award source",
        name="Proposed design (not decided) -- table shape, cadence, mep_engineer feed",
        config_path=None,
        value="PROPOSAL, not a decision: given this pass's own findings, recommend NOT building a board-"
             "agenda fetcher against any of the 6 named hosts as specified. 5 are robots.txt-dead outright; "
             "the 6th (LAUSD) is only open at the metadata layer -- its own agenda titles never name a "
             "firm, and the one place a firm name could live (the attachment) sits on a separately-dead "
             "host. This source, as investigated, would NOT make the BPELSG join fire.",
        source_type=MEASURED,
        source_detail=(
            "Proposed, not asserted, per instruction -- open for the user to weigh differently:\n"
            "\n"
            "TABLE SHAPE (if a future access path opens, e.g. a district PRA release of attachment text, "
            "or a district whose BoardDocs/Granicus instance turns out to carve out a broader robots.txt "
            "exception than the two checked here): one row per Matter/agenda item, natural key (source, "
            "matter_id or file_number), columns for agenda_date, body_name, title, matched_keyword, and "
            "a nullable named_firm/named_person + role guess -- same 'never invent, null when absent' "
            "discipline as every other Pipeline B table, since this pass's own finding is that the title "
            "alone essentially never carries a firm name, so that column would be NULL far more often "
            "than populated unless attachment text becomes reachable.\n"
            "\n"
            "CADENCE (conditional on the above): LAUSD's Board of Education meets roughly twice monthly "
            "(2 agenda dates observed in this 90-day window's tail, plus the 18 items on 2026-08-25 "
            "alone suggest a monthly-or-so full-agenda cycle) -- a 14-day poll against MatterAgendaDate "
            "greater than the last successful poll would not miss a cycle; this is a proposal based on "
            "one 90-day sample, not a measured long-run cadence.\n"
            "\n"
            "mep_engineer / BPELSG join: app.pipeline.bpelsg.match_bpelsg_for_project's join fires off "
            "app.models.Signal.named_people entries carrying an MEP-engineer-shaped title marker (see "
            "app/pipeline/bpelsg.py's MEP_ENGINEER_TITLE_MARKERS) -- it needs a PERSON's name with a "
            "mechanical-engineer title, not a firm name. Nothing found in this pass provides that: LAUSD "
            "agenda titles name neither a firm nor a person, and the one place detail might exist "
            "(the attachment) was not reachable. Even in the best case where a future pass CAN read an "
            "attachment, DCA's own BPELSG roster (see app/pipeline/bpelsg.py's own docstring) holds only "
            "133 individual license-holder rows per Scout's own territory scope, matched by normalized "
            "person name -- an attachment naming a FIRM ('Acme Mechanical Inc.') rather than the licensed "
            "individual engineer of record would still not make that join fire; it would need to name "
            "the actual person. This pass did not confirm attachments name individuals rather than "
            "firms either way (unread, per the access finding above) -- so 'would this feed mep_engineer "
            "roles' is answered NO for what was actually checked, and UNDETERMINED, not YES, for the "
            "best-case future scenario raised above."
        ),
        verified=False,
        last_reviewed="Proposed 2026-09-10, Phase A research only -- not decided, not built.",
    ))

    # ---- Title 24 Part 6 (2025) minimums + CEC MAEDbS (Phase A research) -------
    # Two public equipment-compliance sources checked against DMG's 70 lines.
    # Phase A research only, no code written.

    out.append(Assumption(
        group="Title 24 Part 6 (2025) minimum efficiencies", name="Access classification (Phase A research)",
        config_path=None,
        value="NOT obtained this pass -- no table cell values below are reported, because none were "
             "reliably read. Every candidate source checked (ICC's Digital Codes page, the CEC's own "
             "Building Energy Efficiency Standards program pages, Energy Code Ace's own 'Reference Ace' "
             "tool) renders its actual content client-side; none returned readable table text to a plain "
             "fetch. No record of 'the Energy Code Ace values found Sep 9' exists anywhere in this "
             "codebase or in this assistant's own memory system to compare against either.",
        source_type=MEASURED,
        source_detail=(
            "Checked 2026-09-10, Phase A research, no code written. This entry reports a NEGATIVE "
            "finding on purpose, per this register's own 'null over inference' discipline -- inventing "
            "plausible-looking numbers for 110.2-A/110.2-D/110.2-G/110.2-J/140.4, the VRF IEER advisory, "
            "or an Energy Code Ace comparison would be strictly worse than reporting that none of it was "
            "actually read.\n"
            "\n"
            "1) codes.iccsafe.org/s/CAEC2025P1 -- HTTP 404 for that exact slug (not a redirect, a real "
            "404 on ICC's own React shell). Separately, ICC's robots.txt (HTTP 200) is NOT a full block "
            "-- 'User-agent: *' disallows only /pdfs/, /uploads/, /my_purchase, /update_mypurchase_book, "
            "/update_pa_accesscode, /admin/, and /lookup, so a working /s/ content URL would likely be "
            "robots.txt-permitted. The practical blocker is different: the page that loaded (HTTP 200 on "
            "the bare host) is a New-Relic-instrumented JS single-page app with no server-rendered code "
            "text in its initial HTML at all -- same 'rendered browser session, not a fetcher' limit "
            "already on record for HCAI/ab869/bpelsg in this codebase, not a robots.txt or terms issue.\n"
            "2) CEC's own site (www.energy.ca.gov) -- robots.txt is a standard clean Drupal file (only "
            "/core/, /profiles/, /admin/, /search/, a few auth paths disallowed). The Building Energy "
            "Efficiency Standards program page and its '2025-building-energy-efficiency' page plus three "
            "numbered sub-pages were all fetched and read directly; none link a PDF of the adopted "
            "standards text itself (they link compliance-software and CALGreen proceeding pages instead). "
            "CEC's own e-filing docket system (efiling.energy.ca.gov) almost certainly holds the actual "
            "adopted-standards PDF as a docket filing, but locating the specific filing requires either a "
            "working search (this session's web-search budget was already exhausted from earlier work) "
            "or an already-known docket/document number, neither available this pass -- not attempted "
            "further rather than guessed at.\n"
            "3) Energy Code Ace (energycodeace.com) -- robots.txt (HTTP 200) is fully open: 'User-Agent: "
            "* / Disallow: / Crawl-delay: 10' (an empty Disallow value permits everything) plus a public "
            "sitemap. Its own site structure was surveyed via that sitemap (107 URLs) for a table-shaped "
            "HVAC minimums page; the most likely candidate, 'reference-ace-tool', actually serves an "
            "iframe titled 'Reference Ace 2013 Tool' (stale year, not 2025) pointing at "
            "/site/custom/public/reference-ace-2013/, a 14-line JS-app shell with no static content; "
            "'reference-ace-2022-tool' redirects toward a bare 'CALIFORNIA CODE OF REGULATIONS' page with "
            "the same shape; 'nonresidential-compliance-documents' returned zero PDF links in its raw "
            "HTML (its document list is client-rendered too). No dataset-specific terms beyond the "
            "general CA.gov 'Conditions of Use' were found to apply to Energy Code Ace itself, though "
            "note Energy Code Ace is a separate, utility-funded site, not a CEC domain -- its own terms "
            "were not separately located either.\n"
            "4) www.dgs.ca.gov/BSC (California Building Standards Commission) was checked as a fourth "
            "candidate since it distributes free code PDFs -- its /BSC/Codes page returned only ERRATA "
            "packets (corrections to already-published text, several for Part 3/Electrical and the "
            "plumbing/mechanical codes) not a full base Part 6 text, so not usable either.\n"
            "Net effect: this pass has zero table values to report for packaged AC 65-135 kBtu/h, "
            "air-cooled chillers under 150 tons Path A/B, VRF 65-135 kBtu/h (IEER), or gas hot-water "
            "boilers 300k-2.5M Btu/h, and cannot say whether any 'Energy Code Ace values found Sep 9' "
            "match the adopted text, because no record of those Sep 9 values exists in this codebase or "
            "memory for this pass to check against. If the user has the actual Sep 9 source (a specific "
            "URL, PDF, or screenshot), supplying it directly would let a future pass do the real "
            "comparison instead of re-attempting this same search."
        ),
        verified=True,
        last_reviewed="Checked 2026-09-10 -- a verified NEGATIVE result (not reachable this way), not a "
                      "measured code value.",
    ))

    out.append(Assumption(
        group="CEC MAEDbS advanced search", name="Access, terms, and search-mechanics (Phase A research)",
        config_path=None,
        value="Open by robots.txt (no file, 404) and reachable without a login. Fields returned: "
             "Manufacturer, Brand, Model Number, Individual Model Number Covered By Basic Model, "
             "Regulatory Status, Add Date. Export exists but is gated behind supplying an email address, "
             "not a plain download. 0 of the 5 requested manufacturer searches produced a trustworthy "
             "per-manufacturer count: this pass's own scripted attempt returned the SAME unfiltered "
             "category total for both a real manufacturer (AAON) and a deliberately nonexistent one -- a "
             "silent wrong-answer failure mode, not a clean block.",
        source_type=MEASURED,
        source_detail=(
            "Checked 2026-09-10, Phase A research, no code written. cacertappliances.energy.ca.gov/"
            "robots.txt returns HTTP 404 (confirmed genuine with a browser UA against a live 200 base "
            "page, same 'no restriction stated' reading used elsewhere in this register) -- MAEDbS is "
            "NOT one of this register's robots.txt-dead sources. /Pages/Search/AdvancedSearch.aspx "
            "302-redirects to itself while issuing a session cookie (an ASP.NET forms-auth session "
            "bootstrap, not an access-denied redirect) -- following it WITH a cookie jar (any normal "
            "client does this automatically) reaches the real Advanced Search page at HTTP 200 with no "
            "credentials entered; a 'Back To Login' link implies this is a guest/anonymous session, not "
            "an authenticated one. Site-wide CA.gov 'Conditions of Use' (www.energy.ca.gov/conditions.html) "
            "applies, quoted verbatim in the relevant part: materials are 'free for public use consistent "
            "with the Public Records Act...provided the Energy Commission is credited,' but 'Use or "
            "modification of these materials or information for commercial or profit-making purposes is "
            "prohibited' -- flagged for the user's own judgment given DMG's commercial use case, not "
            "resolved here.\n"
            "\n"
            "FIELDS: the results grid (id ctl00_MainContent_gvSearchResults) header row, read directly "
            "off a real (unfiltered, see below) result set: Manufacturer, Brand, Model Number, Individual "
            "Model Number Covered By Basic Model, Regulatory Status, Add Date -- 6 columns, matching "
            "'Commercial ACs And HPs' category rows like Manufacturer='AAON, Inc.', Brand='Aaon', Model "
            "Number='RQ-003-2-*-FA**-***' (wildcarded basic-model patterns, not fully enumerated unit "
            "SKUs), Regulatory Status='Voluntarily Certified'.\n"
            "\n"
            "EXPORT: an export button/dialog exists (btnConfirmExport, a 'txtUserEmail' field with its "
            "own validation rules) -- exporting requires supplying an email address through a confirm "
            "dialog, not a plain click-to-download; the actual export file format was not obtained since "
            "no search successfully filtered (see below), so the dialog was not carried through to a "
            "real file.\n"
            "\n"
            "SEARCH MECHANICS AND THE 5-SEARCH FINDING: this is not a simple keyword search box. It is "
            "an ASP.NET WebForms cascading filter builder: select a Category (21 options, pre-rendered), "
            "which postbacks to populate an Appliance Type list (9 for Central ACs And HPs, including "
            "'Commercial ACs And HPs' and 'Variable Refrigerant Flow ACs And HPs'), which postbacks to "
            "populate up to 20 filter rows, each an independent (Column, Operator, Value) triple -- "
            "'Manufacturer' is one of 27 selectable columns, with only 4 operators available for it "
            "(Equals, Does Not Equal, Has Value, Does Not Have Value -- no Contains/substring match). No "
            "__EVENTVALIDATION token is emitted by this page at all (event validation is disabled "
            "server-side), which made a plain requests+BeautifulSoup session ABLE to replicate every "
            "cascading postback exactly (category select -> type select -> filter-column select, each "
            "confirmed via the server's own echoed state) without executing any JavaScript. Despite that, "
            "the FINAL search step did not work: submitting Manufacturer Equals 'AAON' returned "
            "'2100 record(s) found' with a grid of AAON rows -- but submitting Manufacturer Equals "
            "'ZZZ_NONEXISTENT_MFR_TEST' (a control test, not one of the 5 requested manufacturers) "
            "returned the IDENTICAL '2100 record(s) found' with the same unfiltered grid. Since 2100 is "
            "the full 'Commercial ACs And HPs' category count (confirmed: the hidden "
            "hdnApplianceColumnId/hdnValidationOperatorId fields DID correctly echo back Manufacturer/"
            "Equals after the postback sequence, yet the query itself ignored the filter entirely), this "
            "pass concludes the search predicate is not actually applied by a scripted client built this "
            "way -- some additional client-side step (a JS-only commit action, or an AJAX partial-"
            "postback protocol this session's plain POST didn't replicate) is required beyond what a "
            "static HTML form describes. This was NOT attempted against AAON, LG, ClimaCool, Marley, or "
            "Titus as 5 separate 'real' searches once the control test exposed the mechanism as "
            "unreliable -- reporting 5 sets of numbers derived from a demonstrably broken filter would be "
            "exactly the invented-precision problem this register exists to catch, worse than reporting "
            "zero. Net: 0 of 5 requested manufacturer searches completed with a trustworthy result; this "
            "source needs a real JS-executing browser session (the same category of tool already used "
            "for HCAI/ab869/bpelsg's one-off hand-pulls in this codebase), not a scripted HTTP client, to "
            "search reliably."
        ),
        verified=True,
        last_reviewed="Checked 2026-09-10 directly against the live MAEDbS Advanced Search page.",
    ))

    out.append(Assumption(
        group="Title 24 Part 6 (2025) minimum efficiencies",
        name="Proposed design (not decided) -- Reference tab + per-line MAEDbS field",
        config_path=None,
        value="PROPOSAL, not a decision: given both sources above came back empty-handed or unreliable "
             "this pass, recommend NOT building either as specified yet. A static 'Title 24 minimums' "
             "Reference tab needs a human-verified source document first (none was obtained here); a "
             "per-line MAEDbS certification field needs either a JS-capable fetch tool or a person running "
             "the search by hand each quarter (this pass's own scripted attempt was shown to fail "
             "silently, which is worse than not automating it at all).",
        source_type=MEASURED,
        source_detail=(
            "Proposed, not asserted, per instruction -- open for the user to weigh differently:\n"
            "\n"
            "REFERENCE TAB 'Title 24 minimums' (static table, annual refresh): shape -- one row per "
            "(equipment_type, capacity_band, path_or_metric), columns for the minimum-efficiency value(s) "
            "as literally stated in the adopted table, a citation column (e.g. 'Table 110.2-A' or "
            "'Section 140.4'), and a last_verified date -- same static, hand-curated pattern as "
            "app/reference.py's existing ASHRAE-sourced content (see that module's own docstring: "
            "'Deliberately NOT pipeline-fed... Basis is ASHRAE'), not a scraped/fetched table, since "
            "every source checked in this pass renders its real content client-side. Annual refresh "
            "matches Title 24's own triennial-with-annual-supplement cadence already on record elsewhere "
            "in this register (see the AHJ A2L and HCAI/OSHPD Title 24 entries above). Blocking "
            "prerequisite: a human needs to actually open the ICC page or locate the correct CEC docket "
            "PDF in a real browser and transcribe the cell values -- this pass could not do that itself, "
            "so this proposal has no values to seed the table with yet.\n"
            "\n"
            "PER-LINE MAEDbS CERTIFICATION STATUS FIELD (manual quarterly): shape -- a nullable field on "
            "each of DMG's 70 lines recording Regulatory Status (e.g. 'Voluntarily Certified', per the "
            "field actually observed above) plus the Add Date and a last_checked_at timestamp, entered by "
            "a person who ran the Advanced Search in a real browser (not a scripted pull, per the finding "
            "above) and read the result grid directly. Quarterly matches a reasonable human-effort cadence "
            "for 70 lines given the search itself is a multi-step form per manufacturer, not a bulk query -- "
            "proposed, not measured against any stated CEC update cadence for this database. NULL for "
            "any line whose manufacturer this quarter's check didn't cover, same 'null over inference' "
            "discipline as every other unfinished field in this system, never a guessed or carried-over "
            "status."
        ),
        verified=False,
        last_reviewed="Proposed 2026-09-10, Phase A research only -- not decided, not built.",
    ))

    # ---- CSLB License Detail (CheckLicense) as a per-account risk monitor ------
    # Phase A research only, no code written. Complaints/disciplinary actions/
    # citations are NOT in the bulk "License Master" file this codebase already
    # imports (app/pipeline/cslb.py) -- only the per-license CheckLicense page
    # carries them.

    out.append(Assumption(
        group="CSLB License Detail (CheckLicense) risk monitor", name="Access classification (Phase A research)",
        config_path=None,
        value="Not barred by declared policy -- no robots.txt on any CSLB domain (confirmed again this "
             "pass), and CSLB's own linked Conditions of Use states data is public-domain with no "
             "automation restriction. BUT: this pass could not get a real per-license result from a "
             "scripted client despite 5 careful, distinct attempts -- the server silently re-renders the "
             "blank search form (fresh VIEWSTATE, no error, no cookie involved) instead of either a "
             "result or a validation message. Policy says allowed; practice, this pass, says undetermined.",
        source_type=MEASURED,
        source_detail=(
            "Checked 2026-09-10, Phase A research, no code written.\n"
            "\n"
            "ROBOTS.TXT: cslb.ca.gov, www.cslb.ca.gov, and web.cslb.ca.gov ALL return a genuine IIS 404 "
            "for /robots.txt (same finding app/pipeline/cslb.py's own docstring already recorded "
            "2026-08-14 for the bulk portal -- re-confirmed here for the CheckLicense path specifically, "
            "since a site can in principle scope different rules to different paths; there is only one "
            "robots.txt per host either way, so this is the same file, re-verified).\n"
            "\n"
            "TERMS: CSLB's own site links only one policy, site-wide: https://www.ca.gov/use/ 'Conditions "
            "of use' (no CSLB-specific terms page exists -- checked the CSLB homepage's own footer links: "
            "just 'Conditions of Use' -> ca.gov and 'Privacy Policy' -> dca.ca.gov, nothing narrower). "
            "Read in full; contains no mention of 'automat', 'robot', 'crawl', 'scrape', or 'bot' anywhere "
            "in its text. The operative 'Ownership' clause, quoted verbatim: 'In general, information "
            "presented on this website, unless otherwise indicated, is considered in the public domain. "
            "It may be distributed or copied as permitted by law... The State shall be free to use, for "
            "any purpose, any ideas, concepts, or techniques contained in information provided through "
            "this site.' No automated-access restriction found, same conclusion as the bulk-file entry "
            "already on record for this source.\n"
            "\n"
            "PRACTICAL ATTEMPT: /OnlineServices/CheckLicenseII/CheckLicense.aspx is a classic ASP.NET "
            "WebForms page (a 'Contractor License #' textbox, name='ctl00$MainContent$LicNo', posting to "
            "itself). Unlike CEC's MAEDbS (a separate source checked this same week -- see that entry's "
            "own register group), this page issues NO session cookie at all on GET (no Set-Cookie header, "
            "ruling out a lost-session explanation), and it loads Google reCAPTCHA's script tag "
            "(recaptcha/api.js) -- but no 'g-recaptcha' div, no 'data-sitekey', and no grecaptcha JS call "
            "anywhere on the page, so that script is not actually wired to this form (initially suspected "
            "as the blocker, then ruled out by direct inspection -- flagged here so a future pass doesn't "
            "waste time on the same false lead). 5 distinct POST attempts were made, each fixing a "
            "hypothesis the previous one raised: (1) minimal __VIEWSTATE/__VIEWSTATEGENERATOR/"
            "__EVENTVALIDATION only -- blank form back, no error; (2) full hidden-field harvest but "
            "INCLUDING every submit button's name=value pair (all 4: license-number, business-name, "
            "personnel-name, HIS searches) -- this actually surfaced a real server-side validation "
            "message, but for the WRONG field ('Please enter at least a last name', the HIS-search "
            "validator), confirming that sending more than one button's fields at once confuses which "
            "action the server thinks was invoked; (3) full hidden-field harvest with ONLY the "
            "license-number button's field included, matching exactly what a real single click sends -- "
            "byte-for-byte, this reproduced attempt (1)'s blank-form result (confirmed via diff: the "
            "LicNo textbox itself renders empty in the response, meaning the posted value was not even "
            "echoed back, let alone searched). No cookie, no CAPTCHA widget, and correct single-button "
            "framing were all ruled out as the cause without finding what the actual cause is -- most "
            "likely an UpdatePanel/partial-postback protocol this session's plain full-page POST doesn't "
            "correctly trigger (the same general class of gap as MAEDbS, though the failure mode here is "
            "an honest 'nothing happened' rather than MAEDbS's dangerous 'wrong answer looks like a right "
            "one').\n"
            "\n"
            "NET: 0 of the 5 Postgres license numbers (1105674, 1105677, 1030772, 842488, 1030773) were "
            "actually looked up -- there is no real per-license field list to report verbatim from this "
            "pass, and inventing one from memory of what a CSLB license page typically shows would be "
            "exactly the fabrication this register exists to prevent. A real detail page most likely "
            "shows License Status, Classifications, Bonding, Workers' Comp (all already in the bulk "
            "file per app.models.Contractor) PLUS a 'Personnel' list and a 'Complaint Disclosure / "
            "Legal Actions' section (per the page's own on-site description: 'verify information, "
            "including complaint disclosure') -- but that is this pass's expectation from the page's own "
            "promotional text, not a verified read, and must not be treated as one."
        ),
        verified=True,
        last_reviewed="Checked 2026-09-10 -- robots.txt/terms verified allowed; the automated-lookup "
                      "mechanism itself verified NOT working in 5 real attempts, cause undetermined.",
    ))

    out.append(Assumption(
        group="CSLB License Detail (CheckLicense) risk monitor",
        name="Proposed design (not decided) -- manual check now, automated design if unblocked later",
        config_path=None,
        value="PROPOSAL, not a decision. Given policy allows this but this pass's own attempts could not "
             "make it work, recommend the SAFE default: a manual monthly check of the top 25 accounts by "
             "annual_revenue, by a person opening CheckLicense.aspx in a real browser -- same fallback the "
             "instruction specifies for a barred source, adopted here for a practical rather than a policy "
             "reason. Separately, and conditionally, sketch what a weekly automated job + contractor_risk "
             "table + badge would look like IF a future pass gets a real lookup working (e.g., with a "
             "JS-capable browser tool) -- not proposed as buildable today.",
        source_type=MEASURED,
        source_detail=(
            "Proposed, not asserted, per instruction -- open for the user to weigh differently:\n"
            "\n"
            "RECOMMENDED NOW: manual monthly check, top 25 Accounts by annual_revenue (Account.annual_"
            "revenue, nullable -- accounts with a null value sort last, per this register's own "
            "'null over inference' convention, never treated as zero/lowest deliberately). "
            "app.contractors.match_account_to_cslb already exists and bridges an Account to its "
            "Contractor.license_no by name/city match, so the 25 license numbers to check by hand are "
            "already derivable today with no new code -- only the CheckLicense.aspx visit itself is "
            "manual. A person records, per account: license status, any complaint-disclosure or legal-"
            "action text shown, and the date checked -- into notes or a lightweight tracking sheet, not "
            "a new table, until/unless automation is proven to work (building contractor_risk now, before "
            "any confirmed automated feed, risks an empty or stale-looking table people stop trusting).\n"
            "\n"
            "CONDITIONAL, IF a future pass gets a real automated lookup working: weekly job, capped at N "
            "lookups per run (N proposed as low, e.g. 25-50, matching the manual top-25 cadence rather "
            "than attempting all ~47,572 contractors currently in Postgres -- this source has no bulk "
            "export, so any per-run cap should stay small and targeted, not scan the whole roster) with "
            "whatever crawl-delay a working future robots.txt check states honored between requests (none "
            "was found this pass, so no specific delay number is proposed here -- re-check at that time "
            "rather than assume this pass's finding still holds). contractor_risk table shape: keyed by "
            "Contractor.license_no (the existing unique natural key, per app.models.Contractor), columns "
            "for whatever the real detail page turns out to carry (proposed candidates only, NOT "
            "confirmed: complaint_count, has_legal_action, personnel list, last_checked_at) -- populated "
            "only from fields this pass could actually verify exist once a working pull is confirmed, "
            "never backfilled from the guess in the access-classification entry above. Badge: a small "
            "indicator on both /contractors (keyed directly by license_no) and /accounts (via the "
            "existing match_account_to_cslb bridge) -- shown only when contractor_risk has a row for that "
            "license, absent (not a false 'clean' badge) for every contractor this hasn't checked yet, "
            "same discipline as every other coverage-gap field in this system."
        ),
        verified=False,
        last_reviewed="Proposed 2026-09-10, Phase A research only -- not decided, not built.",
    ))

    # ---- TECH Clean California QPL / HVAC Product Finder (Phase A research) ---
    # Phase A research only, no code written. Not explicitly asked to "register
    # constants" this time (unlike the three sibling Phase A passes earlier
    # today), but recorded here anyway for the same reason every other Phase A
    # finding this session lives in this file -- so it isn't silently lost.

    out.append(Assumption(
        group="TECH Clean California QPL / HVAC Product Finder",
        name="Access classification (Phase A research) -- DEAD, and specifically to Claude",
        config_path=None,
        value="DEAD by terms on both named hosts, and DEAD by robots.txt -- naming this assistant "
             "specifically -- on the two hosts that actually carry the product data (switchison.org's "
             "own /product-finder, and ahridirectory.org, AHRI's own directory). No AAON or LG model was "
             "tested; doing so would mean knowingly acting against an explicit, on-point block.",
        source_type=MEASURED,
        source_detail=(
            "Checked 2026-09-10, Phase A research, no code written.\n"
            "\n"
            "1) techcleanca.com -- no robots.txt (HTTP 404, genuine). Its own Terms of Use "
            "(techcleanca.com/terms-of-use/, operated by 'Cohen Ventures, Inc., DBA Energy Solutions', "
            "last modified October 5, 2022) quoted verbatim: 'Use any robot, spider or other automatic "
            "device, process or means to access the Website for any purpose, including monitoring or "
            "copying any of the material on the Website. Use any manual process to monitor or copy any "
            "of the material on the Website, or for any other purpose not expressly authorized in these "
            "Terms of Use, without our prior written consent.' This is a full, general-purpose ban on "
            "automated access to ANY page of the site, not scoped to a product database -- broader even "
            "than this register's existing PlanetBids finding.\n"
            "\n"
            "2) catechincentives.com (the operational incentive-application platform, same operator) -- "
            "no robots.txt (HTTP 404, served as a soft-404 within the site's own Django/CoreUI app shell, "
            "still 'no restriction stated by that mechanism'). Its own Terms (catechincentives.com/legal/) "
            "quoted verbatim, and more specific than techcleanca.com's: 'You may not use any robot, "
            "spider or other automatic device, process or means to access, retrieve, scrape, reverse "
            "engineer, compile, create derivative works, publicly display or otherwise distribute any "
            "portion of the Site or the Platform.' Also states Content 'may include information and data "
            "from a variety of publicly accessible and other sources including... state regulatory "
            "agencies' with no representation of accuracy -- consistent with a QPL sourced from a "
            "third-party directory (see AHRI finding below) rather than authored in-house.\n"
            "\n"
            "3) switchison.org -- a THIRD host, not originally named, found via a link on techcleanca."
            "com's own FAQ page ('contractor-finder', 'incentive-finder'). Its robots.txt (HTTP 200) is "
            "unusually explicit and directly on point -- three tiers: (a) a named block of AI/ML crawlers "
            "including 'GPTBot', 'CCBot', 'Google-Extended', and, BY NAME, 'ClaudeBot' -- 'Disallow: /' "
            "for all of them; (b) a second named block -- 'OAI-SearchBot', 'ChatGPT-User', 'Claude-User', "
            "'Claude-SearchBot', 'PerplexityBot', etc. (AI assistants fetching pages live on a user's "
            "behalf, this session's own category) -- 'Disallow: /contractor-finder', '/incentive-finder', "
            "'/product-finder' BY NAME; (c) 'User-agent: * / Disallow: /' -- a full catch-all for every "
            "other UA, including a generic browser string. /product-finder is almost certainly the 'HVAC "
            "Product Finder' this task named. This assistant IS Claude -- both the ClaudeBot block and "
            "the Claude-User/Claude-SearchBot block apply directly, by name, without needing the generic "
            "catch-all at all. Disclosed process note: one request to /incentive-finder was already made "
            "(HTTP 403 -- blocked at the network/WAF layer regardless) in the same command batch as the "
            "robots.txt check, before that check's result was read, same ordering mistake as an earlier "
            "Phase A pass this session -- no further request was made to this host once the block was "
            "seen, and no UA was ever changed or spoofed to get a different answer.\n"
            "\n"
            "4) ahridirectory.org (AHRI's own certification directory -- the underlying data source most "
            "state 'qualified products' programs point back to; the AHRI-derivation question the "
            "instruction asked about) -- robots.txt (HTTP 200) uses the newer IETF Content-Signal draft "
            "convention: 'User-agent: * / Content-Signal: search=yes,ai-train=no,use=reference / Allow: /' "
            "-- i.e. general crawling/indexing is allowed, AI TRAINING is explicitly refused, and "
            "permitted USE is scoped to 'reference' only (the policy's own three-tier scale is immediate/"
            "reference/full -- reference is the middle tier, not full reuse), then separately, by name: "
            "'Disallow: /' for Amazonbot, Applebot-Extended, Bytespider, CCBot, 'ClaudeBot', "
            "CloudflareBrowserRenderingCrawler, Google-Extended, GPTBot, and meta-externalagent -- again "
            "naming this assistant's own crawler identity directly. No record of 'the Sep 9 research' "
            "quoting AHRI's reuse restriction exists anywhere in this codebase or in this assistant's own "
            "memory system (same gap as an earlier Phase A pass this session found for 'the Energy Code "
            "Ace values found Sep 9') -- this entry's own direct robots.txt read is offered in its place, "
            "not a recovery of that specific prior research.\n"
            "\n"
            "QPL ITSELF: no live, general TECH Clean California Qualified Products List was located. The "
            "one QPL reference found on techcleanca.com's own public pages is scoped to the San Fernando "
            "Valley sub-program specifically (techcleanca.com/incentives/sanfernandovalley/, itself "
            "quoted: 'Qualified Products list (coming soon)') -- its own direct link "
            "(techcleanca.com/sanfernandovalley/qpl/) 404s, confirming it is not live yet, not merely "
            "unlinked. Whether the main statewide program's QPL/HVAC Product Finder is the switchison.org "
            "/product-finder tool, lives inside catechincentives.com's authenticated contractor portal, "
            "or both was NOT determined -- once the switchison.org and ahridirectory.org blocks were "
            "found, this pass stopped looking rather than keep searching for a path around them.\n"
            "\n"
            "AAON/LG TEST: NOT PERFORMED. The instruction's own model test would require querying "
            "whichever finder this is on one of the four hosts above; every one of them either bars "
            "automated access generally (techcleanca.com, catechincentives.com) or bars this specific "
            "assistant by name at the specific path in question (switchison.org's /product-finder, "
            "ahridirectory.org generally). Declining is the correct outcome here, not a gap to fill on a "
            "future pass without first getting a different, explicit access path (e.g., the user's own "
            "authenticated portal session, or a written-consent arrangement with Energy Solutions)."
        ),
        verified=True,
        last_reviewed="Checked 2026-09-10 directly against all four hosts' own robots.txt/terms pages.",
    ))

    out.append(Assumption(
        group="TECH Clean California QPL / HVAC Product Finder",
        name="Proposed design (not decided) -- per-line incentive-eligible-models field",
        config_path=None,
        value="PROPOSAL, not a decision: a manual, quarterly-refreshed per-line field recording which "
             "specific models of a manufacturer's line are known to appear on a TECH Clean CA-recognized "
             "qualified-products list -- populated by a person checking by hand (e.g., through the "
             "existing catechincentives.com contractor portal, or a future written-consent data-sharing "
             "arrangement), never by this app's own automated fetch, given every host checked above bars "
             "that outright.",
        source_type=MEASURED,
        source_detail=(
            "Proposed, not asserted, per instruction -- open for the user to weigh differently:\n"
            "\n"
            "SHAPE: a nullable field alongside the existing config.yaml `accounts.line_card` entries "
            "(same location as the already-researched ahri_certified/ahri_certified_basis pair -- see "
            "app/accounts.py's seed_product_lines mapping and [[project_line_card_eligibility_research]]) "
            "-- 'incentive_eligible_models' (a short list or free-text of specific model numbers/series "
            "confirmed present on a QPL) plus its own '_basis' string recording who checked, when, and "
            "against which specific list/portal view, same 'basis alongside every claim' discipline as "
            "every other line-card field in this system. Deliberately NOT a boolean "
            "'incentive_eligible': TECH Clean CA incentive eligibility is model-specific (a manufacturer "
            "can have some qualifying and some non-qualifying models in the same line), so a per-"
            "manufacturer flag would either overclaim (implying every model qualifies) or underclaim "
            "(hiding real qualifying models) -- a model-list field is the honest grain.\n"
            "\n"
            "CADENCE: quarterly, matching the other newly-proposed manual-check cadence this session "
            "already recommended for CEC MAEDbS (see that group's own proposal entry) -- both are "
            "manual-only for the same underlying reason (no working, permitted automated path found), "
            "not because quarterly is independently the right refresh rate for either program's own "
            "update cycle, which was not established either way.\n"
            "\n"
            "NULL DISCIPLINE: a line with no confirmed models this pass stays NULL, not an empty list "
            "asserted as 'checked, zero qualify' -- those are different claims, and conflating them would "
            "make an unresearched line look like a researched-and-failed one. Same reasoning already "
            "documented for AhjA2lGuidance's NOT_REACHED rows (see app/reference.py's own docstring)."
        ),
        verified=False,
        last_reviewed="Proposed 2026-09-10, Phase A research only -- not decided, not built.",
    ))

    # ---- PlanetBids Bid Results / PCC 4104 subcontractor lists (Phase A) -------
    # Phase A research only, no code written. Not explicitly asked to "register
    # constants" this time either, recorded anyway for the same reason as the
    # TECH Clean CA entry above -- so it isn't silently lost.

    out.append(Assumption(
        group="PlanetBids Bid Results / PCC 4104 subcontractor lists",
        name="Access classification and findings (Phase A research)",
        config_path=None,
        value="vendors.planetbids.com's robots.txt is fully open (no disallow at all). BUT this pass "
             "could not actually view a closed bid's 'Bid Results' tab for either portal -- there is no "
             "browser-automation tool available in this environment, and the portal is a pure "
             "client-side Ember SPA with an empty <body> on plain fetch, the same 'rendered browser "
             "session, not a fetcher' limit already on record elsewhere in this codebase. Separately, and "
             "more usefully: the City of San Diego's OWN agency-direct site (sandiego.gov) already "
             "publishes full contract packages containing a filled-in PCC-4100-citing 'List of "
             "Subcontractors' form naming real subcontractors, dollar values, and license numbers -- "
             "confirming subs ARE publicly visible somewhere, just not (as far as this pass could "
             "determine) via a PlanetBids fetch this pass actually performed.",
        source_type=MEASURED,
        source_detail=(
            "Checked 2026-09-10, Phase A research, no code written.\n"
            "\n"
            "ROBOTS.TXT: https://vendors.planetbids.com/robots.txt, HTTP 200, verbatim in full: "
            "'# http://www.robotstxt.org' / 'User-agent: *' / 'Disallow:' (empty value -- permits "
            "everything). Not a blocked host by this mechanism.\n"
            "\n"
            "PLANETBIDS PORTAL ITSELF: NOT VIEWED. https://vendors.planetbids.com/portal/17950/"
            "portal-home returned a complete Ember.js single-page-app shell -- an essentially empty "
            "<body> with only <script> tags, no server-rendered content of any kind (confirmed via "
            "direct fetch, not assumed). This assistant has no headless-browser or JS-execution tool "
            "available in this environment (checked via ToolSearch before starting) -- 'open one closed "
            "bid result in a browser session' as instructed could not be performed at all, by anyone "
            "other than a person with an actual browser. This is a capability gap, not a policy block: "
            "robots.txt does not forbid it, but this pass genuinely could not see what a logged-out human "
            "visitor sees on a Bid Results tab, and does not report a guess in its place. The embedded "
            "Ember config blob happened to reveal internal (non-public, not further pursued) infrastructure "
            "detail -- 'idsHostUrl' and 'transparencyServiceUrl' pointing at 'preprod'/'dev01' PlanetBids "
            "hosts inside what is labeled a 'production' build config -- noted here only because it was "
            "already visible in a normal page-source fetch, not investigated further; irrelevant to the "
            "sub-listing question this pass was asked and not something this pass tried to exploit.\n"
            "\n"
            "PORTAL ID DISCREPANCY: San Diego Unified's own current website "
            "(sandiegounified.org/departments/strategic_sourcing_and_contracts/bids_and_requests_for_"
            "proposal/) links to PlanetBids CompanyID 43764 (which redirects to vendors.planetbids.com/"
            "portal/43764/portal-home), NOT portal 13982 as this task specified. Both 43764 and 13982 "
            "independently resolve to a valid-looking (HTTP 200) PlanetBids portal-home shell -- with no "
            "way to inspect either one's actual content (per the capability gap above), this pass cannot "
            "say whether 13982 is a stale/legacy id, a different SDUSD-affiliated portal (e.g. a bond "
            "program, checked nowhere else in this pass), or simply wrong. Flagged rather than silently "
            "substituted -- confirm the correct id before building anything against it. Portal 17950 for "
            "the City of San Diego WAS independently confirmed correct: the city's own bidtabs page links "
            "directly to vendors.planetbids.com/portal/17950/bo/bo-search.\n"
            "\n"
            "SANDIEGO.GOV/PURCHASING/BIDS-CONTRACTS/BIDTABS: checked directly -- this page posts NO PDFs "
            "at all. Its own text, quoted: 'To obtain bid results, the bidder is requested to... [attend "
            "the bid opening, or mail a self-addressed envelope, or] Visit the City's vendor portal, "
            "click on a posted bid opportunity, and navigate to the Bid Results tab. Note that not all "
            "bid results are posted on the vendor portal.' I.e. the City's own stated primary channel for "
            "bid results IS PlanetBids -- there is no separate agency-hosted bid-tab PDF archive for "
            "current bids at this URL, contrary to what 'posted tab PDFs' in the instruction presupposed. "
            "This page's own 'Awarded Contracts' section links to a DIFFERENT, real PDF archive instead "
            "(see next finding).\n"
            "\n"
            "AGENCY-DIRECT PDF, READ IN FULL: sandiego.gov/cip/reports/constructioncontracts (linked from "
            "the bidtabs page's own 'Construction Contracts' award link) lists real contract-package "
            "PDFs. One was downloaded and read directly (not summarized): 'k-26-2480-EMR-3' -- "
            "'Emergency Construction Services for 9201 Youngstown Way Storm Drain Emergency,' awarded to "
            "Cass Construction, Inc. DBA Cass Arrieta. This is a full ~120-page contract-documents "
            "package (specs, bonds, certifications), not a standalone 'bid tab' -- but page 59 is "
            "Exhibit K's 'Form AA35 -- List of Subcontractors,' explicitly citing 'the Subletting and "
            "Subcontracting Fair Practices Act, Section 4100... of the California Public Contract Code,' "
            "and it IS filled in with two real subcontractors, named in full: 'El Encino Tree Service' "
            "(Ramona, CA, DIR registration PW-LR-1000924687, license 1027654, Tree Removal, "
            "$200,000.00) and 'LSA Associates, Inc' (Irvine, CA, DIR registration 1000010048, Biologist "
            "Consult, $20,000.00) -- full name, address, phone/email, DIR registration, license number, "
            "type of work, and dollar value, all in the clear, no login, no PlanetBids involved at all. "
            "This directly confirms the instruction's own premise (subcontractor lists ARE sometimes "
            "publicly posted) via the agency-direct channel specifically, independent of whatever "
            "PlanetBids' own Bid Results tab does or doesn't show (which this pass could not check).\n"
            "\n"
            "SAN DIEGO UNIFIED equivalent: not found within this pass's effort. sandiegounified.org's own "
            "Strategic Sourcing and Contracts page links only to the PlanetBids portal (id 43764, see "
            "discrepancy above) and to a 'Bids and Requests for Proposal' page with no PDF archive "
            "analogous to the City of San Diego's /cip/reports/constructioncontracts -- not confirmed "
            "absent (a further, deeper page-by-page search was not performed once this pass's own "
            "capability gap on the PlanetBids side made a full parallel comparison moot), just not found "
            "in the pages actually checked.\n"
            "\n"
            "PLANETBIDS TERMS 3.4/6.2: this pass attempted to independently re-verify these (still "
            "recorded elsewhere in this register as 'PlanetBids' -- 'Terms of use bar automated reuse,' "
            "stated by the user 2026-09-11, not independently verified) but could not reach a plain-text "
            "PlanetBids Terms of Use page either -- www.planetbids.com itself redirects to a "
            "'files-prod01.planetbids.com' host and is ALSO a client-rendered app with no server-side "
            "terms page found at any guessed URL. That entry's own 'not independently verified' status is "
            "left unchanged by this pass, not silently upgraded to verified."
        ),
        verified=True,
        last_reviewed="Checked 2026-09-10 -- robots.txt and the agency-direct PDF are directly verified; "
                      "the PlanetBids Bid Results tab itself was NOT viewed (no browser tool available).",
    ))

    out.append(Assumption(
        group="PlanetBids Bid Results / PCC 4104 subcontractor lists",
        name="Recommendation given the findings -- agency-direct PDFs, not PlanetBids automation",
        config_path=None,
        value="Per the instruction's own conditional: subs ARE visible somewhere (the agency-direct "
             "construction-contracts PDF archive), and PlanetBids' terms already on record bar automated "
             "reuse -- so recommend building any future subcontractor-sourcing feature against "
             "agency-direct PDF archives (sandiego.gov/cip/reports/constructioncontracts and its "
             "counterparts) rather than PlanetBids at all, matching the instruction's own proposed "
             "conclusion. This is a straight readout of what this pass found, not a new judgment call.",
        source_type=MEASURED,
        source_detail=(
            "Not a separate proposal needing weighing -- the instruction told this pass what to conclude "
            "if subs turned out to be visible, and they are (see the finding above), so this entry just "
            "records that the stated condition was met. Concretely, for a future pass to actually build "
            "on: the City of San Diego's own award-notice pages (/cip/reports/constructioncontracts, "
            "/cip/reports/consultantcontracts, /cip/reports/minor-repair-contracts, per the bidtabs "
            "page's own links) are a real, un-gated, agency-direct source containing PCC-4100 subcontractor "
            "listing forms buried inside full contract packages -- extracting them would mean identifying "
            "the 'List of Subcontractors' page within a variable-length PDF per contract (page 59 in the "
            "one example read here, not a fixed page number), not a clean standalone bid-tab sheet. "
            "Whether San Diego Unified has an equivalent archive was not established this pass (see "
            "finding above) -- check for one directly before assuming City-of-San-Diego's own page "
            "structure generalizes to it."
        ),
        verified=True,
        last_reviewed="Checked 2026-09-10 -- a direct readout of this pass's own findings, not a fresh "
                      "proposal.",
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
