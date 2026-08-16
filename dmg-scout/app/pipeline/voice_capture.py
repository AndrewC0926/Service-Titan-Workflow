"""Voice-capture -> review_queue proposal. A FIXED CHAIN, not an agent: one
transcription call, one LLM tool call, one fuzzy-match pass, one DB write.
No loop, no tool-calling agent deciding what to do next — see this
module's own call graph for the whole thing.

    iOS Shortcut --(multipart POST)--> /capture/voice (app/web/main.py)
        -> capture_voice_note() (here)
            -> store CaptureAudio (raw bytes, so the review card can replay it)
            -> transcribe_audio()            [Whisper, one HTTP call]
            -> app.llm.extract_voice_capture()  [Claude, one tool call,
               constrained decoding + Pydantic validation]
            -> app.voice_match.resolve_all()  [rapidfuzz against Scout's
               own contacts/firms/projects/contractors -- ranked
               candidates only, nothing auto-selected]
            -> write ONE review_queue row (a PROPOSAL, never a final record)

A human reviews the row at /captures/{id}, edits any field, picks a
resolved candidate (or leaves it unresolved), and either:
  - Confirms  -> app.outreach.log_outreach() -- the SAME writer the
    dashboard's outreach form and the log_outreach MCP tool both use, so
    this whole pipeline never has its own Outreach insert.
  - Rejects   -> the review_queue row is marked rejected and discarded.
    Nothing about a rejected capture is ever written anywhere else.

Never loses the call on a partial failure: if transcription succeeds but
extraction fails (LLM error, or the model's output fails Pydantic
validation), the review_queue row is still created, with empty proposed
fields but the full transcript preserved in provenance -- a human can
still fill the CRM fields by hand from the transcript. Every unlogged call
is a label lost permanently; a capture that only partially automated is
still a capture, not a loss.
"""
from __future__ import annotations

import logging
import uuid

import httpx
from pydantic import ValidationError

from app.config import Config, openai_api_key
from app.models import CaptureAudio, ReviewQueue, utcnow
from app.voice_match import resolve_all

log = logging.getLogger(__name__)

WHISPER_URL = "https://api.openai.com/v1/audio/transcriptions"
WHISPER_MODEL = "whisper-1"
# $0.006/minute, OpenAI's standard published rate for whisper-1 as of
# 2026-08-16 (also gpt-4o-transcribe; gpt-4o-mini-transcribe is cheaper at
# $0.003/min but is a different, newer model this task did not ask for).
# There is no published per-request minimum or rounding rule beyond
# per-second billing, so this is duration-proportional, not a flat fee.
WHISPER_USD_PER_MINUTE = 0.006

MAX_UPLOAD_BYTES = 25 * 1024 * 1024  # Whisper's own file-size ceiling


class TranscriptionFailed(Exception):
    pass


def transcribe_audio(audio_bytes: bytes, filename: str, content_type: str) -> dict:
    """One direct HTTP call to OpenAI's Whisper endpoint -- no SDK, matching
    this codebase's existing style of calling vendor APIs directly via
    httpx (see app/http.py) rather than pulling in a client library for a
    single endpoint. Returns {text, duration_minutes, cost_usd}."""
    key = openai_api_key()
    if not key:
        raise TranscriptionFailed("OPENAI_API_KEY is not set")
    if len(audio_bytes) > MAX_UPLOAD_BYTES:
        raise TranscriptionFailed(
            f"{len(audio_bytes)} bytes exceeds Whisper's {MAX_UPLOAD_BYTES}-byte file limit")
    try:
        resp = httpx.post(
            WHISPER_URL,
            headers={"Authorization": f"Bearer {key}"},
            files={"file": (filename, audio_bytes, content_type)},
            data={"model": WHISPER_MODEL, "response_format": "verbose_json"},
            timeout=120.0,
        )
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise TranscriptionFailed(f"Whisper returned {exc.response.status_code}: {exc.response.text[:300]}") from exc
    except httpx.HTTPError as exc:
        raise TranscriptionFailed(f"Whisper request failed: {exc}") from exc

    data = resp.json()
    text = (data.get("text") or "").strip()
    duration_seconds = data.get("duration")  # verbose_json includes this
    duration_minutes = (duration_seconds / 60.0) if duration_seconds is not None else None
    cost_usd = (duration_minutes * WHISPER_USD_PER_MINUTE) if duration_minutes is not None else None
    return {"text": text, "duration_seconds": duration_seconds,
            "duration_minutes": duration_minutes, "cost_usd": cost_usd}


def capture_voice_note(session, cfg: Config, *, audio_bytes: bytes, filename: str,
                       content_type: str, source: str = "ios_shortcut") -> ReviewQueue:
    """The whole chain, end to end. Raises TranscriptionFailed if Whisper
    itself can't be reached or rejects the file -- that's the one failure
    with nothing to review (no transcript exists yet). Every failure AFTER
    a transcript exists is absorbed into the review_queue row instead."""
    trace_id = str(uuid.uuid4())

    audio = CaptureAudio(content_type=content_type, data=audio_bytes, size_bytes=len(audio_bytes))
    session.add(audio)
    session.commit()
    session.refresh(audio)

    transcription = transcribe_audio(audio_bytes, filename, content_type)
    transcript = transcription["text"]

    proposed: dict = {
        "contact_name": None, "firm_name": None, "project_or_building_name": None,
        "outcome": None, "stage": None, "next_action": None, "next_action_date": None,
    }
    confidence = 0.0
    extraction_error = None
    extraction_cost_usd = 0.0
    candidates: dict = {"contact": [], "firm": [], "contractor": [], "project": []}

    if not transcript:
        extraction_error = "empty transcript -- Whisper returned no text for this audio"
    else:
        try:
            from app.llm import extract_voice_capture, last_call_cost_usd  # local import:
            # keeps this module importable without an Anthropic key
            # configured, same reasoning app.llm.LLMUnavailable exists for.
            from app.spend import check_budget

            check_budget()
            extraction = extract_voice_capture(transcript)
            extraction_cost_usd = last_call_cost_usd() or 0.0
            proposed = extraction.model_dump(exclude={"confidence"}, mode="json")
            confidence = extraction.confidence
            candidates = resolve_all(
                session,
                contact_name=extraction.contact_name,
                firm_name=extraction.firm_name,
                project_or_building_name=extraction.project_or_building_name,
            )
        except ValidationError as exc:
            extraction_error = f"model output failed schema validation: {exc}"
            log.warning("voice_capture #%s: extraction validation failed: %s", trace_id, exc)
        except Exception as exc:  # noqa: BLE001 — the transcript must still be saved
            extraction_error = f"{type(exc).__name__}: {exc}"
            log.warning("voice_capture #%s: extraction failed: %s", trace_id, exc)

    row = ReviewQueue(
        agent="voice_capture",
        trace_id=trace_id,
        entity_type="outreach_call",
        proposed_payload=proposed,
        provenance={
            "source": source,
            "retrieved_at": utcnow().isoformat(),
            "audio_id": audio.id,
            "audio_content_type": content_type,
            "transcript": transcript,
            "transcript_duration_seconds": transcription.get("duration_seconds"),
            "extraction_error": extraction_error,
            "match_candidates": {
                k: [c.__dict__ for c in v] for k, v in candidates.items()
            },
            "whisper_cost_usd": transcription.get("cost_usd"),
            "extraction_cost_usd": extraction_cost_usd,
        },
        confidence=confidence,
        status="pending",
    )
    session.add(row)
    session.commit()
    session.refresh(row)
    return row
