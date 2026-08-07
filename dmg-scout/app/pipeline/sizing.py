"""Sizing engine: convert whatever size input a signal carries into estimated tons of cooling.

Physics: 1 W of IT load becomes 1 W of heat. W x 3.412 = BTU/hr; / 12,000 = tons.
So 1 MW IT = 284.3 tons of pure IT heat. Installed capacity (non-IT heat, N+1
redundancy) runs 300-400 tons per MW; midpoint default 325.

The low/high band is derived from the estimate basis, not a fixed ratio: a
stated IT MW from a filing gets a narrow band, MW inferred from generator
horsepower gets a wider one, square footage gets the widest and is flagged
low-confidence everywhere it appears.

Every estimate carries `estimate_basis` naming which input drove it, so a number
shown on the dashboard can be defended.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass

from app.config import Config
from app.models import Category, FacilityType

log = logging.getLogger(__name__)


@dataclass
class TonsEstimate:
    low: float | None
    high: float | None
    basis: str | None
    basis_key: str | None = None  # stated_it | permitted_capacity | gensets_critical | stated_total | gensets | sqft | industrial_sqft
    mw_it: float | None = None    # back-computed IT MW when derivable
    low_confidence: bool = False
    rejected_inputs: list[str] | None = None  # inputs discarded as implausible

    @property
    def midpoint(self) -> float | None:
        """Geometric mean, not arithmetic — these bands span orders of magnitude.

        `size_factor` is log10-scaled, so the geometric mean is the matching centre.
        With the arithmetic mean, an unknown-type band of 222-11,101 tons centred on
        5,661 and Elsinore Heights jumped from 0.396 to 0.673 and took the top of the
        industrial board purely because its type was unstated. Not knowing must never
        outrank knowing. Geometrically that band centres on 1,570.

        For narrow bands the two agree to within a percent, so nothing else moves.
        """
        if self.low is None or self.high is None:
            return None
        if self.low <= 0:
            return (self.low + self.high) / 2
        return math.sqrt(self.low * self.high)


def hp_to_kw(hp: float, cfg: Config) -> float:
    return hp * cfg.get("sizing.hp_to_kw", 0.7457)


def genset_mw_to_it_mw(genset_mw: float, cfg: Config) -> float:
    # Gensets back cooling and house load too, not just IT.
    return genset_mw / cfg.get("sizing.genset_mw_to_it_mw_divisor", 1.4)


def critical_genset_mw_to_it_mw(critical_mw: float, cfg: Config) -> float:
    """Critical-dedicated NAMEPLATE capacity, as an upper bound — not sized from
    directly. See the "permitted capacity" note on estimate_tons for why: the
    critical count is a fleet built for N+1/N+2 redundancy plus mechanical load,
    so its raw sum overstates IT load, sometimes by a lot (Vernon: 38 x 3 MW =
    114 MW nameplate against what turned out to be ~76 MW IT once sized
    correctly — 1.5x over). Divisor default 1.0 means "trust it as a ceiling,
    not as the estimate."
    """
    return critical_mw / cfg.get("sizing.critical_genset_mw_to_it_mw_divisor", 1.0)


def permitted_capacity_to_it_mw(permitted_mw: float, cfg: Config) -> float:
    """A stated total/site MW figure, converted to IT load by a PUE-based ratio
    (IT = total facility power / PUE; 1.3 is a reasonable default for a modern
    facility, tunable because real PUE varies by climate and design).

    THE LESSON THIS FUNCTION EXISTS TO RECORD: a number in a regulatory filing
    can be a regulatory artifact rather than an engineering fact. Vernon's 99 MW
    is not a measured or designed load — it is almost certainly the filer
    staying just under the California Energy Commission's Small Power Plant
    Exemption ceiling (SPPE covers 50-100 MW; at or above 100 MW the project
    needs full CEC certification, which is far slower and more expensive). A
    facility engineered to stay under a 100 MW regulatory line will state a
    number shaped by that line, not by its actual power draw. The generator
    fleet nameplate told the truer story here (116 MW across 40 units, average
    2.6 MW/unit against a 99 MW permit — visible sparing), which is why this
    basis takes the generator split as corroboration and a ceiling (see
    critical_genset_mw_to_it_mw) rather than trusting either number in
    isolation. Whenever a filing states a capacity figure that a known
    regulatory threshold could plausibly be shaping, treat it as a permit
    artifact first and an engineering fact only once something else confirms it
    — the same caution belongs anywhere else a bright-line threshold exists in
    a permitting regime this system reads (a size threshold that changes which
    review track a project takes is the general shape to watch for).
    """
    return permitted_mw / cfg.get("sizing.permitted_capacity_to_it_mw_divisor", 1.3)


def _band(cfg: Config, mw_it: float, basis_key: str, basis: str,
          low_confidence: bool = False,
          rejected: list[str] | None = None) -> TonsEstimate:
    mid = mw_it * cfg.get("sizing.tons_per_mw_installed_default", 325)
    half = cfg.get("sizing.band_by_basis", {}).get(basis_key, 0.25)
    return TonsEstimate(
        low=mid * (1 - half), high=mid * (1 + half),
        basis=basis, basis_key=basis_key, mw_it=mw_it, low_confidence=low_confidence,
        rejected_inputs=rejected or None,
    )


def implausible_watts_per_sqft(cfg: Config, mw: float, building_sqft: float | None,
                               category: Category) -> float | None:
    """W/sqft implied by a stated MW figure, if it exceeds what the building could
    possibly draw — otherwise None.

    This exists because a "stated" number is trusted more than a derived one, so a
    misread MW sails past every low-confidence flag. Amperesand's filing stated
    500 MW for a 73,000 sqft solid-state transformer factory: that is 6,849 W/sqft,
    which is the product's rating or annual output, not the building's load. It
    became 357 MW IT and 136,964 tons — the largest number on the board, and pure
    fiction. A ceiling turns that from a confident wrong answer into a flagged one.
    """
    if not mw or not building_sqft or building_sqft <= 0:
        return None
    ceilings = cfg.get("sizing.max_watts_per_sqft_by_category", {}) or {}
    ceiling = ceilings.get(category.value, ceilings.get("industrial", 60))
    implied = mw * 1_000_000.0 / building_sqft
    return implied if implied > ceiling else None


def estimate_tons(
    cfg: Config,
    mw_it: float | None = None,
    mw_total: float | None = None,
    generator_count: int | None = None,
    generator_hp_each: float | None = None,
    generator_kw_each: float | None = None,
    generator_critical_count: int | None = None,
    generator_critical_mw_each: float | None = None,
    generator_house_count: int | None = None,
    generator_house_mw_each: float | None = None,
    building_sqft: float | None = None,
    category: Category = Category.data_center,
    facility_type: FacilityType = FacilityType.unknown,
) -> TonsEstimate:
    """Best available input wins, in order of reliability:
    stated IT MW > permitted/site capacity (corroborated by a generator split,
    when one is stated) > critical-dedicated generator fleet alone > stated
    total MW alone > undifferentiated generator fleet > square footage.

    A stated total MW figure alongside ANY generator data (critical/house split
    or a flat count) is treated as a permitted/site capacity, not a blended
    utility total — see permitted_capacity_to_it_mw for why, and its docstring
    for the general lesson about regulatory-filing numbers. Sized with a
    PUE-based divisor, and capped at the critical-dedicated fleet's nameplate
    when that split is stated, since backup capacity is a hard physical ceiling
    on what the site could ever draw. This ranks above the critical-fleet-alone
    basis (below) on purpose: the earlier version of this function sized
    directly from critical capacity with no ceiling and no correction for
    N+1/N+2 sparing, and it double-counted — see Vernon in git history.

    Where a generator split is stated but no total/site capacity is, there is
    nothing to corroborate against, so the critical fleet becomes the estimate
    directly rather than just a cap on one.

    A stated MW that implies an impossible watts-per-square-foot for the building
    type is discarded rather than trusted, and the discard is recorded in the basis
    so the fallback is never mistaken for a first-choice estimate.

    Industrial buildings never use the electrical path: their cooling load is
    envelope-and-ventilation driven, so square footage IS the right basis and the
    data-center W/sqft constant would overstate them by more than an order of
    magnitude (1,000,000 sqft of warehouse is ~1,000 tons, not ~24,000).
    """
    rejected: list[str] = []

    # Industrial AND esco take the envelope path, never the electrical one.
    #
    # For esco the reason is sharper than for industrial. An energy services
    # performance contract is a RETROFIT of buildings that already exist: there is
    # no stated IT load, no generator fleet, and any megawatt figure in the
    # document is far more likely to be the agency's annual electricity purchase
    # than a cooling load. Falling through to the data-center branch would take
    # such a number and multiply it into thousands of tons of chiller — the same
    # order-of-magnitude error the industrial branch exists to prevent, on a
    # building that may not need a single new machine.
    if category in (Category.industrial, Category.esco):
        if building_sqft:
            return _industrial_from_sqft(cfg, building_sqft, facility_type)
        # No area stated: neither has an electrical shortcut worth trusting, so
        # report unknown rather than borrow data-center physics.
        return TonsEstimate(None, None, None)

    if mw_it:
        implied = implausible_watts_per_sqft(cfg, mw_it, building_sqft, category)
        if implied is None:
            return _band(cfg, mw_it, "stated_it", f"stated IT load {mw_it:g} MW")
        rejected.append(f"stated mw_it {mw_it:g} MW implies {implied:,.0f} W/sqft "
                        f"on {building_sqft:,.0f} sqft — implausible, discarded")

    has_generator_data = bool(generator_critical_count or generator_count)
    if mw_total and has_generator_data:
        implied = implausible_watts_per_sqft(cfg, mw_total, building_sqft, category)
        if implied is None:
            it = permitted_capacity_to_it_mw(mw_total, cfg)
            corroboration = ""
            if generator_critical_count and generator_critical_mw_each:
                critical_mw = generator_critical_count * generator_critical_mw_each
                cap = critical_genset_mw_to_it_mw(critical_mw, cfg)
                if it > cap:
                    # The PUE-derived figure exceeds what the critical fleet could
                    # even back — physically impossible, so the fleet wins.
                    it = cap
                    corroboration = (f"; capped at {critical_mw:.0f} MW critical-dedicated "
                                     f"backup nameplate (the PUE-derived figure exceeded it)")
                else:
                    corroboration = (f"; consistent with {critical_mw:.0f} MW "
                                     f"critical-dedicated backup capacity (upper bound, not "
                                     f"exceeded)")
            return _band(
                cfg, it, "permitted_capacity",
                f"permitted/site capacity {mw_total:g} MW (may be a regulatory ceiling, "
                f"not necessarily the engineering load) / "
                f"{cfg.get('sizing.permitted_capacity_to_it_mw_divisor', 1.3):.2g} PUE -> "
                f"~{it:.0f} MW IT{corroboration}",
                rejected=rejected or None,
            )
        rejected.append(f"stated mw_total {mw_total:g} MW implies {implied:,.0f} W/sqft "
                        f"on {building_sqft:,.0f} sqft — implausible, discarded")

    if generator_critical_count and generator_critical_mw_each:
        critical_mw = generator_critical_count * generator_critical_mw_each
        implied = implausible_watts_per_sqft(cfg, critical_mw, building_sqft, category)
        if implied is None:
            it = critical_genset_mw_to_it_mw(critical_mw, cfg)
            house_note = (
                f" (excludes {generator_house_count} house genset(s) x "
                f"{generator_house_mw_each:g} MW — not sized to IT load)"
                if generator_house_count and generator_house_mw_each else ""
            )
            return _band(
                cfg, it, "gensets_critical",
                f"{generator_critical_count} critical-dedicated gensets x "
                f"{generator_critical_mw_each:g} MW = {critical_mw:.1f} MW -> "
                f"~{it:.0f} MW IT{house_note} (no permitted/site capacity stated to size "
                f"from instead — see permitted_capacity_to_it_mw)",
                rejected=rejected or None,
            )
        rejected.append(f"critical genset capacity {critical_mw:g} MW implies "
                        f"{implied:,.0f} W/sqft on {building_sqft:,.0f} sqft — "
                        f"implausible, discarded")

    if mw_total:
        implied = implausible_watts_per_sqft(cfg, mw_total, building_sqft, category)
        if implied is None:
            it = genset_mw_to_it_mw(mw_total, cfg)
            return _band(cfg, it, "stated_total",
                         f"stated total/utility load {mw_total:g} MW -> ~{it:.0f} MW IT",
                         rejected=rejected or None)
        rejected.append(f"stated mw_total {mw_total:g} MW implies {implied:,.0f} W/sqft "
                        f"on {building_sqft:,.0f} sqft — implausible, discarded")

    if generator_count and (generator_hp_each or generator_kw_each):
        kw_each = generator_kw_each if generator_kw_each else hp_to_kw(generator_hp_each, cfg)
        genset_mw = generator_count * kw_each / 1000.0
        it = genset_mw_to_it_mw(genset_mw, cfg)
        return _band(
            cfg, it, "gensets",
            f"{generator_count} gensets x {kw_each:.0f} kW = {genset_mw:.1f} MW standby -> ~{it:.0f} MW IT",
            rejected=rejected or None,
        )

    if building_sqft:
        w_per_sqft = cfg.get("sizing.watts_per_sqft", 150)
        util = cfg.get("sizing.sqft_utilization", 0.5)
        it = building_sqft * util * w_per_sqft / 1_000_000.0
        prefix = "; ".join(rejected) + " -> fell back to " if rejected else ""
        return _band(
            cfg, it, "sqft",
            f"{prefix}{building_sqft:,.0f} sqft @ {w_per_sqft} W/sqft "
            f"({util:.0%} utilization) -> ~{it:.0f} MW IT (LOW CONFIDENCE)",
            low_confidence=True, rejected=rejected or None,
        )

    # No `if rejected:` fallback here on purpose: a rejection requires a known
    # building_sqft, and a known building_sqft always lands in the branch above, so
    # any such branch would be unreachable.
    return TonsEstimate(None, None, None)


FALLBACK_SQFT_PER_TON = {"low": 50, "high": 2500}


def _industrial_from_sqft(cfg: Config, building_sqft: float,
                          facility_type: FacilityType) -> TonsEstimate:
    """Industrial cooling load from floor area, banded by what the building does.

    Rule-of-thumb sqft-per-ton, not IT watts, and the type is the whole story: a
    cleanroom is 50-150 sqft/ton and a fulfillment centre 1,000-2,500, so the same
    1,000,000 sqft is 6,700-20,000 tons or 400-1,000 tons depending only on which
    it is. Sizing every industrial building on one band made a Crocs distribution
    centre look like a factory.

    An unstated type takes the full span across all of them rather than a
    convenient middle — see the note in config.yaml. Every result is flagged
    low-confidence: this ranks jobs, it does not quote them.
    """
    # A data_center facility_type under the industrial category is a contradiction:
    # triage already ruled this building is not a computing facility, and the
    # industrial table has no data_center row by design. Name it rather than let a
    # dict miss quietly decide.
    if facility_type is FacilityType.data_center:
        log.info("industrial project reports facility_type=data_center; treating as "
                 "unknown for sizing (category and facility type disagree)")
        facility_type = FacilityType.unknown

    # An unstated type produces NO tonnage, not a 50x band.
    #
    # The full span across all types is 50-2,500 sqft/ton, so an 8,100,000 sqft
    # building came out as "3,240 to 162,000 tons". That is not an estimate; it is
    # the arithmetic restating that we do not know what the building is, wearing
    # the costume of a number. Anyone reading the board sees two figures and a
    # dash and assumes someone measured something. Better to carry 40 rows that
    # can be quoted and flag the rest than 296 nobody can trust.
    #
    # The row keeps its square footage and says exactly what is missing, so it is
    # actionable: one filing stating the use turns it into a real estimate.
    if facility_type is FacilityType.unknown:
        return TonsEstimate(
            low=None, high=None,
            basis=f"UNKNOWN TYPE — {building_sqft:,.0f} sqft, building use not stated. "
                  f"No tonnage estimated: sqft-per-ton spans 50x across facility "
                  f"types ({FALLBACK_SQFT_PER_TON['low']:,.0f}-"
                  f"{FALLBACK_SQFT_PER_TON['high']:,.0f} sqft/ton), so any band would "
                  f"be a restatement of the uncertainty rather than an estimate.",
            basis_key="industrial_unknown_type", mw_it=None, low_confidence=True,
        )

    table = cfg.get("sizing.industrial_sqft_per_ton_by_type", {}) or {}
    band = table.get(facility_type.value) or FALLBACK_SQFT_PER_TON
    low_sqft_per_ton = band.get("low", FALLBACK_SQFT_PER_TON["low"])
    high_sqft_per_ton = band.get("high", FALLBACK_SQFT_PER_TON["high"])
    # Fewer sqft per ton = denser load = MORE tons, so low/high invert here.
    high = building_sqft / low_sqft_per_ton
    low = building_sqft / high_sqft_per_ton
    return TonsEstimate(
        low=low, high=high,
        basis=f"{building_sqft:,.0f} sqft {facility_type.value} @ "
              f"{low_sqft_per_ton:,.0f}-{high_sqft_per_ton:,.0f} sqft/ton -> "
              f"{low:,.0f}-{high:,.0f} tons (LOW CONFIDENCE, rule of thumb)",
        basis_key="industrial_sqft", mw_it=None, low_confidence=True,
    )


@dataclass
class EquipmentValue:
    """What the cooling on this project is worth, and what equipment that assumes."""
    low: float | None
    high: float | None
    basis: str | None


def estimate_equipment_value(cfg: Config, tons_low: float | None, tons_high: float | None,
                             facility_type: FacilityType) -> EquipmentValue:
    """Rough installed equipment value, from tonnage and what the building buys.

    Ranking on tonnage alone over-values large low-intensity boxes: a 5,000 ton
    fulfillment centre on packaged rooftops is a smaller opportunity than a 600
    ton cleanroom on custom air handlers. This is the correction, and it changes
    ranking rather than tonnage — the tons stay exactly as sized.

    Returns nothing when the tonnage is unknown or the facility type is unstated:
    without a type there is no equipment category, and inventing one here would
    reintroduce the guess that removing the 50x band just took out.

    The dollar figures are config. Two points on that table are real line-card
    figures from DMG's VP of sales — a VU Flow CO2 chiller at ~$8,500/ton and
    commodity LG chillers at ~$1,500/ton — and they anchor both the magnitude and
    the 5.7x spread. The rows between them are interpolated estimates, and each
    row carries a `source` field saying which it is. The basis string repeats that
    on every project so nobody quotes an interpolated number as a real one.
    """
    if not cfg.get("equipment_value.enabled", True):
        return EquipmentValue(None, None, None)
    if tons_low is None or tons_high is None:
        return EquipmentValue(None, None, None)

    table = cfg.get("equipment_value.value_per_ton_by_facility_type", {}) or {}
    band = table.get(facility_type.value)
    if not band:
        return EquipmentValue(
            None, None,
            f"no equipment value: {facility_type.value} has no value-per-ton entry")

    low = tons_low * band["low"]
    high = tons_high * band["high"]
    source = band.get("source", "unverified")
    # VERIFIED vs estimated travels with the number, on every row. A rate anchored
    # to a real line-card figure and one interpolated between two of them are not
    # the same claim, and the board must not present them as one.
    tag = "line-card VERIFIED" if "VERIFIED" in source else "ESTIMATED rate"
    return EquipmentValue(
        low=low, high=high,
        basis=f"{tons_low:,.0f}-{tons_high:,.0f} tons of {band.get('equipment', facility_type.value)} "
              f"@ ${band['low']:,.0f}-{band['high']:,.0f}/ton -> ${low:,.0f}-${high:,.0f} "
              f"[{tag}: {source}]",
    )
