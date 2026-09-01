"""Gate 4: budget kill switch, spend math, http archive, doctor, dead man's switch."""
import httpx
import pytest
import respx
from sqlmodel import select

import app.spend as spend_mod
from app.models import HttpLog, TokenSpend
from app.spend import BudgetExceeded, budget_status, check_budget, price, record


def test_price_math(cfg):
    # Haiku: 1M in @ $1 + 100k out @ $5 = $1.50
    assert abs(price(cfg, "claude-haiku-4-5-20251001", 1_000_000, 100_000) - 1.5) < 1e-9
    # Sonnet: 100k in @ $3 + 10k out @ $15 = $0.45
    assert abs(price(cfg, "claude-sonnet-4-6", 100_000, 10_000) - 0.45) < 1e-9


def test_record_and_budget_flow(db_session, cfg):
    record("triage", "claude-haiku-4-5-20251001", 500_000, 20_000)
    rows = db_session.exec(select(TokenSpend)).all()
    assert len(rows) == 1 and rows[0].stage == "triage"
    st = budget_status()
    assert st["today_usd"] > 0 and not st["exhausted"]
    check_budget()  # under budget: no raise


def _input_tokens_for(cfg, dollars: float, model: str = "claude-sonnet-4-6") -> int:
    """Input tokens that cost roughly `dollars` on `model`.

    Derived from config rather than hardcoded: these tests used fixed amounts tied
    to a $15 cap and broke the moment the cap moved to $40, which tested the
    constant instead of the kill switch.
    """
    per_mtok = cfg.get("llm.prices", {}).get(model, {}).get("in", 3.0)
    return int(dollars / per_mtok * 1_000_000)


def test_budget_kill_switch(db_session, cfg):
    budget = cfg.get("llm.daily_budget_usd", 15.0)
    record("extract", "claude-sonnet-4-6", _input_tokens_for(cfg, budget * 1.2), 0)
    with pytest.raises(BudgetExceeded):
        check_budget()
    assert budget_status()["exhausted"]


def test_manual_kill_switch(db_session, monkeypatch):
    monkeypatch.setenv("SCOUT_LLM_DISABLED", "1")
    with pytest.raises(BudgetExceeded, match="manual kill switch"):
        check_budget()


def test_warn_threshold(db_session, cfg, caplog):
    budget = cfg.get("llm.daily_budget_usd", 15.0)
    warn_at = cfg.get("llm.budget_warn_fraction", 0.8)
    # Between the warn threshold and the cap, wherever those currently sit.
    spend = budget * (warn_at + (1.0 - warn_at) / 2)
    record("extract", "claude-sonnet-4-6", _input_tokens_for(cfg, spend), 0)
    with caplog.at_level("WARNING"):
        check_budget()
    assert any("daily budget" in r.message for r in caplog.records)
    assert budget_status()["warn"] and not budget_status()["exhausted"]


@respx.mock
def test_http_archive_records_requests(db_session, cfg, monkeypatch):
    from app.http import PoliteClient
    from app.pipeline.fetch import run_fetch
    respx.get(url__regex=r".*").mock(return_value=httpx.Response(503))
    orig = PoliteClient.__init__

    def fast_init(self, *a, **k):
        k.update(interval=0, max_retries=1, respect_robots=False)
        orig(self, *a, **k)
    monkeypatch.setattr(PoliteClient, "__init__", fast_init)

    run_fetch(db_session, cfg, only_source="rss")
    logs = db_session.exec(select(HttpLog)).all()
    assert logs, "every fetch request must be archived"
    assert all(l.status == 503 and not l.ok for l in logs)
    assert logs[0].source_run_id is not None


def test_dead_mans_switch_disarmed_without_env(monkeypatch, caplog):
    from app.ops import ping_healthcheck
    monkeypatch.delenv("HEALTHCHECK_URL", raising=False)
    with caplog.at_level("WARNING"):
        assert ping_healthcheck() is False
    assert any("DISARMED" in r.message for r in caplog.records)


@respx.mock
def test_dead_mans_switch_pings(monkeypatch):
    from app.ops import ping_healthcheck
    monkeypatch.setenv("HEALTHCHECK_URL", "https://hc-ping.com/abc123")
    ok_route = respx.get("https://hc-ping.com/abc123").mock(return_value=httpx.Response(200))
    fail_route = respx.get("https://hc-ping.com/abc123/fail").mock(return_value=httpx.Response(200))
    assert ping_healthcheck(success=True)
    assert ok_route.called
    assert ping_healthcheck(success=False)
    assert fail_route.called


@respx.mock
def test_dead_mans_switch_strips_a_trailing_newline_in_the_env_var(monkeypatch):
    """Regression test for the real production failure (2026-08-13):
    HEALTHCHECK_URL was set with a trailing '\\n' (a pasted-in artifact),
    which made httpx raise InvalidURL. ping_healthcheck() sits before
    finish_pipeline_run() in app.cli:pipeline, so that uncaught exception
    crashed the process after a fully successful run and left its
    pipeline_run row stuck at status="running" forever -- the same
    stuck-row failure the heartbeat mechanism exists to catch, but
    self-inflicted by this function rather than an external kill."""
    from app.ops import ping_healthcheck
    monkeypatch.setenv("HEALTHCHECK_URL", "https://hc-ping.com/abc123\n")
    ok_route = respx.get("https://hc-ping.com/abc123").mock(return_value=httpx.Response(200))
    assert ping_healthcheck(success=True) is True
    assert ok_route.called


def test_dead_mans_switch_never_raises_even_on_a_url_httpx_cannot_parse(monkeypatch, caplog):
    """Belt-and-suspenders alongside the .strip() fix: this is a best-effort
    dead man's switch ping, same discipline as app.pipeline_health and
    app.access_log -- a bad HEALTHCHECK_URL must degrade to "switch didn't
    fire" (logged, returns False), never to "pipeline process dies". The old
    `except httpx.HTTPError` didn't cover this: httpx.InvalidURL is not an
    HTTPError subclass, so it passed straight through uncaught."""
    from app.ops import ping_healthcheck
    monkeypatch.setenv("HEALTHCHECK_URL", "https://hc-ping.com/abc\n123")
    with caplog.at_level("ERROR"):
        assert ping_healthcheck(success=True) is False
    assert any("healthcheck ping failed" in r.message for r in caplog.records)


def test_stale_cutoff_defaults_to_36_hours(cfg):
    from app.ops import stale_cutoff
    from app.models import utcnow

    now = utcnow()
    cutoff = stale_cutoff(cfg, "some_source_with_no_override", now)
    assert abs((now - cutoff).total_seconds() - 36 * 3600) < 1


def test_stale_cutoff_uses_a_sources_own_override(cfg):
    """The mechanism app.pipeline.notify._stale_sources now shares with
    `scout doctor` -- a per-source stale_hours in config.yaml changes the
    cutoff. Uses the real hcai_seismic_ratings override (2160h) already in
    config.yaml rather than a synthetic one, so this breaks if that value
    is ever accidentally removed."""
    from app.ops import stale_cutoff
    from app.models import utcnow

    now = utcnow()
    cutoff = stale_cutoff(cfg, "hcai_seismic_ratings", now)
    assert abs((now - cutoff).total_seconds() - 2160 * 3600) < 1


def test_source_is_stale_true_when_no_run_ever_recorded(db_session, cfg):
    """Different from _stale_sources' own skip-if-never-run behavior on
    purpose -- a page showing cached numbers has nothing to trust yet if
    the source has never once succeeded, and must say so rather than stay
    silent the way the digest deliberately does for an unconfigured
    source. See source_is_stale's own docstring for why the two differ."""
    from app.ops import source_is_stale
    assert source_is_stale(db_session, cfg, "match_contractors") is True


def test_source_is_stale_false_for_a_recent_success(db_session, cfg):
    from app.ops import source_is_stale
    from app.models import SourceRun, utcnow

    db_session.add(SourceRun(source="match_contractors", ok=True, started_at=utcnow()))
    db_session.commit()
    assert source_is_stale(db_session, cfg, "match_contractors") is False


def test_source_is_stale_true_past_the_configured_window(db_session, cfg):
    from datetime import timedelta

    from app.ops import source_is_stale
    from app.models import SourceRun, utcnow

    db_session.add(SourceRun(source="match_contractors", ok=True,
                             started_at=utcnow() - timedelta(hours=217)))  # past the 216h weekly window
    db_session.commit()
    assert source_is_stale(db_session, cfg, "match_contractors") is True


def test_source_is_stale_ignores_a_failed_run_with_no_prior_success(db_session, cfg):
    from app.ops import source_is_stale
    from app.models import SourceRun, utcnow

    db_session.add(SourceRun(source="match_contractors", ok=False, started_at=utcnow(),
                             error="boom"))
    db_session.commit()
    assert source_is_stale(db_session, cfg, "match_contractors") is True


def test_source_is_stale_does_not_confuse_a_name_that_prefixes_another(db_session, cfg):
    """match_contractors is a literal string-prefix of match_contractors_overdue
    -- a naive substring/LIKE match would let a fresh overdue-job run mask
    a stale general one, or vice versa."""
    from app.ops import source_is_stale
    from app.models import SourceRun, utcnow

    db_session.add(SourceRun(source="match_contractors_overdue", ok=True, started_at=utcnow()))
    db_session.commit()
    assert source_is_stale(db_session, cfg, "match_contractors") is True
    assert source_is_stale(db_session, cfg, "match_contractors_overdue") is False


def test_source_is_stale_counts_a_mode_suffixed_run(db_session, cfg):
    from app.ops import source_is_stale
    from app.models import SourceRun, utcnow

    db_session.add(SourceRun(source="match_contractors:backfill", ok=True, started_at=utcnow()))
    db_session.commit()
    assert source_is_stale(db_session, cfg, "match_contractors") is False


def test_doctor_and_digest_staleness_agree_on_the_same_source(db_session, cfg):
    """The actual bug: doctor() and _stale_sources() used to implement
    staleness independently and could disagree about the same source.
    A run inside a source's override window (57h old, la_ebewe_
    benchmarking's real 216h override) must read healthy in both."""
    from app.ops import doctor
    from app.pipeline.notify import _stale_sources
    from app.models import SourceRun, utcnow
    from datetime import timedelta

    db_session.add(SourceRun(source="la_ebewe_benchmarking", ok=True,
                             started_at=utcnow() - timedelta(hours=57)))
    db_session.commit()

    doctor_checks = dict((name, ok) for name, ok, _ in doctor())
    assert doctor_checks["source:la_ebewe_benchmarking"] is True
    assert "la_ebewe_benchmarking" not in _stale_sources(db_session, cfg)


def test_doctor_reports_missing_pieces(db_session, monkeypatch):
    from app.ops import doctor
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("HEALTHCHECK_URL", raising=False)
    checks = dict((name, (ok, detail)) for name, ok, detail in doctor())
    assert checks["database"][0] is True
    assert checks["anthropic_api_key"][0] is False
    assert checks["dead_mans_switch"][0] is False
    assert checks["source:ceqanet"][0] is False  # no successful run yet
    assert checks["llm_budget"][0] is True


def test_dead_mans_switch_not_applicable_on_the_web_service(db_session, monkeypatch):
    """The 2026-08-19 false positive: HEALTHCHECK_URL is deliberately only
    provisioned on the cron service (render.yaml), so app.mcp_tools.
    source_health -- which runs inside the WEB service -- always saw it
    unset and reported a FAIL that looked like a real incident. On the web
    service this check must report not-applicable (ok=True), never FAIL --
    a health check that cries wolf is worse than one that doesn't run
    there at all."""
    from app.ops import doctor
    monkeypatch.delenv("HEALTHCHECK_URL", raising=False)
    monkeypatch.setenv("RENDER_SERVICE_TYPE", "web")
    checks = dict((name, (ok, detail)) for name, ok, detail in doctor())
    ok, detail = checks["dead_mans_switch"]
    assert ok is True
    assert "not applicable" in detail


def test_dead_mans_switch_still_real_on_the_cron_service(db_session, monkeypatch):
    """The override is scoped to the web service only -- on the cron
    service (where this check is actually meaningful) a missing
    HEALTHCHECK_URL must still FAIL exactly as before."""
    from app.ops import doctor
    monkeypatch.delenv("HEALTHCHECK_URL", raising=False)
    monkeypatch.setenv("RENDER_SERVICE_TYPE", "cron")
    checks = dict((name, (ok, detail)) for name, ok, detail in doctor())
    assert checks["dead_mans_switch"][0] is False


# ---- doctor reads source_runs through a naming convention -------------------
#
# All seven sources reported "no successful run recorded" against a table holding
# nine successful backfill runs, because doctor matched `source == "ceqanet"` and
# backfill writes "ceqanet:backfill". These pin the convention from both ends.

def _doctor_checks(monkeypatch=None) -> dict:
    """Run doctor. Pass monkeypatch to unset the API key first — the source-run
    checks below do not care about the LLM and must not make a live call."""
    from app.ops import doctor
    if monkeypatch is not None:
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    return {name: (ok, detail) for name, ok, detail in doctor()}


def _run(session, name: str, *, ok: bool | None, hours_ago: float = 1.0, error: str | None = None):
    from datetime import timedelta

    from app.models import SourceRun, utcnow
    run = SourceRun(source=name, started_at=utcnow() - timedelta(hours=hours_ago),
                    ok=ok, error=error)
    session.add(run)
    session.commit()
    return run


def test_backfill_run_counts_as_a_run_of_its_source(db_session, monkeypatch):
    """The bug: a successful backfill left its source reading 'never ran'."""
    _run(db_session, "ceqanet:backfill", ok=True)
    checks = _doctor_checks(monkeypatch)
    assert checks["source:ceqanet"][0] is True
    # and it says which mode succeeded, so nobody reads a hand-run backfill as
    # evidence the scheduled fetch is alive
    assert "backfill" in checks["source:ceqanet"][1]


def test_backfill_run_does_not_hide_staleness(db_session, monkeypatch):
    """Counting backfills must not blind the 36h freshness check."""
    _run(db_session, "ceqanet:backfill", ok=True, hours_ago=40)
    checks = _doctor_checks(monkeypatch)
    assert checks["source:ceqanet"][0] is False
    assert "last success" in checks["source:ceqanet"][1]


def test_failed_runs_are_not_never_ran(db_session, monkeypatch):
    """A broken adapter and a scheduler that never fired are different diagnoses."""
    _run(db_session, "ceqanet:backfill", ok=False, error="HTTPError: 503")
    _run(db_session, "ceqanet", ok=False, error="HTTPError: 503")
    ok, detail = _doctor_checks(monkeypatch)["source:ceqanet"]
    assert ok is False
    assert "2 run(s) recorded, none successful" in detail
    assert "503" in detail
    # a source with genuinely nothing in the table still says so
    assert "no run of any kind recorded" in _doctor_checks(monkeypatch)["source:rss"][1]


def test_field_intel_is_not_a_source_health_check(db_session, monkeypatch):
    """field_intel is human-entered with no scheduled run -- it must never
    gain a source:field_intel doctor check or a staleness banner, which
    would fire on any week nobody happened to talk to a GC and teach the
    same red-banner signal that means something real broke elsewhere to
    mean nothing here. See app.field_intel.field_intel_activity, the
    plain activity counter this uses instead."""
    from app.config import load_config
    assert "field_intel" not in load_config().data.get("sources", {})
    checks = _doctor_checks(monkeypatch)
    assert "source:field_intel" not in checks


def test_unattributable_run_name_fails_loudly(db_session, monkeypatch):
    """The invariant: no source_runs row may be invisible to the health check.

    A run name no check can attribute is the same failure class as a source
    reporting OK while producing nothing — the board goes green over a machine
    nobody is watching. It must fail here, not silently degrade `source:ceqanet`
    into a false alarm nobody can explain.
    """
    _run(db_session, "ceqanet-backfill", ok=True)  # hyphen, not the ':' convention
    ok, detail = _doctor_checks(monkeypatch)["source_run_names"]
    assert ok is False
    assert "ceqanet-backfill" in detail


def test_stage_and_source_run_names_are_attributable(db_session, monkeypatch):
    """Both live conventions pass: stage names and every source:mode name."""
    from app.models import BACKFILL_RUN_MODE, source_run_name
    _run(db_session, "resolve", ok=True)
    _run(db_session, "extract", ok=True)
    _run(db_session, source_run_name("ceqanet", BACKFILL_RUN_MODE), ok=True)
    _run(db_session, source_run_name("edgar"), ok=True)
    # a disabled source's history is still attributable
    _run(db_session, source_run_name("primegov", BACKFILL_RUN_MODE), ok=True)
    assert _doctor_checks(monkeypatch)["source_run_names"][0] is True


def test_key_check_fails_when_the_account_cannot_run_inference(db_session, monkeypatch):
    """Authenticating is not the same as being able to work.

    models.list() returned 200 on 2026-08-06 while every triage and extract call
    failed with "credit balance is too low", so doctor reported the LLM healthy
    against an account that could not run a single inference. count_tokens
    exercises the billing check on the real path and is not itself billed.
    """
    import anthropic

    class _Messages:
        def count_tokens(self, **kw):
            raise RuntimeError("credit balance is too low to access the Anthropic API")

    class _Client:
        def __init__(self, **kw):
            self.messages = _Messages()
            # the old probe: authenticates fine, tells you nothing about billing
            self.models = type("M", (), {"list": staticmethod(lambda **k: ["ok"])})()

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setattr(anthropic, "Anthropic", _Client)
    ok, detail = _doctor_checks()["anthropic_api_key"]
    assert ok is False
    assert "credit balance" in detail


def test_key_check_passes_when_inference_is_available(db_session, monkeypatch):
    import anthropic

    class _Client:
        def __init__(self, **kw):
            self.messages = type("M", (), {
                "count_tokens": staticmethod(
                    lambda **k: type("R", (), {"input_tokens": 9})())})()

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setattr(anthropic, "Anthropic", _Client)
    assert _doctor_checks()["anthropic_api_key"][0] is True


# ---- invariant 2: no project carries a score with zero linked signals -------

def _project(db_session, name: str, status: str = "active", score: float = 0.5):
    from app.models import Category, Project, Stage
    p = Project(name=name, category=Category.data_center, stage=Stage.entitlement,
                status=status, score=score, in_territory=True)
    db_session.add(p)
    db_session.commit()
    return p


def test_scored_project_with_no_signal_fails(db_session, monkeypatch):
    """A row with a name, a score and no evidence under any of it."""
    p = _project(db_session, "Phantom Logistics Center")
    ok, detail = _doctor_checks(monkeypatch)["project_evidence"]
    assert ok is False
    assert f"#{p.id}" in detail and "Phantom Logistics Center" in detail
    assert "NO linked signal" in detail


def test_merged_project_with_no_signal_is_expected(db_session, monkeypatch):
    """Merging moves the signals to the survivor by design, so the merged row is
    signal-less for the right reason — and it is off the board anyway."""
    _project(db_session, "First Industrial Commerce Center II", status="merged")
    assert _doctor_checks(monkeypatch)["project_evidence"][0] is True


def test_project_with_a_linked_signal_passes(db_session, monkeypatch):
    from app.models import (
        Category, ProjectSignal, RawDocument, Signal, SignalType, Stage, TriageResult,
    )
    p = _project(db_session, "Real Campus")
    doc = RawDocument(source="ceqanet", source_uid="ev1", url="https://x/ev1",
                      title="NOP", raw_text="t", content_hash="hev1")
    db_session.add(doc)
    db_session.commit()
    sig = Signal(raw_document_id=doc.id, signal_type=SignalType.ceqa_nop,
                 triage_result=TriageResult.relevant, category=Category.data_center,
                 stage=Stage.entitlement, project_name="Real Campus")
    db_session.add(sig)
    db_session.commit()
    db_session.add(ProjectSignal(project_id=p.id, signal_id=sig.id,
                                 match_confidence=1.0, match_method="manual"))
    db_session.commit()
    assert _doctor_checks(monkeypatch)["project_evidence"][0] is True


def test_run_name_round_trips():
    from app.models import BACKFILL_RUN_MODE, run_name_mode, run_name_source, source_run_name
    name = source_run_name("ceqanet", BACKFILL_RUN_MODE)
    assert name == "ceqanet:backfill"
    assert run_name_source(name) == "ceqanet"
    assert run_name_mode(name) == "backfill"
    assert run_name_source("ceqanet") == "ceqanet"
    assert run_name_mode("ceqanet") is None


def test_backfill_writes_the_name_doctor_reads(db_session, cfg, monkeypatch):
    """Writer and reader agree, pinned against the real backfill path.

    The round-trip test above proves the helpers agree with each other; this
    proves run_backfill actually uses them. That is the join the original bug
    lived in.
    """
    from app.models import SourceRun, run_name_source
    from app.pipeline import backfill as backfill_mod

    monkeypatch.setattr(backfill_mod, "get_adapter",
                        lambda source: _StubAdapter())
    monkeypatch.setattr(backfill_mod, "PoliteClient", lambda **kw: _NullClient())
    backfill_mod.run_backfill(db_session, cfg, "ceqanet",
                              backfill_mod.utcnow(), force=True)
    names = {r.source for r in db_session.exec(select(SourceRun)).all()}
    assert names == {"ceqanet:backfill"}
    assert run_name_source(names.pop()) == "ceqanet"
    assert _doctor_checks(monkeypatch)["source:ceqanet"][0] is True


class _StubAdapter:
    def backfill_chunks(self, cfg, since):
        return [{"key": "chunk-1"}]

    def fetch_chunk(self, cfg, client, since, chunk):
        return iter(())


class _NullClient:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False
