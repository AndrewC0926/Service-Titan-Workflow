"""render.yaml / Dockerfile composition: the check that would have caught
the cron's actual production failure before it shipped.

Every cron run failed with `sh: 1: alembic upgrade head && scout pipeline:
not found` -- `sh` treating the entire multi-word, quoted dockerCommand
string as one opaque token, because whatever Render's own (undocumented,
unavailable-here) composition does to a multi-word `dockerCommand` value did
not preserve `sh -c "script"`'s argument boundary intact. That is not
something a unit test can reproduce without Render's own tokenizer, but the
PROPERTY that made it possible -- a dockerCommand value with whitespace/
quoting for something else to mis-split -- is fully checkable here, and so
is "does the script this now points at actually parse as a shell script",
which is the assertion that actually matters."""
import os
import subprocess
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).parent.parent


def _render_config() -> dict:
    return yaml.safe_load((REPO_ROOT / "render.yaml").read_text())


def _cron_service() -> dict:
    services = _render_config()["services"]
    crons = [s for s in services if s.get("type") == "cron"]
    assert len(crons) == 1, "expected exactly one cron service in render.yaml"
    return crons[0]


def test_cron_docker_command_is_a_single_token():
    """The property that actually matters: a dockerCommand with no
    whitespace cannot be split apart differently than intended by ANY
    tokenizer, because there's nothing in it to split on. This is what
    directly would have caught the original bug -- the old value,
    `sh -c "alembic upgrade head && scout pipeline"`, fails this
    immediately."""
    cmd = _cron_service()["dockerCommand"]
    assert " " not in cmd, (
        f"dockerCommand {cmd!r} contains whitespace -- Render's composition of a multi-word "
        f"dockerCommand string is not guaranteed to preserve shell semantics (this is exactly "
        f"how the cron broke: 'sh -c \"alembic upgrade head && scout pipeline\"' silently lost "
        f"its -c/script pairing). Point dockerCommand at a single script instead."
    )
    assert '"' not in cmd and "&&" not in cmd


def test_cron_docker_command_points_at_an_executable_script_in_the_repo():
    cmd = _cron_service()["dockerCommand"]
    script = REPO_ROOT / cmd
    assert script.is_file(), f"dockerCommand {cmd!r} does not resolve to a file in the repo"
    assert os.access(script, os.X_OK), f"{cmd} is not executable (git's executable bit not set?)"


def test_cron_script_actually_parses_as_a_shell_script():
    """The assertion that actually matters, and the one the original bug
    would have failed: `bash -n` parses the script without executing it. A
    script that resolves to a single not-found argv element (the original
    failure mode, one level up) never gets this far at all; this checks the
    thing Render ultimately execs is valid shell, not just present."""
    cmd = _cron_service()["dockerCommand"]
    script = REPO_ROOT / cmd
    result = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True)
    assert result.returncode == 0, f"scripts/run-pipeline.sh is not valid bash:\n{result.stderr}"


def test_cron_script_runs_migration_before_pipeline_and_fails_loud():
    script = (REPO_ROOT / _cron_service()["dockerCommand"]).read_text()
    assert "set -euo pipefail" in script, (
        "without this, a failed migration would not stop the pipeline from running against an "
        "un-migrated schema"
    )
    migrate_at = script.index("alembic upgrade head")
    pipeline_at = script.index("scout pipeline")
    assert migrate_at < pipeline_at, "migration must run before the pipeline, not after"


def test_cron_script_checks_migration_state_before_running_alembic():
    """2026-08-17: the cron job died 20 seconds into a scheduled run because
    the database was already stamped with a revision this deployment's
    `alembic upgrade head` had never heard of -- discovered mid-pipeline,
    once a day, by job failure. `scout check-migrations` (app.db.
    check_migration_state) checks the same fact explicitly, by revision ID,
    before alembic even runs."""
    lines = [
        line for line in (REPO_ROOT / _cron_service()["dockerCommand"]).read_text().splitlines()
        if not line.strip().startswith("#")
    ]
    executable = "\n".join(lines)
    assert "scout check-migrations" in executable
    check_at = executable.index("scout check-migrations")
    migrate_at = executable.index("alembic upgrade head")
    assert check_at < migrate_at, "migration state must be checked before alembic upgrade head runs"


def test_web_dockerfile_checks_migration_state_before_running_alembic():
    dockerfile = (REPO_ROOT / "Dockerfile").read_text()
    cmd_line = next(line for line in dockerfile.splitlines() if line.startswith("CMD"))
    assert "scout check-migrations" in cmd_line
    assert cmd_line.index("scout check-migrations") < cmd_line.index("alembic upgrade head")


def test_dockerfile_sets_scout_config_for_the_installed_console_script():
    """Regression test for the second cron bug (2026-08-13, found by
    actually triggering the job via Render's API -- a local editable
    install can't surface this, see app/config.py's DEFAULT_CONFIG): `pip
    install .` (no -e) copies app/ into site-packages as a separate copy,
    so app.config.__file__-relative path resolution inside `scout` (the
    installed console-script entry point) always lands in site-packages,
    not /srv/dmg-scout -- confirmed via a real production traceback,
    FileNotFoundError against /usr/local/lib/python3.12/site-packages/
    config.yaml. A `cd` in the cron script does nothing for this (verified
    directly: a `pwd` job confirmed the cwd was already correct) since the
    bug was never about cwd. SCOUT_CONFIG is load_config()'s own documented
    override -- setting it once in the Dockerfile fixes every entry point
    in the image, not just the cron's."""
    dockerfile = (REPO_ROOT / "Dockerfile").read_text()
    assert "ENV SCOUT_CONFIG=/srv/dmg-scout/config.yaml" in dockerfile, (
        "app.config.DEFAULT_CONFIG resolves from the installed package's own file location, which "
        "is not /srv/dmg-scout once `pip install .` (no -e) has copied app/ into site-packages -- "
        "SCOUT_CONFIG must be set explicitly, the same fix that was actually verified against a real "
        "Render Jobs API-triggered run"
    )


def test_web_service_command_is_untouched():
    """This fix is scoped to the cron only -- the web service's own CMD
    (Dockerfile default, works today) must not have been touched."""
    web_services = [s for s in _render_config()["services"] if s.get("type") == "web"]
    assert len(web_services) == 1
    assert "dockerCommand" not in web_services[0]
