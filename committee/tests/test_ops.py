from __future__ import annotations

import datetime as dt
from pathlib import Path

import httpx
import pytest

from committee.journal.store import Journal
from committee.ops import backup
from committee.ops.alerts import Alerter, ntfy_sender
from committee.ops.flags import Flags
from committee.ops.health import check_health
from committee.ops.scheduler import JOBS, Job, build_scheduler, run_job


@pytest.fixture
def var(tmp_path: Path) -> Path:
    v = tmp_path / "var"
    j = Journal(v / "journal.sqlite")
    for i in range(5):
        j.append("note", {"i": i})
    j.close()
    (v / "data" / "prices_daily").mkdir(parents=True)
    (v / "data" / "prices_daily" / "p.parquet").write_bytes(b"PAR1")
    (v / "flags").mkdir()
    (v / "flags" / "kill_switch.json").write_text("{}")
    return v


def test_backup_roundtrip_and_restore_test(var: Path, tmp_path: Path) -> None:
    b = backup.create_backup(var, tmp_path / "bk", "correct horse battery")
    assert b.read_bytes().startswith(backup.MAGIC)
    assert b"journal" not in b.read_bytes()[:200]  # encrypted, not a plain tar
    names = backup.restore_backup(b, "correct horse battery", tmp_path / "restore")
    assert "journal.sqlite" in names and "data/prices_daily/p.parquet" in names
    assert not any(n.startswith("flags") for n in names)
    rt = backup.restore_test(b, "correct horse battery")
    assert rt.ok and rt.journal_entries == 5
    bad = backup.restore_test(b, "wrong")
    assert not bad.ok and "passphrase" in bad.detail


def test_backup_requires_passphrase_and_magic(var: Path, tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        backup.create_backup(var, tmp_path, "")
    junk = tmp_path / "x.bak"
    junk.write_bytes(b"nope")
    with pytest.raises(ValueError, match="not a Committee backup"):
        backup.decrypt_backup(junk, "x")


def test_restore_test_detects_tampered_journal(var: Path, tmp_path: Path) -> None:
    import sqlite3

    con = sqlite3.connect(var / "journal.sqlite")
    con.execute("DROP TRIGGER journal_no_update")
    con.execute("UPDATE journal SET payload_json='{\"i\":99}' WHERE seq=2")
    con.commit()
    con.close()
    b = backup.create_backup(var, tmp_path / "bk", "pw-pw-pw")
    rt = backup.restore_test(b, "pw-pw-pw")
    assert not rt.ok and "seq 2" in rt.detail


def test_prune(tmp_path: Path) -> None:
    for i in range(5):
        (tmp_path / f"committee-2026010{i}T000000Z.bak").write_bytes(b"x")
    removed = backup.prune(tmp_path, keep=2)
    assert len(removed) == 3 and len(list(tmp_path.glob("*.bak"))) == 2


def test_run_job_journals_failures(tmp_path: Path) -> None:
    j = Journal(tmp_path / "j.sqlite")
    alerter = Alerter()
    ok = run_job(Job("x", "0 0 * * *", "data check"), j, alerter, runner=lambda a: (0, "fine"))
    assert ok.ok and j.count() == 0
    bad = run_job(
        Job("y", "0 0 * * *", "journal verify", "P1"),
        j,
        alerter,
        runner=lambda a: (1, "broken chain"),
    )
    assert not bad.ok
    inc = j.latest("incident")
    assert inc is not None and inc.payload["level"] == "P1" and inc.payload["job"] == "y"
    assert alerter.sent and "P1" in alerter.sent[0][0]

    def boom(a: list[str]) -> tuple[int, str]:
        raise RuntimeError("no python")

    assert run_job(Job("z", "0 0 * * *", "x"), j, alerter, runner=boom).returncode == 99


def test_alerter_survives_sender_failure() -> None:
    def broken(s: str, b: str) -> None:
        raise ConnectionError("smtp down")

    a = Alerter([broken])
    a.alert("P2", "subject", "body")
    assert a.failures and a.sent


def test_ntfy_sender() -> None:
    seen: list[httpx.Request] = []
    send = ntfy_sender(
        "https://ntfy.example/committee",
        transport=httpx.MockTransport(lambda r: seen.append(r) or httpx.Response(200)),
    )
    send("hello", "world")
    assert seen[0].headers["Title"] == "hello" and seen[0].content == b"world"


def test_scheduler_registers_all_jobs(tmp_path: Path) -> None:
    sched = build_scheduler(
        lambda: Journal(tmp_path / "j.sqlite"), Alerter(), runner=lambda a: (0, "")
    )
    ids = {job.id for job in sched.get_jobs()}  # type: ignore[attr-defined]
    assert ids == {j.id for j in JOBS}
    assert len({j.id for j in JOBS}) == len(JOBS)


def test_health(tmp_path: Path) -> None:
    j = Journal(tmp_path / "j.sqlite")
    flags = Flags(tmp_path / "flags")
    now = dt.datetime.now(dt.UTC)
    checks = {c.name: c for c in check_health(j, flags, tmp_path, now)}
    assert checks["journal"].ok and not checks["ingest_fresh"].ok and checks["orders_enabled"].ok
    j.append("ingest_summary", {"n": 1})
    j.append("incident", {"level": "P1", "kind": "x"})
    flags.set("kill_switch", "drill")
    checks = {c.name: c for c in check_health(j, flags, tmp_path, now)}
    assert (
        checks["ingest_fresh"].ok and not checks["no_p1_60d"].ok and not checks["orders_enabled"].ok
    )
