"""Block 4C Item 1: nightly pg_dump + a tested restore
(app.pipeline.backup)."""
from datetime import datetime

import pytest

from app.db import get_session
from app.pipeline.backup import (
    DUMP_PREFIX, DUMP_SUFFIX, _server_url, _stamp_of, latest_dump, list_dumps,
    prune_old_dumps, restore_drill, run_backup,
)

REAL_LOCAL_DB = "postgresql://scout:scout@localhost:5432/scout_local"


@pytest.fixture
def client(db_session):
    from fastapi.testclient import TestClient

    from app.web.main import app as web_app
    web_app.dependency_overrides[get_session] = lambda: db_session
    yield TestClient(web_app)
    web_app.dependency_overrides.clear()


class TestInternalBackupAuth:
    def test_503_when_backup_api_key_unset(self, client, monkeypatch):
        monkeypatch.delenv("BACKUP_API_KEY", raising=False)
        resp = client.post("/internal/backup")
        assert resp.status_code == 503

    def test_401_with_no_bearer_token(self, client, monkeypatch):
        monkeypatch.setenv("BACKUP_API_KEY", "realkey")
        resp = client.post("/internal/backup")
        assert resp.status_code == 401

    def test_401_with_wrong_bearer_token(self, client, monkeypatch):
        monkeypatch.setenv("BACKUP_API_KEY", "realkey")
        resp = client.post("/internal/backup", headers={"Authorization": "Bearer wrongkey"})
        assert resp.status_code == 401

    def test_runs_a_real_backup_with_the_right_token(self, client, monkeypatch, tmp_path):
        monkeypatch.setenv("BACKUP_API_KEY", "realkey")
        monkeypatch.setenv("BACKUP_DIR", str(tmp_path))
        monkeypatch.setenv("DATABASE_URL", REAL_LOCAL_DB)
        if not _postgres_reachable():
            pytest.skip("needs the real local Postgres restore")
        resp = client.post("/internal/backup", headers={"Authorization": "Bearer realkey"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["size_bytes"] > 0
        assert latest_dump(tmp_path) is not None


def _touch_dump(out_dir, stamp: str) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / f"{DUMP_PREFIX}{stamp}{DUMP_SUFFIX}").write_bytes(b"not a real dump, just a marker")


class TestStampParsing:
    def test_recognizes_a_well_formed_name(self, tmp_path):
        assert _stamp_of(tmp_path / "scout-2026-09-01.dump") == datetime(2026, 9, 1)

    def test_ignores_a_file_with_the_wrong_prefix(self, tmp_path):
        assert _stamp_of(tmp_path / "backup-2026-09-01.dump") is None

    def test_ignores_a_file_with_the_wrong_suffix(self, tmp_path):
        assert _stamp_of(tmp_path / "scout-2026-09-01.sql") is None

    def test_ignores_an_unparseable_date(self, tmp_path):
        assert _stamp_of(tmp_path / "scout-not-a-date.dump") is None


class TestListAndLatestDumps:
    def test_empty_directory(self, tmp_path):
        assert list_dumps(tmp_path / "nonexistent") == []
        assert latest_dump(tmp_path) is None

    def test_sorted_oldest_first_and_latest_is_newest(self, tmp_path):
        _touch_dump(tmp_path, "2026-09-01")
        _touch_dump(tmp_path, "2026-09-03")
        _touch_dump(tmp_path, "2026-09-02")
        dumps = list_dumps(tmp_path)
        assert [p.name for p in dumps] == [
            "scout-2026-09-01.dump", "scout-2026-09-02.dump", "scout-2026-09-03.dump",
        ]
        assert latest_dump(tmp_path).name == "scout-2026-09-03.dump"

    def test_ignores_unrelated_files(self, tmp_path):
        _touch_dump(tmp_path, "2026-09-01")
        (tmp_path / "README.txt").write_text("not a dump")
        assert [p.name for p in list_dumps(tmp_path)] == ["scout-2026-09-01.dump"]


class TestPruning:
    def test_keeps_everything_inside_retention(self, tmp_path):
        now = datetime(2026, 9, 13)
        _touch_dump(tmp_path, "2026-09-01")
        _touch_dump(tmp_path, "2026-09-10")
        pruned = prune_old_dumps(tmp_path, retention_days=14, now=now)
        assert pruned == []
        assert len(list_dumps(tmp_path)) == 2

    def test_prunes_only_what_is_past_retention(self, tmp_path):
        now = datetime(2026, 9, 13)
        _touch_dump(tmp_path, "2026-08-01")  # 43 days old -- past 14
        _touch_dump(tmp_path, "2026-09-10")  # 3 days old -- kept
        pruned = prune_old_dumps(tmp_path, retention_days=14, now=now)
        assert pruned == ["scout-2026-08-01.dump"]
        assert [p.name for p in list_dumps(tmp_path)] == ["scout-2026-09-10.dump"]

    def test_never_prunes_the_only_remaining_dump_even_if_ancient(self, tmp_path):
        """A source down for three weeks should still leave one backup on
        disk, not zero -- stale beats nothing for a real recovery."""
        now = datetime(2026, 9, 13)
        _touch_dump(tmp_path, "2026-01-01")
        pruned = prune_old_dumps(tmp_path, retention_days=14, now=now)
        assert pruned == []
        assert len(list_dumps(tmp_path)) == 1


class TestServerUrl:
    def test_swaps_only_the_database_name(self):
        url = _server_url("postgresql://scout:scout@localhost:5432/scout_local", "postgres")
        assert url == "postgresql://scout:scout@localhost:5432/postgres"

    def test_preserves_query_string(self):
        url = _server_url("postgresql://u:p@host:5432/db?sslmode=require", "scratch")
        assert url == "postgresql://u:p@host:5432/scratch?sslmode=require"


def _postgres_reachable() -> bool:
    try:
        from sqlalchemy import create_engine, text
        engine = create_engine(REAL_LOCAL_DB)
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        engine.dispose()
        return True
    except Exception:
        return False


@pytest.mark.skipif(not _postgres_reachable(), reason="needs the real local Postgres restore")
class TestBackupAndRestoreDrillAgainstRealPostgres:
    """The one test in this file that touches a real Postgres server --
    everything above is pure filesystem/string logic against tmp_path.
    Skipped (not failed) when the local restore named in this session's
    own instructions isn't reachable, e.g. in an environment with no
    Postgres at all."""

    def test_backup_then_restore_drill_asserts_real_row_counts(self, tmp_path):
        result = run_backup(REAL_LOCAL_DB, out_dir=tmp_path)
        assert result["size_bytes"] > 0
        dump_path = latest_dump(tmp_path)
        assert dump_path is not None

        drill = restore_drill(REAL_LOCAL_DB, dump_path=dump_path)
        assert drill["ok"] is True
        for table in ("projects", "signals", "opportunities", "metric_snapshots"):
            assert drill["tables"][table] > 0

    def test_restore_drill_fails_loudly_on_a_garbage_dump(self, tmp_path):
        garbage = tmp_path / "scout-2026-01-01.dump"
        garbage.write_bytes(b"this is not a real pg_dump file")
        with pytest.raises(Exception):
            restore_drill(REAL_LOCAL_DB, dump_path=garbage, scratch_db_name="scout_restore_drill_garbage_test")
