"""app/precall.py: the cache never touches Postgres, brief_sections() parses
the model's four-header format (and degrades gracefully when it doesn't), and
the orchestrator short-circuits on a cache hit rather than paying for another
LLM call. The LLM call itself is never exercised here -- see the Vernon brief
generated manually for that; these tests are about the surrounding plumbing.
"""
from __future__ import annotations

import pytest

from app import precall
from app.models import Category, OpscProject, Project, Stage, Window


def _project(id_=None, name="Test DC"):
    return Project(id=id_, name=name, category=Category.data_center,
                   stage=Stage.permitting, window=Window.IN_BOD, status="active")


def _opsc(id_=None, district="Test USD", school_name="Test Elementary", status="Application Received"):
    return OpscProject(id=id_, county="Los Angeles", district=district, school_name=school_name,
                       program="Modernization", application_number="26/12345-00-001",
                       status=status, in_territory=True, source_url="https://data.ca.gov/test")


@pytest.fixture(autouse=True)
def _cache_dir(tmp_path, monkeypatch):
    """Every test gets its own empty cache dir -- never the real
    data/precall_cache/, and never touches Postgres either way (see module
    docstring: the cache is local disk, on purpose)."""
    monkeypatch.setattr(precall, "CACHE_DIR", tmp_path / "precall_cache")


# ---- cache -----------------------------------------------------------

def test_cache_round_trip():
    entry = {"entity_type": "project", "entity_id": 1, "name": "X", "text": "hello",
             "cost_usd": 0.12}
    precall._write_cache("project", 1, entry)
    assert precall._read_cache("project", 1) == entry


def test_cache_miss_returns_none():
    assert precall._read_cache("project", 999) is None


def test_cache_corrupt_file_returns_none_not_raises():
    precall.CACHE_DIR.mkdir(parents=True)
    (precall.CACHE_DIR / "project_1.json").write_text("{not valid json")
    assert precall._read_cache("project", 1) is None


def test_precall_cost_report_sums_across_cached_entries():
    precall._write_cache("project", 1, {"cost_usd": 0.10})
    precall._write_cache("contractor", 2, {"cost_usd": 0.05})
    report = precall.precall_cost_report()
    assert report["n_briefs"] == 2
    assert report["total_cost_usd"] == pytest.approx(0.15)


def test_precall_cost_report_empty_when_nothing_cached():
    report = precall.precall_cost_report()
    assert report == {"n_briefs": 0, "total_cost_usd": 0.0, "entries": []}


def test_precall_cost_report_skips_corrupt_files():
    precall._write_cache("project", 1, {"cost_usd": 0.10})
    (precall.CACHE_DIR / "project_2.json").write_text("not json")
    report = precall.precall_cost_report()
    assert report["n_briefs"] == 1
    assert report["total_cost_usd"] == pytest.approx(0.10)


# ---- brief_sections() ---------------------------------------------------

def test_brief_sections_splits_all_four_headers():
    text = (
        "BOTTOM LINE\nCall them now.\n\n"
        "WHO TO CALL\nJane Doe, 555-1234.\n\n"
        "SCOUT DATA\n- Stage: permitting\n- Score: 1.2\n\n"
        "FROM TODAY'S WEB RESEARCH\nNothing found (unknown)."
    )
    sections = precall.brief_sections(text)
    headers = [s["header"] for s in sections]
    assert headers == ["BOTTOM LINE", "WHO TO CALL", "SCOUT DATA", "FROM TODAY'S WEB RESEARCH"]
    assert sections[0]["blocks"] == [{"type": "para", "text": "Call them now."}]
    assert sections[2]["blocks"] == [{"type": "bullets", "lines": ["Stage: permitting", "Score: 1.2"]}]


def test_brief_sections_headers_case_insensitive_and_colon_optional():
    text = "Bottom Line:\nShort reason.\n\nWho To Call\nJane Doe."
    sections = precall.brief_sections(text)
    assert [s["header"] for s in sections] == ["BOTTOM LINE", "WHO TO CALL"]


def test_brief_sections_falls_back_to_single_section_without_headers():
    text = "Just a plain paragraph with no section headers at all."
    sections = precall.brief_sections(text)
    assert len(sections) == 1
    assert sections[0]["header"] is None
    assert sections[0]["blocks"] == [{"type": "para", "text": text}]


def test_brief_sections_preserves_leading_text_before_first_header():
    text = "Some stray preamble.\n\nBOTTOM LINE\nThe actual point."
    sections = precall.brief_sections(text)
    assert sections[0]["header"] is None
    assert sections[1]["header"] == "BOTTOM LINE"


# ---- orchestrator ---------------------------------------------------------

def test_pre_call_brief_unknown_entity_type_raises(db_session):
    with pytest.raises(ValueError, match="entity_type"):
        precall.pre_call_brief(db_session, "firm", 1)


def test_pre_call_brief_missing_project_raises(db_session):
    with pytest.raises(ValueError, match="no project"):
        precall.pre_call_brief(db_session, "project", 999999)


def test_pre_call_brief_missing_contractor_raises(db_session):
    with pytest.raises(ValueError, match="no contractor"):
        precall.pre_call_brief(db_session, "contractor", 999999)


def test_pre_call_brief_missing_opsc_project_raises(db_session):
    with pytest.raises(ValueError, match="no opsc_project"):
        precall.pre_call_brief(db_session, "opsc_project", 999999)


def test_opsc_ingredients_and_serialize(db_session, cfg):
    r = _opsc(status="Funds Released")
    db_session.add(r)
    db_session.commit()
    db_session.refresh(r)

    ing = precall._opsc_ingredients(db_session, r.id)
    assert ing["entity_type"] == "opsc_project"
    assert "Test USD" in ing["name"]

    payload = precall._serialize_opsc(ing)
    assert payload["district"] == "Test USD"
    assert payload["school_name"] == "Test Elementary"
    assert payload["call_target"]["label"]  # bidding_contractors, since Funds Released
    assert payload["contact_ladder"] == (
        "not applicable -- OPSC rows have no named human contact; the call "
        "target above is the district or contractor pool, not a person")


def test_pre_call_brief_opsc_project_end_to_end(db_session, monkeypatch):
    r = _opsc()
    db_session.add(r)
    db_session.commit()
    db_session.refresh(r)

    monkeypatch.setattr(precall, "_call_llm", lambda cfg, sys, user, **k: {
        "text": "BOTTOM LINE\nschool brief", "model": "m", "input_tokens": 1, "output_tokens": 1,
        "web_searches": 0, "token_cost_usd": 0.0, "search_cost_usd": 0.0, "cost_usd": 0.0,
    })

    entry = precall.pre_call_brief(db_session, "opsc_project", r.id)
    assert entry["from_cache"] is False
    assert entry["text"] == "BOTTOM LINE\nschool brief"
    assert entry["name"] == "Test USD -- Test Elementary"


def test_pre_call_brief_cache_hit_never_calls_llm(db_session, monkeypatch):
    """The whole point of the cache: opening a brief twice must not spend
    twice. Proven here by making a second LLM call raise if it happens."""
    p = _project()
    db_session.add(p)
    db_session.commit()
    db_session.refresh(p)

    def _boom(*a, **k):
        raise AssertionError("LLM should not be called on a cache hit")

    precall._write_cache("project", p.id, {
        "entity_type": "project", "entity_id": p.id, "name": p.name,
        "generated_at": "2026-01-01T00:00:00+00:00", "generated_at_display": "2026-01-01 00:00Z",
        "text": "BOTTOM LINE\ncached brief", "model": "claude-sonnet-5",
        "input_tokens": 10, "output_tokens": 5, "web_searches": 1,
        "token_cost_usd": 0.01, "search_cost_usd": 0.01, "cost_usd": 0.02,
    })
    monkeypatch.setattr(precall, "_call_llm", _boom)

    entry = precall.pre_call_brief(db_session, "project", p.id)
    assert entry["from_cache"] is True
    assert entry["text"] == "BOTTOM LINE\ncached brief"


def test_pre_call_brief_force_refresh_bypasses_cache(db_session, monkeypatch):
    p = _project()
    db_session.add(p)
    db_session.commit()
    db_session.refresh(p)

    precall._write_cache("project", p.id, {
        "entity_type": "project", "entity_id": p.id, "name": p.name,
        "text": "stale", "cost_usd": 0.02,
    })
    monkeypatch.setattr(precall, "_project_ingredients",
                        lambda session, pid: {"entity_type": "project", "entity_id": pid, "name": p.name})
    monkeypatch.setattr(precall, "_build_prompt", lambda entity_type, ing: ("sys", "user"))
    monkeypatch.setattr(precall, "_call_llm", lambda cfg, sys, user, **k: {
        "text": "BOTTOM LINE\nfresh brief", "model": "claude-sonnet-5",
        "input_tokens": 1, "output_tokens": 1, "web_searches": 0,
        "token_cost_usd": 0.001, "search_cost_usd": 0.0, "cost_usd": 0.001,
    })

    entry = precall.pre_call_brief(db_session, "project", p.id, force_refresh=True)
    assert entry["from_cache"] is False
    assert entry["text"] == "BOTTOM LINE\nfresh brief"
    # And the cache file itself was overwritten, not just the return value:
    assert precall._read_cache("project", p.id)["text"] == "BOTTOM LINE\nfresh brief"


def test_pre_call_brief_never_writes_to_the_db_session(db_session, monkeypatch):
    """Hard constraint from the feature spec: this must never write to
    Postgres. db_session here is a real (file-backed SQLite) session: if
    anything in the orchestrator called session.add/commit on pipeline
    tables, session.new/session.dirty would show it."""
    p = _project()
    db_session.add(p)
    db_session.commit()
    db_session.refresh(p)

    monkeypatch.setattr(precall, "_project_ingredients",
                        lambda session, pid: {"entity_type": "project", "entity_id": pid, "name": p.name})
    monkeypatch.setattr(precall, "_build_prompt", lambda entity_type, ing: ("sys", "user"))
    monkeypatch.setattr(precall, "_call_llm", lambda cfg, sys, user, **k: {
        "text": "BOTTOM LINE\nfresh", "model": "m", "input_tokens": 1, "output_tokens": 1,
        "web_searches": 0, "token_cost_usd": 0.0, "search_cost_usd": 0.0, "cost_usd": 0.0,
    })

    precall.pre_call_brief(db_session, "project", p.id, force_refresh=True)
    assert not db_session.new
    assert not db_session.dirty


# ---- _call_llm: token_spend recording (2026-08-19) -----------------------
#
# Until now this stage was entirely invisible to token_spend -- see the
# module docstring for why that made a per-stage budget cap here
# meaningless (it would be enforced against a number that never moved).
# _call_llm's own API call was never exercised by this file's other tests
# (they all mock _call_llm itself, per its module docstring) -- these
# mock ONE level deeper, at the Anthropic client, specifically to prove
# the real function now records real spend.

class _FakeMessages:
    def __init__(self, n_searches=1):
        self.n_searches = n_searches

    def create(self, **kwargs):
        from types import SimpleNamespace
        blocks = [SimpleNamespace(type="server_tool_use", name="web_search")
                 for _ in range(self.n_searches)]
        blocks.append(SimpleNamespace(type="text", text="BOTTOM LINE\nTest brief text."))
        return SimpleNamespace(content=blocks,
                               usage=SimpleNamespace(input_tokens=1000, output_tokens=200))


class _FakeAnthropicClient:
    def __init__(self, n_searches=1):
        self.messages = _FakeMessages(n_searches)


def test_call_llm_records_token_spend_including_search_cost(db_session, cfg, monkeypatch):
    from sqlmodel import select
    from app.models import TokenSpend

    monkeypatch.setattr(precall, "_client", lambda: _FakeAnthropicClient(n_searches=2))
    result = precall._call_llm(cfg, "system", "user content", stage="precall_project")

    assert result["web_searches"] == 2
    rows = db_session.exec(select(TokenSpend).where(TokenSpend.stage == "precall_project")).all()
    assert len(rows) == 1
    # cost_usd on the row must be the COMBINED (token + search) cost, folded into
    # this one row -- not just the token portion price() alone would compute.
    assert rows[0].cost_usd == pytest.approx(result["cost_usd"], abs=1e-6)
    assert rows[0].cost_usd > result["token_cost_usd"]  # search cost genuinely added something


def test_call_llm_is_gated_by_its_own_stage_daily_cap(db_session, cfg, monkeypatch):
    """The cap this whole change exists to make possible: a per-stage daily
    ceiling that's actually enforceable now that precall writes to
    token_spend at all."""
    from app.spend import BudgetExceeded, record

    monkeypatch.setitem(cfg.data["llm"], "stage_daily_budget_usd", {"precall_project": 0.01})
    record("precall_project", "claude-sonnet-5", 100_000, 0)  # already well past the cap

    called = {"n": 0}
    def _client_should_not_be_called():
        called["n"] += 1
        return _FakeAnthropicClient()
    monkeypatch.setattr(precall, "_client", _client_should_not_be_called)

    with pytest.raises(BudgetExceeded, match="daily budget for stage 'precall_project' exhausted"):
        precall._call_llm(cfg, "system", "user content", stage="precall_project")
    assert called["n"] == 0  # check_budget raised before the API was ever touched
