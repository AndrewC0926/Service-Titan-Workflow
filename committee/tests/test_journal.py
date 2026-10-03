from __future__ import annotations

import datetime as dt
import json
import sqlite3
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from committee.journal.anchor import check_anchors, write_anchor
from committee.journal.canonical import canonical_json
from committee.journal.store import GENESIS_HASH, Journal


class Clock:
    def __init__(self) -> None:
        self.t = dt.datetime(2026, 10, 1, 12, 0, tzinfo=dt.UTC)

    def __call__(self) -> dt.datetime:
        self.t += dt.timedelta(seconds=1)
        return self.t


@pytest.fixture
def journal(tmp_path: Path) -> Journal:
    return Journal(tmp_path / "j.sqlite", clock=Clock())


def test_append_chains(journal: Journal) -> None:
    a = journal.append("note", {"x": 1})
    b = journal.append("note", {"x": 2})
    assert a.seq == 1 and a.prev_hash == GENESIS_HASH
    assert b.prev_hash == a.hash
    rep = journal.verify()
    assert rep.ok and rep.entries == 2 and rep.last_hash == b.hash


def test_unknown_type_and_non_object_rejected(journal: Journal) -> None:
    with pytest.raises(ValueError):
        journal.append("whatever", {})
    with pytest.raises(ValueError):
        journal.append("note", [1, 2])  # type: ignore[arg-type]


def test_triggers_block_update_and_delete(journal: Journal, tmp_path: Path) -> None:
    journal.append("note", {"x": 1})
    con = sqlite3.connect(tmp_path / "j.sqlite")
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        con.execute("UPDATE journal SET payload_json='{}' WHERE seq=1")
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        con.execute("DELETE FROM journal")


def _tamper(path: Path, sql: str) -> None:
    con = sqlite3.connect(path)
    con.execute("DROP TRIGGER journal_no_update")
    con.execute("DROP TRIGGER journal_no_delete")
    con.execute(sql)
    con.commit()
    con.close()


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE journal SET payload_json='{\"x\":9}' WHERE seq=2",
        "UPDATE journal SET entry_type='decision' WHERE seq=2",
        "UPDATE journal SET created_at_utc='2020-01-01T00:00:00+00:00' WHERE seq=2",
        "UPDATE journal SET prev_hash='" + "1" * 64 + "' WHERE seq=2",
        "DELETE FROM journal WHERE seq=2",
        "UPDATE journal SET hash='" + "f" * 64 + "' WHERE seq=3",
    ],
)
def test_any_tampering_detected(tmp_path: Path, sql: str) -> None:
    p = tmp_path / "j.sqlite"
    j = Journal(p, clock=Clock())
    for i in range(4):
        j.append("note", {"x": i})
    j.close()
    _tamper(p, sql)
    rep = Journal(p).verify()
    assert not rep.ok
    assert rep.first_break


def test_tampering_with_recomputed_hash_breaks_next_link(tmp_path: Path) -> None:
    """Even a forger who recomputes the row's own hash breaks the following link."""
    from committee.journal.store import compute_hash

    p = tmp_path / "j.sqlite"
    j = Journal(p, clock=Clock())
    for i in range(3):
        j.append("note", {"x": i})
    e2 = j.get(2)
    assert e2 is not None
    j.close()
    pj = canonical_json({"x": 99})
    h = compute_hash(e2.prev_hash, 2, "note", e2.created_at_utc, pj)
    _tamper(p, f"UPDATE journal SET payload_json='{pj}', hash='{h}' WHERE seq=2")
    rep = Journal(p).verify()
    assert not rep.ok
    assert any("seq 3" in e for e in rep.errors)


def test_canonical_json_stable() -> None:
    a = canonical_json({"b": 1, "a": [1, 2, {"z": 1, "y": 2}], "c": "é"})
    b = canonical_json({"c": "é", "a": [1, 2, {"y": 2, "z": 1}], "b": 1})
    assert a == b == '{"a":[1,2,{"y":2,"z":1}],"b":1,"c":"é"}'
    with pytest.raises(ValueError):
        canonical_json({"x": float("nan")})
    with pytest.raises(ValueError):
        canonical_json({"t": dt.datetime(2026, 1, 1)})
    assert (
        canonical_json({"t": dt.datetime(2026, 1, 1, tzinfo=dt.UTC)})
        == '{"t":"2026-01-01T00:00:00+00:00"}'
    )


@given(
    st.dictionaries(
        st.text(max_size=5), st.one_of(st.integers(), st.text(max_size=5), st.booleans(), st.none())
    )
)
def test_canonical_roundtrip_property(d: dict[str, object]) -> None:
    s = canonical_json(d)
    assert canonical_json(json.loads(s)) == s


def test_anchor_written_and_checked(journal: Journal, tmp_path: Path) -> None:
    journal.append("note", {"x": 1})
    sent: list[str] = []
    path = write_anchor(journal, tmp_path / "anchors", email=lambda s, b: sent.append(s))
    data = json.loads(path.read_text())
    assert data["seq"] == 1 and len(data["hash"]) == 64
    assert sent and "anchor" in sent[0]
    assert journal.latest("anchor") is not None
    assert check_anchors(journal, tmp_path / "anchors") == []
    p2 = write_anchor(journal, tmp_path / "anchors")
    assert p2 != path


def test_anchor_detects_rewritten_history(tmp_path: Path) -> None:
    p = tmp_path / "j.sqlite"
    j = Journal(p, clock=Clock())
    j.append("note", {"x": 1})
    write_anchor(j, tmp_path / "a")
    j.close()
    p.unlink()
    j2 = Journal(p, clock=Clock())
    j2.append("note", {"x": "forged"})
    assert check_anchors(j2, tmp_path / "a")


def test_entries_filter_and_lookup(journal: Journal) -> None:
    journal.append("note", {"x": 1})
    d = journal.append("decision", {"y": 1})
    assert [e.seq for e in journal.entries("decision")] == [2]
    assert journal.find_by_hash(d.hash) == d
    assert journal.count() == 2
    assert journal.latest("note") is not None


def test_cli_verify_anchor_and_freeze(project: Path) -> None:
    from typer.testing import CliRunner

    from committee.cli import app

    r = CliRunner()
    res = r.invoke(app, ["config", "pending", "--root", str(project)])
    assert res.exit_code == 0 and "baseline" in res.output
    assert r.invoke(app, ["journal", "anchor", "--root", str(project)]).exit_code == 0
    res = r.invoke(app, ["journal", "verify", "--root", str(project)])
    assert res.exit_code == 0 and "journal OK" in res.output
    assert r.invoke(app, ["journal", "tail", "--root", str(project)]).exit_code == 0
    _tamper(project / "var" / "journal.sqlite", "UPDATE journal SET payload_json='{}' WHERE seq=1")
    res = r.invoke(app, ["journal", "verify", "--root", str(project)])
    assert res.exit_code == 1
    assert (project / "var" / "flags" / "frozen.json").exists()
