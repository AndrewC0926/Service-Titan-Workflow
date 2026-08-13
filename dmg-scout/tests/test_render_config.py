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


def test_cron_script_does_not_rely_on_inherited_cwd():
    """Regression test for the second cron bug (2026-08-13, found by
    actually triggering the job via Render's API -- a local run can't
    surface this): load_config() opens config.yaml by a relative path, and
    the process's working directory when Render's Jobs API starts it is
    NOT the image's WORKDIR the way a normal scheduled dockerCommand run
    is -- confirmed via a real traceback, FileNotFoundError against
    /usr/local/lib/python3.12/site-packages/config.yaml. An explicit `cd`
    to the known deployment path makes this independent of whatever CWD
    the invoking process happens to start with."""
    script = (REPO_ROOT / _cron_service()["dockerCommand"]).read_text()
    cd_at = script.find("cd /srv/dmg-scout")
    assert cd_at != -1, "script must cd to an absolute path before running anything relative-path-dependent"
    # rfind, not index/find: the header comment explaining this fix also
    # mentions "alembic upgrade head" in prose, before the real command.
    migrate_at = script.rfind("alembic upgrade head")
    assert cd_at < migrate_at, "the cd must happen before the actual alembic/scout invocation, not after"


def test_web_service_command_is_untouched():
    """This fix is scoped to the cron only -- the web service's own CMD
    (Dockerfile default, works today) must not have been touched."""
    web_services = [s for s in _render_config()["services"] if s.get("type") == "web"]
    assert len(web_services) == 1
    assert "dockerCommand" not in web_services[0]
