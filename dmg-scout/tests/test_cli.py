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
                "fetch_ebewe_benchmarks_cmd", "build_retrofit_buildings_cmd",
                "find_replacement_candidates_cmd", "match_contractors_cmd",
                "match_contractors_overdue_cmd"):
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


def test_match_contractors_steps_only_run_on_the_weekly_day(db_session, monkeypatch):
    """Same weekly gate as find_replacement_candidates_cmd, and for the
    same reason: both jobs join against retrofit_buildings, which is only
    refreshed on this day -- running them any other day would rank/count
    against last week's population."""
    calls = []
    _patch_stages(monkeypatch, calls)
    other_day = (utcnow().weekday() + 1) % 7
    monkeypatch.setattr(cli_mod, "RETROFIT_WEEKLY_WEEKDAY", other_day)

    result = CliRunner().invoke(cli_app, ["pipeline"])

    assert "match_contractors_cmd" not in calls
    assert "match_contractors_overdue_cmd" not in calls
    assert result.exit_code == 0


def test_match_contractors_steps_run_on_a_matching_weekly_day_after_the_rebuild(db_session, monkeypatch):
    calls = []
    _patch_stages(monkeypatch, calls)
    monkeypatch.setattr(cli_mod, "RETROFIT_WEEKLY_WEEKDAY", utcnow().weekday())

    result = CliRunner().invoke(cli_app, ["pipeline"])

    assert "match_contractors_cmd" in calls
    assert "match_contractors_overdue_cmd" in calls
    # Both run AFTER find_replacement_candidates_cmd -- they must see that
    # day's rebuilt retrofit_buildings, not the population from before it.
    assert calls.index("find_replacement_candidates_cmd") < calls.index("match_contractors_cmd")
    assert calls.index("match_contractors_cmd") < calls.index("match_contractors_overdue_cmd")
    assert result.exit_code == 0


# --- collision guards: Render's own schedule, the GitHub Actions backup   -
# --- trigger, and a human running `scout pipeline` by hand must never    -
# --- run two pipelines at once, or re-run a cycle that already succeeded -


def test_pipeline_refuses_to_start_when_one_is_already_running(db_session, monkeypatch):
    """The currently-running guard is never bypassable -- not even by
    --force -- because the risk is two processes sharing one daily LLM
    budget concurrently, not just wasted time."""
    from app.models import PipelineRun

    db_session.add(PipelineRun(status="running", started_at=utcnow(), heartbeat_at=utcnow()))
    db_session.commit()

    calls = []
    _patch_stages(monkeypatch, calls)

    result = CliRunner().invoke(cli_app, ["pipeline", "--force"])

    assert calls == [], "no stage should run while another pipeline_run is status=running"
    assert result.exit_code == 0


def test_pipeline_skips_a_second_run_within_the_recent_success_window(db_session, monkeypatch):
    """This is what makes the GitHub Actions backup trigger a true no-op on
    any day Render's own scheduler starts working again -- if a run already
    succeeded a few hours ago, a second trigger firing later the same day
    must not start a genuinely duplicate full pipeline."""
    from datetime import timedelta

    from app.models import PipelineRun

    db_session.add(PipelineRun(status="success", started_at=utcnow() - timedelta(hours=2),
                               finished_at=utcnow() - timedelta(hours=1, minutes=50)))
    db_session.commit()

    calls = []
    _patch_stages(monkeypatch, calls)

    result = CliRunner().invoke(cli_app, ["pipeline"])

    assert calls == []
    assert result.exit_code == 0


def test_pipeline_force_bypasses_only_the_recent_success_guard(db_session, monkeypatch):
    from datetime import timedelta

    from app.models import PipelineRun

    db_session.add(PipelineRun(status="success", started_at=utcnow() - timedelta(hours=2),
                               finished_at=utcnow() - timedelta(hours=1, minutes=50)))
    db_session.commit()

    calls = []
    _patch_stages(monkeypatch, calls)

    result = CliRunner().invoke(cli_app, ["pipeline", "--force"])

    assert "fetch" in calls, "--force must bypass the recent-success guard"
    assert result.exit_code == 0


def test_pipeline_runs_normally_when_last_success_is_old(db_session, monkeypatch):
    from datetime import timedelta

    from app.models import PipelineRun

    db_session.add(PipelineRun(status="success", started_at=utcnow() - timedelta(hours=30),
                               finished_at=utcnow() - timedelta(hours=29)))
    db_session.commit()

    calls = []
    _patch_stages(monkeypatch, calls)

    result = CliRunner().invoke(cli_app, ["pipeline"])

    assert "fetch" in calls, "a success older than RECENT_SUCCESS_SKIP_HOURS must not block a new run"
    assert result.exit_code == 0


def test_snapshot_metrics_cmd_runs_and_prints_a_count_per_metric(db_session):
    """Block 4A Item 4: `scout snapshot-metrics` on its own, not just as
    part of the full pipeline."""
    result = CliRunner().invoke(cli_app, ["snapshot-metrics"])
    assert result.exit_code == 0
    import json
    output = json.loads(result.output)
    assert "qualified_opportunities" in output
    assert output["qualified_opportunities"] == 1  # one dimensionless row: {} -> value
