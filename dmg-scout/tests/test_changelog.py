"""Block 4C Item 3: docs/CHANGELOG.md and the /changelog page, both
generated from app.changelog.BLOCK_DEFS + real git history."""
import base64
import re

from app.changelog import BLOCK_DEFS, commits_for_block, generate_changelog, render_markdown
from app.db import get_session


class TestCommitsForBlock:
    def test_none_grep_returns_empty(self):
        assert commits_for_block(None) == []

    def test_matches_only_the_subject_line_not_the_body(self):
        """Regression: `git log --grep` anchors `^` to the start of ANY
        line in the full commit message (subject AND body), not just the
        subject -- confirmed the hard way, a Block 4B-prep commit whose
        BODY happened to contain a line starting "Block 4A ..." was
        wrongly pulled into the Block 4A list. commits_for_block must
        filter against the subject only."""
        block_3 = commits_for_block(r"^Block 3")
        block_4a = commits_for_block(r"^Block 4A")
        subjects_3 = {c["subject"] for c in block_3}
        subjects_4a = {c["subject"] for c in block_4a}
        assert subjects_3.isdisjoint(subjects_4a)

    def test_returns_real_commits_oldest_first(self):
        commits = commits_for_block(r"^Block 3")
        assert len(commits) >= 5
        dates = [c["date"] for c in commits]
        assert dates == sorted(dates)
        assert all(re.fullmatch(r"[0-9a-f]{7,}", c["hash"]) for c in commits)

    def test_every_block_4b_commit_actually_starts_with_block_4b(self):
        for c in commits_for_block(r"^Block 4B"):
            assert c["subject"].startswith("Block 4B"), c["subject"]


class TestGenerateChangelog:
    def test_every_block_def_is_present_once(self):
        blocks = generate_changelog()
        assert [b["id"] for b in blocks] == [b["id"] for b in BLOCK_DEFS]

    def test_blocks_1_and_2_have_no_commits(self):
        """Predate this repo's per-feature commit convention -- the real
        content lives in docs/BUILD-PLAN.md, not git history; see
        BLOCK_DEFS' own comments."""
        blocks = {b["id"]: b for b in generate_changelog()}
        assert blocks["1"]["commits"] == []
        assert blocks["2"]["commits"] == []

    def test_blocks_3_through_4b_have_real_commits(self):
        blocks = {b["id"]: b for b in generate_changelog()}
        for block_id in ("3", "4A", "4B"):
            assert len(blocks[block_id]["commits"]) > 0, block_id

    def test_4c_is_not_yet_included(self):
        """Block 4C Item 3's own instruction: "Backfill Blocks 1 through
        4B" -- 4C (still in progress in the same session that adds this
        module) is deliberately not listed yet."""
        assert "4C" not in {b["id"] for b in generate_changelog()}

    def test_no_summary_is_empty(self):
        for b in generate_changelog():
            assert b["summary"].strip()
            assert len(b["summary"]) > 100  # a real paragraph, not a placeholder


class TestRenderMarkdown:
    def test_contains_every_block_title(self):
        md = render_markdown(generate_changelog())
        for b in BLOCK_DEFS:
            assert b["title"] in md

    def test_links_a_real_commit_hash(self):
        md = render_markdown(generate_changelog())
        blocks = {b["id"]: b for b in generate_changelog()}
        assert blocks["3"]["commits"][0]["hash"] in md

    def test_no_shippable_commits_note_for_blocks_1_and_2(self):
        md = render_markdown(generate_changelog())
        assert md.count("no shippable commits") == 2


class TestChangelogPage:
    def test_requires_auth(self, db_session, monkeypatch):
        from fastapi.testclient import TestClient

        from app.web.main import app
        monkeypatch.setenv("DASHBOARD_PASSWORD", "testpw")
        app.dependency_overrides[get_session] = lambda: db_session
        client = TestClient(app)
        try:
            assert client.get("/changelog").status_code == 401
        finally:
            app.dependency_overrides.clear()

    def test_any_authenticated_user_can_read_it_not_operator_only(self, db_session, monkeypatch):
        """Unlike /settings/health (operator only), /changelog is written
        for any rep -- a non-operator user must get 200, not 403."""
        from fastapi.testclient import TestClient

        from app.web.main import app
        monkeypatch.setenv("DASHBOARD_PASSWORD_REP", "reppw")
        cfg_users = [{"username": "rep1", "password_env": "DASHBOARD_PASSWORD_REP", "role": "rep"}]
        from app.config import load_config

        real_cfg = load_config()

        class _Cfg:
            def __getattr__(self, name):
                return getattr(real_cfg, name)

            def get(self, key, default=None):
                if key == "dashboard.users":
                    return cfg_users
                if key == "dashboard.operator_usernames":
                    return []
                return real_cfg.get(key, default)

        monkeypatch.setattr("app.web.main.load_config", lambda: _Cfg())
        app.dependency_overrides[get_session] = lambda: db_session
        client = TestClient(app)
        rep_auth = {"Authorization": "Basic " + base64.b64encode(b"rep1:reppw").decode()}
        try:
            resp = client.get("/changelog", headers=rep_auth)
            assert resp.status_code == 200
            assert "Block 4B" in resp.text
        finally:
            app.dependency_overrides.clear()

    def test_shows_the_plain_language_summary_and_a_commit_hash(self, db_session, monkeypatch):
        from fastapi.testclient import TestClient

        from app.web.main import app
        monkeypatch.setenv("DASHBOARD_PASSWORD", "testpw")
        app.dependency_overrides[get_session] = lambda: db_session
        client = TestClient(app)
        auth = {"Authorization": "Basic " + base64.b64encode(b"andrew:testpw").decode()}
        try:
            resp = client.get("/changelog", headers=auth)
            assert resp.status_code == 200
            assert "Weekly Sales Intelligence Brief" in resp.text  # Block 4B's own summary text
            blocks = {b["id"]: b for b in generate_changelog()}
            assert blocks["4B"]["commits"][0]["hash"] in resp.text
        finally:
            app.dependency_overrides.clear()

    def test_linked_from_the_page_footer(self, db_session, monkeypatch):
        from fastapi.testclient import TestClient

        from app.web.main import app
        monkeypatch.setenv("DASHBOARD_PASSWORD", "testpw")
        app.dependency_overrides[get_session] = lambda: db_session
        client = TestClient(app)
        auth = {"Authorization": "Basic " + base64.b64encode(b"andrew:testpw").decode()}
        try:
            resp = client.get("/", headers=auth)
            assert 'href="/changelog"' in resp.text
        finally:
            app.dependency_overrides.clear()
