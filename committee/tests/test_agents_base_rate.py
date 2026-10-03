from __future__ import annotations

import datetime as dt
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from committee.agents.base_rate_table import (
    Profile,
    attach_signal_profiles,
    forward_outcomes,
    load_samples_from_pit,
    reference_class_table,
    size_bucket,
)
from committee.data.lake import Lake, new_ingest_id
from committee.data.pit import PIT


def _bdays(n: int, start: str = "2020-01-01") -> pd.DatetimeIndex:
    return pd.bdate_range(start, periods=n)


def test_forward_outcomes_known_answer() -> None:
    dates = _bdays(400)
    up = pd.DataFrame(
        {"security_id": "UP", "date": dates, "adj_close": 100 * 1.001 ** np.arange(400)}
    )
    crash = np.full(400, 100.0)
    crash[10:] = 60.0  # -40% after day 10
    down = pd.DataFrame({"security_id": "DN", "date": dates, "adj_close": crash})
    bench = pd.Series(100.0, index=dates)  # flat benchmark
    out = forward_outcomes(pd.concat([up, down]), bench)
    u0 = out[(out.security_id == "UP")].iloc[0]
    assert u0["excess_3m"] == pytest.approx(1.001**63 - 1)
    assert u0["max_dd_12m"] > 0  # never below start
    d0 = out[(out.security_id == "DN")].iloc[0]
    assert d0["excess_12m"] == pytest.approx(-0.4)
    assert d0["max_dd_12m"] == pytest.approx(-0.4)
    # right-censoring: last rows have no forward outcomes
    assert np.isnan(out[out.security_id == "UP"].iloc[-1]["excess_3m"])
    assert np.isnan(out[out.security_id == "UP"].iloc[-200]["max_dd_12m"])


def _samples() -> pd.DataFrame:
    rows = []
    for i in range(60):  # Tech/small/momentum: 40 winners of 60
        rows.append(
            (
                "T",
                "Technology",
                "small",
                "momentum",
                0.1 if i < 40 else -0.1,
                -0.4 if i < 6 else -0.1,
            )
        )
    for i in range(100):  # Energy/large/no signals: 30 winners
        rows.append(("E", "Energy", "large", "", 0.1 if i < 30 else -0.1, -0.1))
    for i in range(20):  # Tech/small/value
        rows.append(("V", "Technology", "small", "value", -0.1, -0.1))
    df = pd.DataFrame(
        rows, columns=["security_id", "sector", "size_bucket", "signals", "x", "max_dd_12m"]
    )
    df["date"] = pd.Timestamp("2020-01-01")
    for h in (3, 6, 12):
        df[f"excess_{h}m"] = df["x"]
    return df.drop(columns="x")


def test_reference_class_exact_match() -> None:
    t = reference_class_table(
        _samples(),
        Profile(sector="Technology", size_bucket="small", signals=frozenset({"momentum"})),
    )
    assert t.match_level == "sector+size+signals" and t.n_samples == 60
    assert t.frequency("beats_benchmark", 12) == pytest.approx(40 / 60, abs=1e-4)
    assert t.frequency("drawdown_exceeds_30pct", 12) == pytest.approx(0.1)
    assert all(o.n == 60 for o in t.outcomes)
    ev = t.as_evidence()[0]
    assert ev["n_samples"] == 60 and ev["computed_by"].startswith("code")


def test_reference_class_relaxes_when_sample_small() -> None:
    t = reference_class_table(
        _samples(),
        Profile(sector="Technology", size_bucket="small", signals=frozenset({"value"})),
        min_n=30,
    )
    assert t.match_level == "sector+size" and t.n_samples == 80
    t = reference_class_table(
        _samples(), Profile(sector="Health Care", size_bucket="mid"), min_n=30
    )
    assert t.match_level == "all" and t.n_samples == 180
    t = reference_class_table(
        _samples(), Profile(sector="Energy", size_bucket="large"), exclude_security="E"
    )
    assert t.n_samples == 80 and t.match_level == "all"


def test_size_bucket() -> None:
    assert (
        size_bucket(5e8) == "small" and size_bucket(5e9) == "mid" and size_bucket(5e10) == "large"
    )
    assert size_bucket(None) == "unknown" and size_bucket(float("nan")) == "unknown"


def test_attach_signal_profiles_window() -> None:
    samples = pd.DataFrame(
        {"security_id": ["A", "A"], "date": pd.to_datetime(["2020-02-01", "2020-06-01"])}
    )
    sig = pd.DataFrame(
        {
            "security_id": ["A", "A", "A"],
            "asof": ["2020-01-20", "2020-01-25", "2020-05-30"],
            "signal_name": ["momentum", "value", "value"],
            "zscore": [2.0, 0.5, 1.5],
        }
    )
    out = attach_signal_profiles(samples, sig)
    assert out["signals"].tolist() == [frozenset({"momentum"}), frozenset({"value"})]
    assert attach_signal_profiles(samples, pd.DataFrame())["signals"].tolist() == [
        frozenset(),
        frozenset(),
    ]


def test_load_samples_from_pit_respects_asof(tmp_path: Path) -> None:
    lake = Lake(tmp_path / "lake")
    dates = _bdays(320, "2024-01-01")
    rows = []
    for sid, growth in (("CIKA", 1.002), ("BENCH", 1.0005)):
        for i, d in enumerate(dates):
            kt = dt.datetime.combine(d.date(), dt.time(20, 30), tzinfo=dt.UTC)
            rows.append(
                {
                    "security_id": sid,
                    "date": d.date(),
                    "adj_close": 50 * growth**i,
                    "event_time": kt,
                    "known_time": kt,
                }
            )
    lake.write("prices_daily", rows, source="t", ingest_id=new_ingest_id())
    t0 = dt.datetime(2023, 1, 1, tzinfo=dt.UTC)
    lake.write(
        "security_master",
        [
            {
                "security_id": "CIKA",
                "sector": "Technology",
                "ticker": "A",
                "event_time": t0,
                "known_time": t0,
            }
        ],
        source="t",
        ingest_id=new_ingest_id(),
    )
    lake.write(
        "fundamentals",
        [
            {
                "security_id": "CIKA",
                "metric": "shares_outstanding",
                "fiscal_period": "FY2023",
                "value": 1e7,
                "event_time": t0,
                "known_time": t0,
            }
        ],
        source="t",
        ingest_id=new_ingest_id(),
    )
    pit = PIT(lake)
    asof = dates[-1].date()
    samples = load_samples_from_pit(pit, asof, benchmark_security_id="BENCH", sample_every_days=21)
    assert set(samples["security_id"]) == {"CIKA"}
    assert (samples["sector"] == "Technology").all()
    assert (samples["size_bucket"] == "small").all()
    assert samples["excess_3m"].dropna().gt(0).all()
    # outcomes beyond asof are unknowable: only samples >= 252 days before asof have 12m data
    assert samples["excess_12m"].notna().sum() == 4  # sample rows 0, 21, 42, 63 of 320
    # an earlier as-of sees fewer prices
    early = load_samples_from_pit(pit, dates[100].date(), benchmark_security_id="BENCH")
    assert len(early) < len(samples)
    assert load_samples_from_pit(
        PIT(Lake(tmp_path / "empty")), asof, benchmark_security_id="BENCH"
    ).empty
