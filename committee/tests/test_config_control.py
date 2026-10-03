from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest
import yaml

from committee.config.control import active_version, load_active_config, pending_versions, sync
from committee.config.loader import ConfigError
from committee.journal.store import Journal

T0 = dt.datetime(2026, 10, 1, tzinfo=dt.UTC)


def _set_core_max(project: Path, value: float) -> None:
    p = project / "config" / "risk_limits.yaml"
    d = yaml.safe_load(p.read_text())
    d["buckets"]["core_pick"]["max_pct_total"] = value
    p.write_text(yaml.safe_dump(d))


def test_first_sync_is_baseline_effective_now(project: Path, tmp_path: Path) -> None:
    j = Journal(tmp_path / "j.sqlite")
    res = sync(j, project / "config", T0)
    assert {r.status for r in res} == {"baseline"}
    assert sync(j, project / "config", T0)[0].status == "unchanged"
    cfg = load_active_config(j, project / "config", T0)
    assert cfg.risk_limits.buckets.core_pick.max_pct_total == 6.0


def test_change_waits_seven_days(project: Path, tmp_path: Path) -> None:
    j = Journal(tmp_path / "j.sqlite")
    sync(j, project / "config", T0)
    _set_core_max(project, 8.0)
    t1 = T0 + dt.timedelta(days=1)
    cfg = load_active_config(j, project / "config", t1)
    assert cfg.risk_limits.buckets.core_pick.max_pct_total == 6.0  # old value still active
    pend = pending_versions(j, t1)
    assert len(pend) == 1 and pend[0].effective_at == t1 + dt.timedelta(days=7)
    just_before = t1 + dt.timedelta(days=7) - dt.timedelta(seconds=1)
    assert (
        load_active_config(
            j, project / "config", just_before
        ).risk_limits.buckets.core_pick.max_pct_total
        == 6.0
    )
    on_time = t1 + dt.timedelta(days=7)
    assert (
        load_active_config(
            j, project / "config", on_time
        ).risk_limits.buckets.core_pick.max_pct_total
        == 8.0
    )
    assert pending_versions(j, on_time) == []
    assert j.verify().ok


def test_second_edit_restarts_its_own_clock(project: Path, tmp_path: Path) -> None:
    j = Journal(tmp_path / "j.sqlite")
    sync(j, project / "config", T0)
    _set_core_max(project, 8.0)
    sync(j, project / "config", T0)
    _set_core_max(project, 5.0)
    sync(j, project / "config", T0 + dt.timedelta(days=3))
    v = active_version(j, "risk_limits.yaml", T0 + dt.timedelta(days=8))
    assert v is not None and v.content["buckets"]["core_pick"]["max_pct_total"] == 8.0
    v = active_version(j, "risk_limits.yaml", T0 + dt.timedelta(days=10))
    assert v is not None and v.content["buckets"]["core_pick"]["max_pct_total"] == 5.0


def test_invalid_change_is_never_journaled(project: Path, tmp_path: Path) -> None:
    j = Journal(tmp_path / "j.sqlite")
    sync(j, project / "config", T0)
    n = j.count()
    _set_core_max(project, -1.0)
    with pytest.raises(ConfigError):
        sync(j, project / "config", T0)
    assert j.count() == n
