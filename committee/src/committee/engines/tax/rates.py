"""Flat-rate tax model built from the dated tax config.

Short-term gains are taxed at the assumed ordinary rate, long-term gains at
the assumed LTCG rate; the state rate is added to both, and NIIT is added to
both when ``apply_niit`` is true. Losses produce a negative tax (a saving)
valued at the same rate (assumes they offset gains of the same character).
"""

from __future__ import annotations

from dataclasses import dataclass

from committee.config.schema import TaxConfig
from committee.engines.tax.holding import Term


@dataclass(frozen=True)
class TaxRates:
    ordinary: float
    ltcg: float
    state: float = 0.0
    niit: float = 0.0
    apply_niit: bool = False

    @classmethod
    def from_config(cls, cfg: TaxConfig) -> TaxRates:
        return cls(
            ordinary=cfg.assumed_ordinary_rate,
            ltcg=cfg.assumed_ltcg_rate,
            state=cfg.state_rate,
            niit=cfg.niit_rate,
            apply_niit=cfg.apply_niit,
        )

    def rate(self, term: Term) -> float:
        base = self.ordinary if term == "short" else self.ltcg
        return base + self.state + (self.niit if self.apply_niit else 0.0)

    def tax_on(self, gain: float, term: Term) -> float:
        """Tax due on a realized gain (negative = tax saved by a loss)."""
        return gain * self.rate(term)
