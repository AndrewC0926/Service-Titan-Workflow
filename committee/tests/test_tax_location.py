"""Asset-location recommender."""

from __future__ import annotations

import pytest

from committee.domain import AccountKind
from committee.engines.tax import DISCLAIMER, recommend_location
from committee.engines.tax.location import preference_for

PLENTY: dict[AccountKind, float] = {"taxable": 1e6, "ira": 1e6, "k401": 1e6}


@pytest.mark.parametrize(
    ("kind", "account"),
    [
        ("satellite", "ira"),
        ("core_equity", "taxable"),
        ("broad_index", "taxable"),
        ("core_tilt", "ira"),
        ("bonds", "ira"),
        ("tips", "ira"),
        ("trend", "ira"),
        ("diversifier", "ira"),
        ("liquidity", "taxable"),
        ("speculative", "taxable"),
    ],
)
def test_default_locations(kind: str, account: AccountKind) -> None:
    rec = recommend_location(kind, 5000, PLENTY)
    assert rec.account == account
    assert DISCLAIMER in rec.reason


def test_config_preferences_are_respected(ctx) -> None:  # type: ignore[no-untyped-def]
    pref = ctx.config.tax.location_preference
    assert recommend_location("satellite", 1000, PLENTY, pref).account == "ira"
    assert recommend_location("bonds", 1000, {"k401": 5000}, pref).account == "k401"
    custom = {"satellite": ["taxable"]}
    assert recommend_location("satellite", 1000, PLENTY, custom).account == "taxable"
    assert preference_for("trend", pref) == ("ira", "k401", "taxable")


def test_satellite_falls_back_to_taxable_when_ira_is_short() -> None:
    rec = recommend_location("satellite", 5000, {"ira": 1000, "taxable": 10000})
    assert rec.account == "taxable"


def test_split_across_accounts_and_unfunded() -> None:
    rec = recommend_location("satellite", 5000, {"ira": 3000, "taxable": 1000})
    assert rec.account is None
    assert rec.allocations == (("ira", 3000), ("taxable", 1000))
    assert rec.unfunded == pytest.approx(1000)
    assert "unfunded" in rec.reason
    # liquidity never goes to the IRA even when only the IRA has cash
    rec2 = recommend_location("liquidity", 500, {"ira": 10000})
    assert rec2.allocations == () and rec2.unfunded == 500


def test_split_fully_funded() -> None:
    rec = recommend_location("bonds", 5000, {"ira": 3000, "k401": 2000})
    assert rec.allocations == (("ira", 3000), ("k401", 2000)) and rec.unfunded == 0
    assert rec.account is None


def test_invalid_inputs() -> None:
    with pytest.raises(ValueError):
        recommend_location("satellite", 0, PLENTY)
    with pytest.raises(ValueError):
        recommend_location("crypto_moonshot", 10, PLENTY)
