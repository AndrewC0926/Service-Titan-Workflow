from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import anthropic
import httpx2
import pytest

from committee.agents.errors import LLMCallFailed
from committee.agents.llm import (
    AnthropicClient,
    LLMRequest,
    RecordedClient,
    RecordingClient,
    RetryPolicy,
    Usage,
    accepts_sampling,
    build_messages_params,
    cost_usd,
)
from committee.config.schema import ModelPrice

REQ = LLMRequest(
    agent="fundamentals",
    model="claude-opus-5-5",
    max_tokens=100,
    temperature=0.0,
    system_blocks=("PREAMBLE", "AGENT PROMPT"),
    packet="PACKET",
)


def test_params_cache_preamble_and_packet() -> None:
    p = build_messages_params(REQ)
    assert p["model"] == "claude-opus-5-5" and p["max_tokens"] == 100
    assert p["system"][0] == {
        "type": "text",
        "text": "PREAMBLE",
        "cache_control": {"type": "ephemeral"},
    }
    assert "cache_control" not in p["system"][1]
    user = p["messages"][0]
    assert user["role"] == "user"
    assert user["content"][0]["cache_control"] == {"type": "ephemeral"}
    assert user["content"][0]["text"] == "PACKET"
    # current-generation models reject sampling params: none sent
    assert "extra_body" not in p and "temperature" not in p


def test_params_temperature_only_for_models_that_accept_it() -> None:
    req = LLMRequest(**{**REQ.__dict__, "model": "claude-haiku-4-5-20251001"})
    assert build_messages_params(req)["extra_body"] == {"temperature": 0.0}
    assert accepts_sampling("claude-sonnet-4-6")
    assert not accepts_sampling("claude-opus-5-5")
    assert not accepts_sampling("claude-sonnet-5-5")


def test_followups_appended() -> None:
    req = LLMRequest(**{**REQ.__dict__, "followups": (("assistant", "bad"), ("user", "fix it"))})
    msgs = build_messages_params(req)["messages"]
    assert [m["role"] for m in msgs] == ["user", "assistant", "user"]


def test_cost_includes_cache_tokens() -> None:
    price = ModelPrice(input=15, output=75, cache_read=1.5, cache_write=18.75)
    u = Usage(
        input_tokens=1_000_000, output_tokens=0, cache_read_tokens=1_000_000, cache_write_tokens=0
    )
    assert cost_usd(u, price) == pytest.approx(16.5)
    assert cost_usd(
        Usage(output_tokens=2_000_000, cache_write_tokens=1_000_000), price
    ) == pytest.approx(168.75)


# ------------------------------------------------------------ real client
def _req() -> httpx2.Request:
    return httpx2.Request("POST", "https://api.anthropic.com/v1/messages")


def _status(cls: type[anthropic.APIStatusError], code: int) -> anthropic.APIStatusError:
    return cls("err", response=httpx2.Response(code, request=_req()), body=None)


def _message(text: str = '{"ok": true}') -> Any:
    return SimpleNamespace(
        content=[
            SimpleNamespace(type="thinking", thinking=""),
            SimpleNamespace(type="text", text=text),
        ],
        model="claude-opus-5-5",
        stop_reason="end_turn",
        usage=SimpleNamespace(
            input_tokens=10,
            output_tokens=5,
            cache_read_input_tokens=100,
            cache_creation_input_tokens=7,
        ),
    )


class FakeSDK:
    def __init__(self, outcomes: list[Any]) -> None:
        self.outcomes = outcomes
        self.calls: list[dict[str, Any]] = []
        self.messages = self

    def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        o = self.outcomes.pop(0)
        if isinstance(o, BaseException):
            raise o
        return o


def _client(outcomes: list[Any], attempts: int = 4) -> tuple[AnthropicClient, FakeSDK, list[float]]:
    sleeps: list[float] = []
    sdk = FakeSDK(outcomes)
    c = AnthropicClient(sdk=sdk, retry=RetryPolicy(max_attempts=attempts, sleep=sleeps.append))
    return c, sdk, sleeps


def test_retries_429_5xx_overloaded_and_connection_then_succeeds() -> None:
    c, sdk, sleeps = _client(
        [
            _status(anthropic.RateLimitError, 429),
            _status(anthropic.InternalServerError, 500),
            anthropic.APIConnectionError(request=_req()),
            _message(),
        ]
    )
    resp = c.complete(REQ)
    assert resp.text == '{"ok": true}'  # thinking blocks ignored
    assert resp.usage == Usage(10, 5, 100, 7)
    assert len(sdk.calls) == 4 and len(sleeps) == 3
    assert sleeps[0] < sleeps[2]  # exponential backoff
    assert sdk.calls[0]["timeout"] == REQ.timeout_s


def test_overloaded_is_retried() -> None:
    c, _sdk, _ = _client([_status(anthropic.OverloadedError, 529), _message()])
    assert c.complete(REQ).model_id == "claude-opus-5-5"


def test_gives_up_after_max_attempts() -> None:
    c, sdk, _ = _client([_status(anthropic.RateLimitError, 429)] * 3, attempts=3)
    with pytest.raises(LLMCallFailed, match="after 3 attempts"):
        c.complete(REQ)
    assert len(sdk.calls) == 3


def test_non_retryable_fails_fast() -> None:
    c, sdk, sleeps = _client([_status(anthropic.BadRequestError, 400), _message()])
    with pytest.raises(LLMCallFailed, match="non-retryable"):
        c.complete(REQ)
    assert len(sdk.calls) == 1 and not sleeps


def test_missing_key_and_repr_hide_secret() -> None:
    with pytest.raises(LLMCallFailed):
        AnthropicClient(api_key=None)
    c = AnthropicClient(api_key="sk-ant-secret-value")
    assert "secret" not in repr(c)


# ------------------------------------------------------------ test doubles
def test_recorded_client_replays_in_order() -> None:
    c = RecordedClient({"fundamentals": [{"a": 1}, "raw text"]})
    assert json.loads(c.complete(REQ).text) == {"a": 1}
    r2 = c.complete(REQ)
    assert r2.text == "raw text" and r2.usage.output_tokens >= 1 and r2.usage.cache_write_tokens > 0
    with pytest.raises(LLMCallFailed):
        c.complete(REQ)
    assert len(c.requests) == 3


def test_recording_client_saves(tmp_path: Path) -> None:
    inner = RecordedClient({"fundamentals": [{"x": 1}, "not json"]})
    rec = RecordingClient(inner, tmp_path / "rec.json")
    rec.complete(REQ)
    rec.complete(REQ)
    assert json.loads((tmp_path / "rec.json").read_text()) == {
        "fundamentals": [{"x": 1}, "not json"]
    }
