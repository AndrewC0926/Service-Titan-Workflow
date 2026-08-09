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

    @property
    def slug(self) -> str:
        return slugify(self.name)


def _fmt_table(d: dict, fmt: str = "{:.2f}") -> str:
    if not d:
        return "—"
    return ", ".join(f"{k}={fmt.format(v)}" for k, v in d.items())


def load_assumptions(cfg: Config) -> list[Assumption]:
    """Everything below is read from cfg at call time — never hand-copied —
    so the VALUE column can't drift from what's actually running even if
    this function's prose goes stale."""
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
        source_detail="\"No new signal in 180 days ~ halves the score.\" A round-number judgment call — not "
                      "measured against how often a quiet-for-180-days project has actually gone stale versus "
                      "simply being between filings.",
    ))

    out.append(Assumption(
        group="Score weights", name="Size factor formula",
        config_path=None,
        value="max(0.25, log10(tons_midpoint) − 2.0); unknown size → 0.5",
        source_type=PLACEHOLDER,
        source_detail="Hardcoded in app/pipeline/scoring.py — not even config-tunable without a code change, "
                      "unlike every other row on this page. Log-scaled so a 10x bigger project doesn't drown "
                      "out winnability; the specific −2.0 offset and 0.25 floor are not measured.",
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
                      "and actual callability.",
    ))

    dtb = cfg.get("scoring.days_to_bid_by_stage", {})
    out.append(Assumption(
        group="Score weights", name="Days to estimated bid, by stage",
        config_path="scoring.days_to_bid_by_stage",
        value=_fmt_table(dtb, fmt="{:.0f}"),
        source_type=PLACEHOLDER,
        source_detail="\"Midpoints of lead-time windows\" per config.yaml — a judgment call about typical "
                      "stage duration, not measured from this territory's own closed deals.",
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
    out.append(Assumption(
        group="Equipment sizing", name="Industrial sqft/ton, by facility type (new-construction sizing)",
        config_path="sizing.industrial_sqft_per_ton_by_type",
        value=(f"{len(isqft)} types, "
              f"{min(b['low'] for b in isqft.values())}–{max(b['high'] for b in isqft.values())} sqft/ton"
              if isqft else "—"),
        source_type=STATED,
        source_detail="\"These are Andrew's numbers, not defaults — the spread across types is 50x, so type "
                      "matters more than area\" — verbatim from config.yaml. Stated by name, no date recorded.",
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

    return out


def assumptions_by_group(cfg: Config) -> dict[str, list[Assumption]]:
    grouped: dict[str, list[Assumption]] = {}
    for a in load_assumptions(cfg):
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
