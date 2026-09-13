"""`scout pipeline`'s own stage wiring -- not the individual stages
themselves (each has its own test module), but the orchestration: does a
failing retrofit step still let the rest of the run finish, and does the
weekly gate around find-replacement-candidates actually gate."""
import json

from typer.testing import CliRunner

import app.cli as cli_mod
from app.cli import app as cli_app
from app.models import ProductLine, utcnow


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


def test_no_duplicate_command_names_registered():
    """Block 4B-prep-3 Item 6 finding: a second @app.command("match-
    contractors") (Block 4B-prep-2 Item 1's Contact-to-Contractor match)
    silently shadowed the pre-existing "match-contractors" command (the
    geocoded-contractor nearby-replacement-candidate-count precompute) --
    Python module-level function redefinition rebinds the name, so
    app.cli.match_contractors_cmd resolved to the wrong one everywhere,
    including inside pipeline_cmd's own weekly-step dispatch, which calls
    it as step(radius_miles=None) -- a real TypeError against the
    zero-argument function that name actually pointed to, confirmed by
    calling it directly. This guards against any future accidental
    duplicate command name doing the same thing silently."""
    from typer.main import get_command_name

    names = [c.name or get_command_name(c.callback.__name__) for c in cli_app.registered_commands]
    duplicates = {n for n in names if names.count(n) > 1}
    assert duplicates == set(), f"duplicate @app.command name(s): {duplicates}"


def test_match_contractors_cmd_is_the_nearby_count_precompute_not_the_contact_match():
    """The specific collision test_no_duplicate_command_names_registered
    guards against generically -- this pins down which function the name
    must resolve to."""
    import inspect

    assert "radius_miles" in inspect.signature(cli_mod.match_contractors_cmd).parameters
    assert cli_mod.match_contractors_cmd is not cli_mod.match_contacts_to_contractors_cmd


class TestPromoteTopSignalsCmd:
    def test_promotes_up_to_top_n_all_four_pass_signals(self, db_session):
        from datetime import datetime

        from app.models import Contact, Contractor, RetrofitBuilding

        b = RetrofitBuilding(apn="cli-1", population="recently_active", equipment_type="split_dx",
                            latitude=34.0, longitude=-118.0, latest_install_year=2010,
                            service_life_status="overdue")
        contractor = Contractor(license_no="CLI1", business_name="CLI Test Mechanical",
                                latitude=34.01, longitude=-118.01)
        db_session.add(b)
        db_session.add(contractor)
        db_session.add(ProductLine(name="LG", name_norm="lg", category="vrf_split",
                                   building_role="cooling_generation"))
        db_session.flush()
        db_session.add(Contact(name="CLI Contact", phone="555-0001", reach_status="confirmed",
                               contractor_id=contractor.id))
        db_session.commit()

        result = CliRunner().invoke(cli_app, ["promote-top-signals", "--owner-user", "andrew"])
        assert result.exit_code == 0
        report = json.loads(result.output)
        assert report["pass_all_four"] == 1
        assert len(report["promoted"]) == 1
        assert report["promoted"][0]["source"] == "retrofit_building"

    def test_respects_top_n(self, db_session):
        from app.models import Contact, Contractor, RetrofitBuilding

        for i in range(3):
            b = RetrofitBuilding(apn=f"cli-top-{i}", population="recently_active", equipment_type="split_dx",
                                latitude=34.0 + i * 0.01, longitude=-118.0, latest_install_year=2010,
                                service_life_status="overdue")
            contractor = Contractor(license_no=f"CLITOP{i}", business_name=f"CLI Top Mechanical {i}",
                                    latitude=34.0 + i * 0.01, longitude=-118.01)
            db_session.add(b)
            db_session.add(contractor)
            db_session.flush()
            db_session.add(Contact(name=f"Contact {i}", phone="555-0002", reach_status="confirmed",
                                   contractor_id=contractor.id))
        db_session.add(ProductLine(name="LG", name_norm="lg", category="vrf_split",
                                   building_role="cooling_generation"))
        db_session.commit()

        result = CliRunner().invoke(cli_app, ["promote-top-signals", "--top-n", "2", "--owner-user", "andrew"])
        assert result.exit_code == 0
        report = json.loads(result.output)
        assert report["pass_all_four"] == 3
        assert len(report["promoted"]) == 2

    def test_requires_owner_user(self, db_session):
        result = CliRunner().invoke(cli_app, ["promote-top-signals"])
        assert result.exit_code != 0
