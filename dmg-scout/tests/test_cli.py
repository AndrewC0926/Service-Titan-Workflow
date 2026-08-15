"""`scout pipeline`'s own stage wiring -- not the individual stages
themselves (each has its own test module), but the orchestration: does a
failing retrofit step still let the rest of the run finish, and does the
weekly gate around find-replacement-candidates actually gate."""
from typer.testing import CliRunner

import app.cli as cli_mod
from app.cli import app as cli_app
from app.models import utcnow


def _recorder(calls, name):
    def _inner(*args, **kwargs):
        calls.append(name)
    return _inner


def _patch_stages(monkeypatch, calls, *, boom: str | None = None):
    """Replace every stage app.cli.pipeline() calls with a call-recording
    stand-in, so the run touches no network/LLM/real retrofit code. `boom`
    names one stage to raise instead of recording success."""
    for name in ("fetch", "triage", "extract", "grounding", "resolve", "score", "notify",
                "build_retrofit_buildings_cmd", "find_replacement_candidates_cmd"):
        if name == boom:
            def _raiser(*a, _name=name, **k):
                calls.append(_name)
                raise RuntimeError(f"{_name} exploded")
            monkeypatch.setattr(cli_mod, name, _raiser)
        else:
            monkeypatch.setattr(cli_mod, name, _recorder(calls, name))
    monkeypatch.setattr("app.ops.ping_healthcheck", lambda **k: True)


def test_a_failing_retrofit_step_does_not_abort_the_other_retrofit_step(db_session, monkeypatch):
    """build-retrofit-buildings blowing up (e.g. ArcGIS down) must not stop
    find-replacement-candidates from still getting its turn -- the whole
    point of running each stage in its own try/except."""
    calls = []
    _patch_stages(monkeypatch, calls, boom="build_retrofit_buildings_cmd")
    monkeypatch.setattr(cli_mod, "RETROFIT_WEEKLY_WEEKDAY", utcnow().weekday())

    result = CliRunner().invoke(cli_app, ["pipeline"])

    assert "build_retrofit_buildings_cmd" in calls
    assert "find_replacement_candidates_cmd" in calls
    # every earlier stage still ran too -- a later failure can't retroactively
    # skip anything that already happened
    assert calls[:7] == ["fetch", "triage", "extract", "grounding", "resolve", "score", "notify"]
    # a failed stage still marks the overall run failed -- same invariant
    # every other stage already gets, not a special exemption for retrofit
    assert result.exit_code == 1


def test_find_replacement_candidates_only_runs_on_its_weekly_day(db_session, monkeypatch):
    calls = []
    _patch_stages(monkeypatch, calls)
    other_day = (utcnow().weekday() + 1) % 7
    monkeypatch.setattr(cli_mod, "RETROFIT_WEEKLY_WEEKDAY", other_day)

    result = CliRunner().invoke(cli_app, ["pipeline"])

    assert "build_retrofit_buildings_cmd" in calls  # daily -- always runs
    assert "find_replacement_candidates_cmd" not in calls  # not today
    assert result.exit_code == 0


def test_find_replacement_candidates_runs_on_a_matching_weekly_day(db_session, monkeypatch):
    calls = []
    _patch_stages(monkeypatch, calls)
    monkeypatch.setattr(cli_mod, "RETROFIT_WEEKLY_WEEKDAY", utcnow().weekday())

    result = CliRunner().invoke(cli_app, ["pipeline"])

    assert "find_replacement_candidates_cmd" in calls
    assert result.exit_code == 0
