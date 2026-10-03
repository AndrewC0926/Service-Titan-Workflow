from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from committee.agents.packets import build_review_packet
from committee.config.loader import load_config
from committee.orchestration.pit_source import PITSource
from fixtures.signals.builder import ASOF, LakeBuilder

CFG = load_config(Path(__file__).resolve().parents[1] / "config")
S = "CIK0000000001"
OLD = "Supply chain risk from overseas vendors.\n\nCompetition may reduce margins."
NEW = "Cyber attacks may disrupt operations.\n\nLitigation could be costly and lengthy."


@pytest.fixture
def src(tmp_path: Path) -> PITSource:
    b = LakeBuilder(tmp_path)
    b.company(S, "AAA", price=40, shares=1e8)
    b.insider(S, "i1", dt.date(2026, 9, 5), shares=20_000, price=40)
    for acc, year, text in ((f"{S}-24", 2024, OLD), (f"{S}-25", 2025, NEW)):
        b.section(S, acc, "10-K", dt.date(year, 12, 31), "1A", text, dt.date(year + 1, 2, 20))
        b.section(
            S,
            acc,
            "10-K",
            dt.date(year, 12, 31),
            "7",
            "Revenue grew modestly.",
            dt.date(year + 1, 2, 20),
        )
    b.news(S, ASOF - dt.timedelta(days=3), 1)
    b.news(S, ASOF + dt.timedelta(days=3), 2)  # future article: must not appear
    b.flush()
    return PITSource(b.pit(), CFG.scenarios)


def test_security_and_facts(src: PITSource) -> None:
    info = src.security(S, ASOF)
    assert info.ticker == "AAA" and info.size_bucket
    assert src.market_cap(S, ASOF) == pytest.approx(40 * 1e8, rel=1e-6)
    facts = src.trading_facts(S, ASOF)
    assert facts["price"] == pytest.approx(40) and facts["adv_usd"] and facts["annual_vol"]
    with pytest.raises(KeyError):
        src.security("CIK9999999999", ASOF)


def test_every_kind_is_as_of(src: PITSource) -> None:
    assert src.fetch("valuation", S, ASOF)[0]["market_cap_musd"] == pytest.approx(4000)
    assert src.fetch("insider_txns", S, ASOF)[0]["txn_code"] == "P"
    diffs = src.fetch("filing_diffs", S, ASOF)
    assert diffs and diffs[0]["item"] == "1A" and "Cyber" in diffs[0]["added"]
    news = src.fetch("news", S, ASOF)
    assert len(news) == 1
    assert len(src.fetch("prices", S, ASOF)) > 200
    assert {r["id"] for r in src.fetch("scenarios", S, ASOF)} >= {"rate_shock"}
    assert src.fetch("call_tone", S, ASOF) == []
    # before the 2025 10-K was filed there is nothing to compare
    assert src.fetch("filing_diffs", S, dt.date(2026, 1, 15)) == []


def test_packet_from_lake_is_anonymized_and_wrapped(src: PITSource) -> None:
    pkt = build_review_packet(src, S, ASOF, "rv-1", bucket_tag="CORE_PICK")
    text = pkt.model_dump_json()
    assert "AAA" not in text
    assert "<untrusted_content>" in text
    assert pkt.items and all(i.id.startswith("E") for i in pkt.items)
