"""Guards against the Alembic deploy-skew bug that failed the cron job on
2026-08-13 and again on 2026-08-17: running `alembic upgrade head` by hand
against production, ahead of committing the migration, desyncs the web and
cron services (they autodeploy independently against one shared database).
See app.db.refuse_remote_migration_without_override and
app.db.check_migration_state, and alembic/env.py."""
import pytest
from sqlalchemy import text

from app.db import (
    check_migration_state,
    get_engine,
    init_db,
    refuse_remote_migration_without_override,
)


class TestRefuseRemoteMigrationWithoutOverride:
    def test_blocks_a_remote_looking_host_by_default(self):
        with pytest.raises(RuntimeError, match="Refusing to run Alembic"):
            refuse_remote_migration_without_override(
                "postgresql://user:pw@dpg-abc123-a.oregon-postgres.render.com/db"
            )

    def test_error_message_names_the_safe_path(self):
        with pytest.raises(RuntimeError, match="commit the migration, push it"):
            refuse_remote_migration_without_override("postgresql://user:pw@some-remote-host/db")

    @pytest.mark.parametrize("host", ["localhost", "127.0.0.1", "::1", ""])
    def test_allows_local_hosts(self, host):
        refuse_remote_migration_without_override(f"postgresql://user:pw@{host}/db")

    def test_allows_sqlite_with_no_host(self):
        refuse_remote_migration_without_override("sqlite:///scout.db")

    def test_render_env_var_bypasses_the_guard(self, monkeypatch):
        monkeypatch.setenv("RENDER", "true")
        refuse_remote_migration_without_override("postgresql://user:pw@some-remote-host/db")

    def test_explicit_override_bypasses_the_guard(self, monkeypatch):
        monkeypatch.setenv("ALEMBIC_ALLOW_REMOTE", "1")
        refuse_remote_migration_without_override("postgresql://user:pw@some-remote-host/db")


class TestKnownAlembicRevisions:
    def test_does_not_depend_on_this_module_s_own_file_location(self, monkeypatch):
        """Regression: the first version of this check computed its repo
        root from Path(__file__).resolve().parent.parent, which resolves
        inside site-packages for the pip-installed `scout` console script
        (not /srv/dmg-scout) -- the exact bug app.config.DEFAULT_CONFIG's
        SCOUT_CONFIG override already exists to prevent for config.yaml.
        That version returned an empty revision set in production and
        failed EVERY deploy, not just a real mismatch (caught 2026-08-18
        redeploying the very fix for the alembic deploy-skew bug). Faking
        this module's __file__ must not change the result: only
        SCOUT_CONFIG's directory may."""
        import app.db as db_mod

        monkeypatch.setattr(db_mod, "__file__", "/somewhere/site-packages/app/db.py")
        revs = db_mod._known_alembic_revisions()
        assert "c2e9a4f61b7d" in revs
        assert len(revs) > 10

    def test_does_not_depend_on_process_cwd(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        import app.db as db_mod

        revs = db_mod._known_alembic_revisions()
        assert "c2e9a4f61b7d" in revs


class TestCheckMigrationState:
    def test_passes_when_no_alembic_version_table_exists(self, tmp_path, monkeypatch):
        import app.db as db_mod

        monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/fresh.db")
        monkeypatch.setattr(db_mod, "_engine", None)
        init_db()  # creates app tables, deliberately not alembic_version
        check_migration_state()  # must not raise

    def test_fails_loudly_with_the_revision_id_when_db_is_stamped_ahead(self, tmp_path, monkeypatch):
        import app.db as db_mod

        monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/stamped.db")
        monkeypatch.setattr(db_mod, "_engine", None)
        init_db()
        engine = get_engine()
        bogus_revision = "ffffffffffff_does_not_exist"
        with engine.begin() as conn:
            conn.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32) PRIMARY KEY)"))
            conn.execute(
                text("INSERT INTO alembic_version (version_num) VALUES (:rev)"),
                {"rev": bogus_revision},
            )
        with pytest.raises(SystemExit, match=bogus_revision):
            check_migration_state()

    def test_passes_when_db_is_stamped_with_a_real_known_revision(self, tmp_path, monkeypatch):
        import app.db as db_mod

        monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/known.db")
        monkeypatch.setattr(db_mod, "_engine", None)
        init_db()
        engine = get_engine()
        real_revision = sorted(db_mod._known_alembic_revisions())[0]
        with engine.begin() as conn:
            conn.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32) PRIMARY KEY)"))
            conn.execute(
                text("INSERT INTO alembic_version (version_num) VALUES (:rev)"),
                {"rev": real_revision},
            )
        check_migration_state()  # must not raise
