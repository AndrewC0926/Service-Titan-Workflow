from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from committee.cli import app
from committee.config.loader import ConfigError, load_config, load_raw, validate
from committee.config.secrets import Secrets, mask


def _edit(project: Path, name: str, fn) -> None:  # type: ignore[no-untyped-def]
    p = project / "config" / name
    data = yaml.safe_load(p.read_text())
    fn(data)
    p.write_text(yaml.safe_dump(data))


def test_real_config_is_valid() -> None:
    cfg = load_config(Path(__file__).resolve().parents[1] / "config")
    assert cfg.risk_limits.buckets.core_pick.max_pct_total == 6.0
    assert cfg.risk_limits.position.kelly_fraction_cap == 0.5
    assert sum(s.target_pct for s in cfg.policy_portfolio.sleeves) == pytest.approx(100)


def test_latest_alias_rejected(project: Path) -> None:
    _edit(project, "models.yaml", lambda d: d["tiers"]["top"].update(model="claude-opus-latest"))
    with pytest.raises(ConfigError, match="latest"):
        load_config(project / "config")


def test_bear_must_differ_from_chair(project: Path) -> None:
    _edit(project, "models.yaml", lambda d: d["agents"].update(bear="top"))
    with pytest.raises(ConfigError, match="bear must use a different"):
        load_config(project / "config")


def test_missing_agent_tier(project: Path) -> None:
    _edit(project, "models.yaml", lambda d: d["agents"].pop("chair"))
    with pytest.raises(ConfigError, match="chair"):
        load_config(project / "config")


def test_policy_must_sum_to_100(project: Path) -> None:
    _edit(project, "policy_portfolio.yaml", lambda d: d["sleeves"][0].update(target_pct=40))
    with pytest.raises(ConfigError, match="sum to"):
        load_config(project / "config")


def test_prohibitions_required(project: Path) -> None:
    _edit(project, "risk_limits.yaml", lambda d: d.update(prohibited=["margin"]))
    with pytest.raises(ConfigError, match="prohibited"):
        load_config(project / "config")


def test_kelly_cap_never_above_half(project: Path) -> None:
    _edit(project, "risk_limits.yaml", lambda d: d["position"].update(kelly_fraction_cap=1.0))
    with pytest.raises(ConfigError, match="kelly_fraction_cap"):
        load_config(project / "config")


def test_unknown_key_rejected(project: Path) -> None:
    _edit(project, "risk_limits.yaml", lambda d: d.update(surprise=1))
    with pytest.raises(ConfigError, match=r"risk_limits\.yaml"):
        load_config(project / "config")


def test_satellite_target_within_range(project: Path) -> None:
    raw = load_raw(project / "config")
    raw["risk_limits"]["account"]["satellite_target_pct"] = 50
    with pytest.raises(ConfigError, match="satellite_target_pct"):
        validate(raw)


def test_missing_file(project: Path) -> None:
    (project / "config" / "scenarios.yaml").unlink()
    with pytest.raises(ConfigError, match="missing config file"):
        load_config(project / "config")


def test_mask() -> None:
    assert mask(None) == "(unset)"
    assert mask("abc") == "***"
    assert mask("sk-ant-1234567890") == "sk***90"


def test_secrets_precedence_and_live_gate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env = tmp_path / ".env"
    env.write_text(
        'FRED_KEY="from-env-file"\nALPACA_LIVE_KEY=live123456\nLIVE_TRADING_ENABLED=false\n'
    )
    monkeypatch.setenv("FRED_KEY", "from-process")
    s = Secrets(env_file=env, keychain=lambda n: "from-keychain" if n == "FINNHUB_KEY" else None)
    assert s.get("FINNHUB_KEY") == "from-keychain"
    assert s.get("FRED_KEY") == "from-env-file"
    assert s.get("ALPACA_LIVE_KEY") is None  # live key not loaded while flag is off
    assert "live123456" not in repr(s)
    env.write_text("ALPACA_LIVE_KEY=live123456\nLIVE_TRADING_ENABLED=true\n")
    s2 = Secrets(env_file=env, keychain=lambda n: None)
    assert s2.get("ALPACA_LIVE_KEY") == "live123456"
    with pytest.raises(KeyError):
        s2.get("NOT_A_SECRET")


def test_cli_config_check_masks_secrets(project: Path) -> None:
    (project / ".env").write_text("ANTHROPIC_API_KEY=sk-ant-supersecretvalue\n")
    res = CliRunner().invoke(app, ["config", "check", "--root", str(project)])
    assert res.exit_code == 0, res.output
    assert "config OK" in res.output
    assert "supersecretvalue" not in res.output
    assert "sk***ue" in res.output


def test_cli_config_check_fails_cleanly(project: Path) -> None:
    _edit(project, "models.yaml", lambda d: d["tiers"]["top"].update(model="x-latest"))
    res = CliRunner().invoke(app, ["config", "check", "--root", str(project)])
    assert res.exit_code == 2
