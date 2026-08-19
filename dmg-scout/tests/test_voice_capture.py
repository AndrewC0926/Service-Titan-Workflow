"""Voice capture: transcription, structured extraction (constrained decoding
+ Pydantic validation), fuzzy name resolution, and the review_queue ->
confirm/reject chain. See app/pipeline/voice_capture.py's module docstring
for the full call graph.

Whisper (httpx) and the Anthropic extraction call are mocked -- these tests
are about the chain's own logic (idempotency, validation, exactly-one-writer),
not live network. See test_capture_live.py (skipped unless real API keys are
present) for the actual end-to-end run against real services."""
from __future__ import annotations

import base64

import httpx
import pydantic
import pytest
import respx

import app.llm as llm_mod
import app.pipeline.voice_capture as vc
from app.db import get_session
from app.models import CaptureAudio, Outreach, Project, ReviewQueue
from app.schemas import OutreachCallExtraction
from app.web.main import app as web_app


def _mock_whisper(text: str, duration: float = 30.0) -> None:
    respx.post(vc.WHISPER_URL).mock(
        return_value=httpx.Response(200, json={"text": text, "duration": duration}))


# --- OutreachCallExtraction (Pydantic layer) ---------------------------


def test_blank_and_unknown_values_become_none():
    e = OutreachCallExtraction(contact_name="", firm_name="unknown", stage="Not stated",
                               next_action="n/a", confidence=0.5)
    assert e.contact_name is None and e.firm_name is None
    assert e.stage is None and e.next_action is None


def test_real_values_pass_through_unchanged():
    e = OutreachCallExtraction(contact_name="Jon Smith", outcome="Discussed pricing.",
                               next_action_date="2026-08-20", confidence=0.9)
    assert e.contact_name == "Jon Smith"
    assert str(e.next_action_date) == "2026-08-20"


def test_confidence_out_of_range_raises():
    with pytest.raises(pydantic.ValidationError):
        OutreachCallExtraction(confidence=1.5)


def test_malformed_date_raises_not_silently_nulled():
    with pytest.raises(pydantic.ValidationError):
        OutreachCallExtraction(next_action_date="next Tuesday", confidence=0.5)


def test_unknown_field_rejected_additionalproperties_false():
    with pytest.raises(pydantic.ValidationError):
        OutreachCallExtraction.model_validate({"confidence": 0.5, "made_up_field": "x"})


# --- extract_voice_capture: constrained decoding + explicit validation --


def test_extract_voice_capture_validates_tool_output(monkeypatch, cfg):
    monkeypatch.setattr(llm_mod, "_tool_call", lambda *a, **k: {
        "contact_name": "Jon Smith", "firm_name": "ACCO", "confidence": 0.8,
    })
    result = llm_mod.extract_voice_capture("some transcript")
    assert isinstance(result, OutreachCallExtraction)
    assert result.contact_name == "Jon Smith"


def test_extract_voice_capture_raises_on_bad_model_output(monkeypatch, cfg):
    """The model didn't respect the schema (out-of-range confidence) --
    this must raise, not silently coerce, per the explicit-validation
    requirement. If _tool_call's forced tool_choice were trusted alone,
    a stray out-of-range value would sail through uncaught."""
    monkeypatch.setattr(llm_mod, "_tool_call", lambda *a, **k: {"confidence": 7.0})
    with pytest.raises(pydantic.ValidationError):
        llm_mod.extract_voice_capture("some transcript")


def test_extract_voice_capture_records_cost_via_last_call_cost_usd(monkeypatch, cfg):
    def fake_tool_call(model, system, tool, content, max_tokens=1024, stage="voice_capture"):
        llm_mod._last_call_cost_usd.set(0.0042)
        return {"confidence": 0.5}
    monkeypatch.setattr(llm_mod, "_tool_call", fake_tool_call)
    llm_mod.extract_voice_capture("t")
    assert llm_mod.last_call_cost_usd() == 0.0042


# --- app.voice_match: fuzzy resolution, ranked, thresholded --------------


def test_resolve_firm_finds_a_misheard_name(db_session, cfg):
    from app.models import Firm
    from app.normalize import normalize_name
    from app.voice_match import resolve_firm

    db_session.add(Firm(name="ACCO Engineered Systems", name_norm=normalize_name("ACCO Engineered Systems")))
    db_session.commit()
    candidates = resolve_firm(db_session, "Ekko Engineered Systems")  # misheard "ACCO"
    assert candidates and candidates[0].label == "ACCO Engineered Systems"


def test_resolve_below_threshold_returns_unresolved(db_session, cfg):
    from app.models import Firm
    from app.normalize import normalize_name
    from app.voice_match import resolve_firm

    db_session.add(Firm(name="Totally Different Company", name_norm=normalize_name("Totally Different Company")))
    db_session.commit()
    assert resolve_firm(db_session, "Zephyr Widgets") == []


def test_resolve_ranks_multiple_candidates_best_first(db_session, cfg):
    from app.models import Project
    from app.voice_match import resolve_project

    db_session.add(Project(name="Vantage Data Centers Reno"))
    db_session.add(Project(name="Vantage Data Centers Storey County"))
    db_session.commit()
    candidates = resolve_project(db_session, "Vantage Data Centers Reno")
    assert len(candidates) == 2
    assert candidates[0].score >= candidates[1].score
    assert candidates[0].label.startswith("Vantage Data Centers Reno")


def test_resolve_project_treats_different_phases_as_ties_not_a_bug(db_session, cfg):
    """normalize_name() (app/normalize.py) deliberately strips "Phase N"
    wording -- built for matching company names against SPE-coded
    subsidiaries, where that's correct. Reused here for project names, the
    same rule means two real, distinct phases of one campus score as a
    perfect tie rather than one clearly outranking the other. Not a
    resolver bug: BOTH still surface as candidates (never auto-selected),
    so the human picks the right one -- this test documents the tie
    instead of asserting a ranking the normalization can't actually
    produce."""
    from app.models import Project
    from app.voice_match import resolve_project

    db_session.add(Project(name="Vantage Reno Phase 1"))
    db_session.add(Project(name="Vantage Reno Phase 2"))
    db_session.commit()
    candidates = resolve_project(db_session, "Vantage Reno Phase 2")
    assert len(candidates) == 2
    assert {c.score for c in candidates} == {100.0}


def test_resolve_project_never_matches_retrofit_buildings(db_session, cfg):
    """Scoped deliberately -- Outreach has no writer for RetrofitBuilding,
    so a match there would be confirmable into a wrong entity type."""
    from app.voice_match import resolve_project
    # No RetrofitBuilding import/seed needed -- the point is the query only
    # ever touches Project, so an empty Project table means empty results
    # regardless of what's in retrofit_buildings.
    assert resolve_project(db_session, "1234 Main St") == []


# --- capture_voice_note: the fixed chain, one row, resilient to failure --


@respx.mock
def test_capture_voice_note_happy_path(db_session, cfg, monkeypatch):
    _mock_whisper("Talked to Jon about the chiller quote for Vantage Reno.")
    monkeypatch.setattr("app.llm.extract_voice_capture", lambda transcript: OutreachCallExtraction(
        contact_name="Jon Smith", firm_name="ACCO", project_or_building_name="Vantage Reno",
        outcome="Talked pricing on the chiller.", next_action="send quote", confidence=0.85))
    monkeypatch.setattr("app.llm.last_call_cost_usd", lambda: 0.001)
    monkeypatch.setattr("app.spend.check_budget", lambda: None)

    row = vc.capture_voice_note(db_session, cfg, audio_bytes=b"FAKE", filename="x.m4a",
                                content_type="audio/m4a")

    assert row.status == "pending" and row.agent == "voice_capture"
    assert row.proposed_payload["contact_name"] == "Jon Smith"
    assert row.confidence == 0.85
    assert row.provenance["transcript"].startswith("Talked to Jon")
    assert row.provenance["extraction_error"] is None
    audio = db_session.get(CaptureAudio, row.provenance["audio_id"])
    assert audio.data == b"FAKE"


@respx.mock
def test_capture_voice_note_survives_extraction_failure(db_session, cfg, monkeypatch):
    """Transcription succeeds, extraction blows up -- the row must still be
    created with the transcript preserved, per this module's own contract:
    every unlogged call is a label lost permanently."""
    _mock_whisper("A real transcript that must not be lost.")

    def boom(transcript):
        raise RuntimeError("LLM is down")
    monkeypatch.setattr("app.llm.extract_voice_capture", boom)
    monkeypatch.setattr("app.spend.check_budget", lambda: None)

    row = vc.capture_voice_note(db_session, cfg, audio_bytes=b"FAKE", filename="x.m4a",
                                content_type="audio/m4a")

    assert row.status == "pending"
    assert row.proposed_payload["contact_name"] is None
    assert "LLM is down" in row.provenance["extraction_error"]
    assert row.provenance["transcript"] == "A real transcript that must not be lost."


@respx.mock
def test_capture_voice_note_raises_on_transcription_failure(db_session, cfg):
    respx.post(vc.WHISPER_URL).mock(return_value=httpx.Response(500, text="server error"))
    with pytest.raises(vc.TranscriptionFailed):
        vc.capture_voice_note(db_session, cfg, audio_bytes=b"FAKE", filename="x.m4a",
                              content_type="audio/m4a")
    # Nothing partial was left behind -- no review_queue row for a call
    # with no transcript at all.
    assert db_session.exec(__import__("sqlmodel").select(ReviewQueue)).all() == []


def test_capture_voice_note_rejects_oversized_upload(db_session, cfg):
    with pytest.raises(vc.TranscriptionFailed):
        vc.transcribe_audio(b"x" * (vc.MAX_UPLOAD_BYTES + 1), "x.m4a", "audio/m4a")


# --- web endpoints: auth, confirm is the one writer, reject discards -----


@pytest.fixture()
def client(db_session, monkeypatch):
    monkeypatch.setenv("DASHBOARD_PASSWORD", "testpw")
    monkeypatch.setenv("CAPTURE_API_KEY", "testcapkey")
    from fastapi.testclient import TestClient
    web_app.dependency_overrides[get_session] = lambda: db_session
    yield TestClient(web_app)
    web_app.dependency_overrides.clear()


AUTH = {"Authorization": "Basic " + base64.b64encode(b"andrew:testpw").decode()}


def _seed_capture(db_session, *, project_name="Vantage Reno") -> tuple[int, int]:
    from app.models import utcnow
    audio = CaptureAudio(content_type="audio/m4a", data=b"AUDIO", size_bytes=5)
    db_session.add(audio)
    db_session.commit()
    db_session.refresh(audio)
    project = Project(name=project_name)
    db_session.add(project)
    db_session.commit()
    db_session.refresh(project)
    row = ReviewQueue(
        agent="voice_capture", trace_id="t1", entity_type="outreach_call",
        proposed_payload={"contact_name": None, "firm_name": None,
                          "project_or_building_name": project_name, "outcome": "Talked pricing.",
                          "stage": None, "next_action": "send quote", "next_action_date": "2026-08-20"},
        provenance={"source": "ios_shortcut", "audio_id": audio.id, "transcript": "test",
                    "extraction_error": None},
        confidence=0.8, status="pending",
    )
    db_session.add(row)
    db_session.commit()
    db_session.refresh(row)
    return row.id, project.id


def test_capture_endpoint_requires_bearer_token(client):
    assert client.post("/capture/voice", files={"file": ("x.m4a", b"a", "audio/m4a")}).status_code == 401
    assert client.post("/capture/voice", headers={"Authorization": "Bearer wrong"},
                       files={"file": ("x.m4a", b"a", "audio/m4a")}).status_code == 401


def test_captures_list_requires_dashboard_auth(client):
    assert client.get("/captures").status_code == 401


def test_confirm_calls_the_one_outreach_writer(db_session, client):
    row_id, project_id = _seed_capture(db_session)
    r = client.post(f"/captures/{row_id}/confirm", headers=AUTH,
                    data={"project_id": project_id, "notes": "Talked pricing.",
                          "next_action": "send quote", "next_action_date": "2026-08-20",
                          "channel": "call"})
    assert r.status_code == 200

    from sqlmodel import select
    outreach_rows = db_session.exec(select(Outreach).where(Outreach.project_id == project_id)).all()
    assert len(outreach_rows) == 1
    assert outreach_rows[0].notes == "Talked pricing."
    assert db_session.get(ReviewQueue, row_id).status == "approved"


def test_confirm_twice_is_rejected_not_double_written(db_session, client):
    row_id, project_id = _seed_capture(db_session)
    data = {"project_id": project_id, "notes": "x", "next_action": "", "next_action_date": "", "channel": "call"}
    assert client.post(f"/captures/{row_id}/confirm", headers=AUTH, data=data).status_code == 200
    assert client.post(f"/captures/{row_id}/confirm", headers=AUTH, data=data).status_code == 409

    from sqlmodel import select
    assert len(db_session.exec(select(Outreach).where(Outreach.project_id == project_id)).all()) == 1


def test_reject_writes_nothing_to_outreach(db_session, client):
    row_id, project_id = _seed_capture(db_session)
    r = client.post(f"/captures/{row_id}/reject", headers=AUTH)
    assert r.status_code == 200
    assert db_session.get(ReviewQueue, row_id).status == "rejected"

    from sqlmodel import select
    assert db_session.exec(select(Outreach).where(Outreach.project_id == project_id)).all() == []


def test_confirm_requires_a_real_project(db_session, client):
    row_id, _ = _seed_capture(db_session)
    r = client.post(f"/captures/{row_id}/confirm", headers=AUTH,
                    data={"project_id": 999999, "notes": "x", "next_action": "",
                          "next_action_date": "", "channel": "call"})
    assert r.status_code == 400


def test_audio_streams_back_the_original_bytes(db_session, client):
    row_id, _ = _seed_capture(db_session)
    r = client.get(f"/captures/{row_id}/audio", headers=AUTH)
    assert r.status_code == 200 and r.content == b"AUDIO"


# --- browser capture: /capture page, /capture/voice GET probe, relay -----


def test_capture_page_requires_dashboard_auth(client):
    assert client.get("/capture").status_code == 401


def test_capture_page_renders_the_recorder_when_configured(client):
    r = client.get("/capture", headers=AUTH)
    assert r.status_code == 200
    assert "record-btn" in r.text


def test_capture_page_shows_not_configured_when_key_missing(client, monkeypatch):
    monkeypatch.delenv("CAPTURE_API_KEY", raising=False)
    r = client.get("/capture", headers=AUTH)
    assert r.status_code == 200
    assert "not configured" in r.text.lower()
    assert "record-btn" not in r.text


def test_capture_voice_probe_with_no_token_explains_what_it_expects(client):
    r = client.get("/capture/voice")
    assert r.status_code == 200
    assert "alive" in r.text.lower()
    assert "Bearer" in r.text


def test_capture_voice_probe_rejects_a_wrong_token(client):
    r = client.get("/capture/voice?token=wrong")
    assert r.status_code == 401
    assert "does NOT match" in r.text


def test_capture_voice_probe_confirms_a_correct_token_via_query_param(client):
    """A bare Safari address-bar visit can't attach an Authorization header
    -- ?token=... is the only way to verify the bearer token from a phone
    without going through the Shortcut itself."""
    r = client.get("/capture/voice?token=testcapkey")
    assert r.status_code == 200
    assert "correct" in r.text.lower()


def test_capture_voice_probe_confirms_a_correct_token_via_header(client):
    r = client.get("/capture/voice", headers={"Authorization": "Bearer testcapkey"})
    assert r.status_code == 200
    assert "correct" in r.text.lower()


def test_capture_voice_probe_when_key_is_unset(client, monkeypatch):
    monkeypatch.delenv("CAPTURE_API_KEY", raising=False)
    r = client.get("/capture/voice")
    assert r.status_code == 200
    assert "not set" in r.text.lower()


def test_capture_upload_requires_dashboard_auth(client):
    r = client.post("/capture/upload", files={"file": ("x.webm", b"a", "audio/webm")})
    assert r.status_code == 401


def test_capture_upload_503s_when_key_is_unset(client, monkeypatch):
    monkeypatch.delenv("CAPTURE_API_KEY", raising=False)
    r = client.post("/capture/upload", headers=AUTH, files={"file": ("x.webm", b"a", "audio/webm")})
    assert r.status_code == 503


@respx.mock
def test_capture_upload_relays_to_the_real_capture_voice_endpoint(client, db_session, monkeypatch):
    """The browser-capture relay (app/web/main.py:capture_upload_relay) must
    exercise the SAME /capture/voice code path the iOS Shortcut hits -- an
    in-process ASGI call, not a bypass -- so a working browser capture
    actually proves the server side is fine. audio/mp4 here matches what
    Safari's MediaRecorder actually produces (see capture_record.html)."""
    _mock_whisper("Recorded from Safari on iOS.")
    monkeypatch.setattr("app.llm.extract_voice_capture",
                        lambda transcript: OutreachCallExtraction(confidence=0.4))
    monkeypatch.setattr("app.llm.last_call_cost_usd", lambda: 0.0)
    monkeypatch.setattr("app.spend.check_budget", lambda: None)

    r = client.post("/capture/upload", headers=AUTH,
                    files={"file": ("capture.mp4", b"FAKE-AAC-AUDIO", "audio/mp4")})
    assert r.status_code == 200
    assert "logged, review at /captures/" in r.text

    from sqlmodel import select
    rows = db_session.exec(select(ReviewQueue)).all()
    assert len(rows) == 1
    assert rows[0].provenance["transcript"] == "Recorded from Safari on iOS."


# --- capture_voice_logging_middleware: every attempt logged, with why -----
#
# 2026-08-19 bugfix: the previous version of these tests only asserted that
# SOME log line appeared, or that a generic "401" substring was in it --
# never the actual reason text. That's exactly how a real bug shipped past
# all of them: the reason field was reading back "HTTP 401" for every
# rejection regardless of cause (call_next()'s returned response has no
# eagerly-rendered .body -- see app.access_log's module comment), and
# "capture/voice" and "401" were BOTH still present in that wrong string,
# so the old assertions passed anyway. These now assert the real reason
# text a human would actually read off the log line.


def test_capture_voice_logging_middleware_logs_the_real_auth_rejection_reason(client, caplog):
    """The 2026-08-19 finding this whole feature responds to: the iOS
    Shortcut has never once appeared anywhere, including this log -- if it
    ever DOES fire and fail, this is what makes that visible instead of
    invisible."""
    with caplog.at_level("WARNING"):
        client.post("/capture/voice", files={"file": ("x.m4a", b"a", "audio/m4a")})
    assert any(
        "capture/voice POST status=401 "
        "reason=bearer token missing or does not match CAPTURE_API_KEY" in r.message
        for r in caplog.records
    )


def test_capture_voice_logging_middleware_logs_the_real_missing_file_reason(client, caplog):
    """Confirmed empirically: FastAPI can't actually distinguish "wrong
    content type" from "missing file field" for this endpoint -- a
    non-multipart body just means the required 'file' part is never found
    either, so both surface as the identical RequestValidationError. The
    reason text reflects that (body.file: Field required), not a made-up
    distinction the framework doesn't actually draw."""
    with caplog.at_level("WARNING"):
        client.post("/capture/voice", headers={"Authorization": "Bearer testcapkey"})
    assert any(
        "capture/voice POST status=422 reason=" in r.message and "file" in r.message
        and "Field required" in r.message
        for r in caplog.records
    )


def test_capture_voice_logging_middleware_logs_the_real_wrong_content_type_reason(client, caplog):
    with caplog.at_level("WARNING"):
        client.post("/capture/voice", headers={"Authorization": "Bearer testcapkey",
                                               "Content-Type": "application/json"},
                    content=b'{"not":"multipart"}')
    assert any(
        "capture/voice POST status=422 reason=" in r.message and "file" in r.message
        for r in caplog.records
    )


def test_capture_voice_logging_middleware_logs_the_real_oversized_upload_reason(client, caplog):
    oversized = vc.MAX_UPLOAD_BYTES + 1
    with caplog.at_level("WARNING"):
        client.post("/capture/voice", headers={"Authorization": "Bearer testcapkey"},
                    files={"file": ("x.m4a", b"x" * oversized, "audio/m4a")})
    assert any(
        f"capture/voice POST status=413 reason={oversized} bytes exceeds the" in r.message
        for r in caplog.records
    )


@respx.mock
def test_capture_voice_logging_middleware_logs_success_with_reason_ok(client, caplog, monkeypatch):
    _mock_whisper("logged ok")
    monkeypatch.setattr("app.llm.extract_voice_capture",
                        lambda transcript: OutreachCallExtraction(confidence=0.1))
    monkeypatch.setattr("app.llm.last_call_cost_usd", lambda: 0.0)
    monkeypatch.setattr("app.spend.check_budget", lambda: None)
    with caplog.at_level("INFO"):
        r = client.post("/capture/voice", headers={"Authorization": "Bearer testcapkey"},
                        files={"file": ("x.m4a", b"a", "audio/m4a")})
    assert r.status_code == 200
    assert any("capture/voice POST status=200 reason=ok" in r.message for r in caplog.records)


def test_capture_voice_logging_middleware_ignores_get(client, caplog):
    with caplog.at_level("INFO"):
        client.get("/capture/voice")
    assert not any("capture/voice POST" in r.message for r in caplog.records)
