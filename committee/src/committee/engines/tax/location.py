"""Asset-location recommender for new purchases (DESIGN 3 and 9).

Preference order per sleeve kind comes from ``TaxConfig.location_preference``
(falling back to ``DEFAULT_PREFERENCE``). Defaults: satellite (high turnover)
-> IRA first, then taxable; broad equity index -> taxable; tilts -> IRA;
bonds, TIPS and trend -> tax-deferred; liquidity -> taxable.

The amount is placed in the first preferred account with enough cash; if no
single account can take it all, it is split across accounts in preference
order and any remainder is reported as unfunded. Accounts not listed in the
preference are never used.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from committee.domain import AccountKind
from committee.engines.tax.labels import labeled

DEFAULT_PREFERENCE: dict[str, tuple[AccountKind, ...]] = {
    "satellite": ("ira", "taxable"),
    "core_equity": ("taxable", "ira"),
    "core_tilt": ("ira", "taxable"),
    "diversifier": ("ira", "k401", "taxable"),
    "bonds": ("ira", "k401", "taxable"),
    "liquidity": ("taxable",),
    "speculative": ("taxable",),
}

ALIASES: dict[str, str] = {
    "broad_index": "core_equity",
    "broad_equity": "core_equity",
    "index": "core_equity",
    "tips": "bonds",
    "bond": "bonds",
    "trend": "diversifier",
    "managed_futures": "diversifier",
    "cash": "liquidity",
    "tbills": "liquidity",
}


@dataclass(frozen=True)
class LocationRecommendation:
    kind: str
    amount: float
    preference: tuple[AccountKind, ...]
    allocations: tuple[tuple[AccountKind, float], ...]
    unfunded: float
    reason: str

    @property
    def account(self) -> AccountKind | None:
        """The single account when the purchase fits in one, else None."""
        return self.allocations[0][0] if len(self.allocations) == 1 and not self.unfunded else None


def preference_for(
    kind: str, config_pref: Mapping[str, list[AccountKind]] | None = None
) -> tuple[AccountKind, ...]:
    k = ALIASES.get(kind, kind)
    if config_pref and k in config_pref:
        return tuple(config_pref[k])
    if k in DEFAULT_PREFERENCE:
        return DEFAULT_PREFERENCE[k]
    raise ValueError(f"unknown sleeve kind {kind!r}")


def recommend_location(
    kind: str,
    amount: float,
    cash: Mapping[AccountKind, float],
    config_pref: Mapping[str, list[AccountKind]] | None = None,
) -> LocationRecommendation:
    if amount <= 0:
        raise ValueError("amount must be positive")
    pref = preference_for(kind, config_pref)
    for acct in pref:
        if cash.get(acct, 0.0) >= amount:
            return LocationRecommendation(
                kind,
                amount,
                pref,
                ((acct, amount),),
                0.0,
                labeled(
                    f"{kind}: place {amount:,.2f} in {acct} "
                    f"(preference {' > '.join(pref)}; first account with enough cash)."
                ),
            )
    allocs: list[tuple[AccountKind, float]] = []
    need = amount
    for acct in pref:
        take = min(need, max(0.0, cash.get(acct, 0.0)))
        if take > 0:
            allocs.append((acct, take))
            need -= take
    parts = ", ".join(f"{a} {v:,.2f}" for a, v in allocs) or "nothing"
    return LocationRecommendation(
        kind,
        amount,
        pref,
        tuple(allocs),
        need,
        labeled(
            f"{kind}: no single account has {amount:,.2f}; split {parts}"
            + (f"; {need:,.2f} unfunded." if need > 0 else ".")
        ),
    )
