"""Sizing engine: convert whatever size input a signal carries into estimated tons of cooling.

Physics: 1 W of IT load becomes 1 W of heat. W x 3.412 = BTU/hr; / 12,000 = tons.
So 1 MW IT = 284.3 tons of pure IT heat. Installed capacity (non-IT heat, N+1
redundancy) runs 300-400 tons per MW.

Every estimate carries `estimate_basis` naming which input drove it, so a number
shown on the dashboard can be defended.
"""
from __future__ import annotations

from dataclasses import dataclass

from app.config import Config


@dataclass
class TonsEstimate:
    low: float | None
    high: float | None
    basis: str | None
    mw_it: float | None = None  # back-computed IT MW when derivable
    low_confidence: bool = False

    @property
    def midpoint(self) -> float | None:
        if self.low is None or self.high is None:
            return None
        return (self.low + self.high) / 2


def hp_to_kw(hp: float, cfg: Config) -> float:
    return hp * cfg.get("sizing.hp_to_kw", 0.7457)


def genset_mw_to_it_mw(genset_mw: float, cfg: Config) -> float:
    # Gensets back cooling and house load too, not just IT.
    return genset_mw / cfg.get("sizing.genset_mw_to_it_mw_divisor", 1.4)


def estimate_tons(
    cfg: Config,
    mw_it: float | None = None,
    mw_total: float | None = None,
    generator_count: int | None = None,
    generator_hp_each: float | None = None,
    generator_kw_each: float | None = None,
    building_sqft: float | None = None,
) -> TonsEstimate:
    """Best available input wins, in order of reliability:
    stated IT MW > stated total MW > generator fleet > square footage.
    """
    t_low = cfg.get("sizing.tons_per_mw_installed_low", 300)
    t_high = cfg.get("sizing.tons_per_mw_installed_high", 400)

    if mw_it:
        return TonsEstimate(mw_it * t_low, mw_it * t_high, f"stated IT load {mw_it:g} MW", mw_it=mw_it)

    if mw_total:
        it = genset_mw_to_it_mw(mw_total, cfg)
        return TonsEstimate(
            it * t_low, it * t_high,
            f"stated total/utility load {mw_total:g} MW -> ~{it:.0f} MW IT", mw_it=it,
        )

    if generator_count and (generator_hp_each or generator_kw_each):
        kw_each = generator_kw_each if generator_kw_each else hp_to_kw(generator_hp_each, cfg)
        genset_mw = generator_count * kw_each / 1000.0
        it = genset_mw_to_it_mw(genset_mw, cfg)
        return TonsEstimate(
            it * t_low, it * t_high,
            f"{generator_count} gensets x {kw_each:.0f} kW = {genset_mw:.1f} MW standby -> ~{it:.0f} MW IT",
            mw_it=it,
        )

    if building_sqft:
        w_per_sqft = cfg.get("sizing.watts_per_sqft", 150)
        util = cfg.get("sizing.sqft_utilization", 0.5)
        it = building_sqft * util * w_per_sqft / 1_000_000.0
        return TonsEstimate(
            it * t_low, it * t_high,
            f"{building_sqft:,.0f} sqft @ {w_per_sqft} W/sqft ({util:.0%} utilization) -> ~{it:.0f} MW IT (LOW CONFIDENCE)",
            mw_it=it,
            low_confidence=True,
        )

    return TonsEstimate(None, None, None)
