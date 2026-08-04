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

from dataclasses import dataclass

from app.config import Config


@dataclass
class TonsEstimate:
    low: float | None
    high: float | None
    basis: str | None
    basis_key: str | None = None  # stated_it | stated_total | gensets | sqft
    mw_it: float | None = None    # back-computed IT MW when derivable
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


def _band(cfg: Config, mw_it: float, basis_key: str, basis: str,
          low_confidence: bool = False) -> TonsEstimate:
    mid = mw_it * cfg.get("sizing.tons_per_mw_installed_default", 325)
    half = cfg.get("sizing.band_by_basis", {}).get(basis_key, 0.25)
    return TonsEstimate(
        low=mid * (1 - half), high=mid * (1 + half),
        basis=basis, basis_key=basis_key, mw_it=mw_it, low_confidence=low_confidence,
    )


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
    if mw_it:
        return _band(cfg, mw_it, "stated_it", f"stated IT load {mw_it:g} MW")

    if mw_total:
        it = genset_mw_to_it_mw(mw_total, cfg)
        return _band(cfg, it, "stated_total",
                     f"stated total/utility load {mw_total:g} MW -> ~{it:.0f} MW IT")

    if generator_count and (generator_hp_each or generator_kw_each):
        kw_each = generator_kw_each if generator_kw_each else hp_to_kw(generator_hp_each, cfg)
        genset_mw = generator_count * kw_each / 1000.0
        it = genset_mw_to_it_mw(genset_mw, cfg)
        return _band(
            cfg, it, "gensets",
            f"{generator_count} gensets x {kw_each:.0f} kW = {genset_mw:.1f} MW standby -> ~{it:.0f} MW IT",
        )

    if building_sqft:
        w_per_sqft = cfg.get("sizing.watts_per_sqft", 150)
        util = cfg.get("sizing.sqft_utilization", 0.5)
        it = building_sqft * util * w_per_sqft / 1_000_000.0
        return _band(
            cfg, it, "sqft",
            f"{building_sqft:,.0f} sqft @ {w_per_sqft} W/sqft ({util:.0%} utilization) -> ~{it:.0f} MW IT (LOW CONFIDENCE)",
            low_confidence=True,
        )

    return TonsEstimate(None, None, None)
