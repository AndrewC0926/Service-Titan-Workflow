"""LLM client layer: one Protocol, a real Anthropic implementation and test doubles.

Every agent call goes through ``LLMClient.complete``. The real client:

- uses the pinned model id and max_tokens from models.yaml (never an alias);
- puts ``cache_control: ephemeral`` on the shared preamble (first system block)
  and on the evidence packet (first user block), so both are prompt-cached;
- retries 429 / 5xx / overloaded / connection errors with exponential backoff
  and jitter, under a per-request timeout;
- returns token usage including cache read/write tokens, for cost accounting.

Temperature: config pins 0. The Anthropic SDK 1.x removed sampling parameters
from ``messages.create`` and current-generation models (Opus 4.7+, Sonnet 5.x)
reject them, so temperature is sent (via ``extra_body``) only to models that
still accept it. Determinism on newer models comes from pinned ids, fixed
prompts and canonical packets; outputs are journaled for exact replay.
"""

from __future__ import annotations

import json
import random
import re
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from committee.agents.errors import LLMCallFailed
from committee.config.schema import ModelPrice

CACHE = {"type": "ephemeral"}

# Models that still accept sampling parameters (Claude 4.6 line and earlier).
_SAMPLING_OK = re.compile(r"^claude-(?:(?:opus|sonnet|haiku)-4-[0-6]|3)")


def accepts_sampling(model: str) -> bool:
    return bool(_SAMPLING_OK.match(model))


@dataclass(frozen=True)
class LLMRequest:
    """A fully specified, cache-friendly request.

    system_blocks[0] is the composed shared preamble (cached); the rest are the
    agent's own instructions. ``packet`` is the evidence packet text (cached).
    ``followups`` are extra (role, text) turns, used for the repair retry.
    """

    agent: str
    model: str
    max_tokens: int
    temperature: float
    system_blocks: tuple[str, ...]
    packet: str
    instruction: str = "Return the JSON object now."
    followups: tuple[tuple[str, str], ...] = ()
    timeout_s: float = 120.0


@dataclass(frozen=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0

    def __add__(self, other: Usage) -> Usage:
        return Usage(
            self.input_tokens + other.input_tokens,
            self.output_tokens + other.output_tokens,
            self.cache_read_tokens + other.cache_read_tokens,
            self.cache_write_tokens + other.cache_write_tokens,
        )

    def as_dict(self) -> dict[str, int]:
        return {
            "input": self.input_tokens,
            "output": self.output_tokens,
            "cache_read": self.cache_read_tokens,
            "cache_write": self.cache_write_tokens,
        }


@dataclass(frozen=True)
class LLMResponse:
    text: str
    model_id: str
    usage: Usage
    stop_reason: str | None = None
    latency_s: float = 0.0


class LLMClient(Protocol):
    def complete(self, request: LLMRequest) -> LLMResponse: ...


def cost_usd(usage: Usage, price: ModelPrice) -> float:
    """USD cost from token usage and per-MTok pricing (input excludes cached tokens)."""
    return (
        usage.input_tokens * price.input
        + usage.output_tokens * price.output
        + usage.cache_read_tokens * price.cache_read
        + usage.cache_write_tokens * price.cache_write
    ) / 1_000_000


def build_messages_params(request: LLMRequest) -> dict[str, Any]:
    """The exact ``messages.create`` kwargs for a request (pure; unit-tested)."""
    system: list[dict[str, Any]] = []
    for i, text in enumerate(request.system_blocks):
        block: dict[str, Any] = {"type": "text", "text": text}
        if i == 0:
            block["cache_control"] = dict(CACHE)
        system.append(block)
    messages: list[dict[str, Any]] = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": request.packet, "cache_control": dict(CACHE)},
                {"type": "text", "text": request.instruction},
            ],
        }
    ]
    for role, text in request.followups:
        messages.append({"role": role, "content": text})
    params: dict[str, Any] = {
        "model": request.model,
        "max_tokens": request.max_tokens,
        "system": system,
        "messages": messages,
    }
    if accepts_sampling(request.model):
        params["extra_body"] = {"temperature": request.temperature}
    return params


# ------------------------------------------------------------------ real client
@dataclass
class RetryPolicy:
    max_attempts: int = 5
    base_delay_s: float = 1.0
    max_delay_s: float = 30.0
    sleep: Callable[[float], None] = time.sleep
    rng: random.Random = field(default_factory=lambda: random.Random(0))  # noqa: S311 (jitter)

    def delay(self, attempt: int) -> float:
        d = min(self.max_delay_s, self.base_delay_s * (2**attempt))
        return float(d * (0.5 + self.rng.random() / 2))


def _is_retryable(exc: BaseException) -> bool:
    import anthropic

    if isinstance(exc, anthropic.APIConnectionError):  # includes APITimeoutError
        return True
    if isinstance(exc, anthropic.RateLimitError | anthropic.OverloadedError):
        return True
    if isinstance(exc, anthropic.APIStatusError):
        return exc.status_code >= 500 or exc.status_code in (408, 409)
    return False


class AnthropicClient:
    """Thin wrapper over ``anthropic.Anthropic`` with our retry/backoff policy.

    The SDK's own retries are disabled (max_retries=0) so the policy here is the
    single source of truth and is testable with an injected ``sdk`` object.
    """

    def __init__(
        self,
        api_key: str | None = None,
        *,
        sdk: Any | None = None,
        retry: RetryPolicy | None = None,
    ) -> None:
        if sdk is None:
            import anthropic

            if not api_key:
                raise LLMCallFailed("ANTHROPIC_API_KEY is not configured")
            sdk = anthropic.Anthropic(api_key=api_key, max_retries=0)
        self._sdk = sdk
        self.retry = retry or RetryPolicy()

    def __repr__(self) -> str:  # never expose the key
        return "AnthropicClient(<configured>)"

    def complete(self, request: LLMRequest) -> LLMResponse:
        params = build_messages_params(request)
        last: BaseException | None = None
        for attempt in range(self.retry.max_attempts):
            t0 = time.monotonic()
            try:
                msg = self._sdk.messages.create(**params, timeout=request.timeout_s)
            except Exception as exc:
                if not _is_retryable(exc):
                    raise LLMCallFailed(
                        f"{request.agent}: non-retryable API error {type(exc).__name__}"
                    ) from exc
                last = exc
                if attempt + 1 < self.retry.max_attempts:
                    self.retry.sleep(self.retry.delay(attempt))
                continue
            latency = time.monotonic() - t0
            text = "".join(
                getattr(b, "text", "") for b in msg.content if getattr(b, "type", "") == "text"
            )
            u = msg.usage
            usage = Usage(
                input_tokens=int(u.input_tokens or 0),
                output_tokens=int(u.output_tokens or 0),
                cache_read_tokens=int(getattr(u, "cache_read_input_tokens", 0) or 0),
                cache_write_tokens=int(getattr(u, "cache_creation_input_tokens", 0) or 0),
            )
            return LLMResponse(
                text=text,
                model_id=str(msg.model),
                usage=usage,
                stop_reason=getattr(msg, "stop_reason", None),
                latency_s=latency,
            )
        raise LLMCallFailed(
            f"{request.agent}: API call failed after {self.retry.max_attempts} attempts "
            f"({type(last).__name__ if last else 'unknown'})"
        ) from last


# ----------------------------------------------------------------- test doubles
def _approx_tokens(text: str) -> int:
    return max(1, len(text) // 4)


class RecordedClient:
    """Replays recorded responses per agent, in order. No network.

    ``responses`` maps agent name to a list of responses; each is either a JSON
    object (served as canonical text) or a raw string (served verbatim, e.g. to
    exercise the repair path). Usage is approximated from text length so cost
    accounting is exercised too.
    """

    def __init__(self, responses: Mapping[str, Sequence[Any]], model_override: str | None = None):
        self._queues: dict[str, list[Any]] = {k: list(v) for k, v in responses.items()}
        self._model_override = model_override
        self.requests: list[LLMRequest] = []

    def complete(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        queue = self._queues.get(request.agent)
        if not queue:
            raise LLMCallFailed(f"{request.agent}: no recorded response left")
        item = queue.pop(0)
        text = item if isinstance(item, str) else json.dumps(item, sort_keys=True)
        prefix = sum(_approx_tokens(b) for b in request.system_blocks[:1]) + _approx_tokens(
            request.packet
        )
        rest = sum(_approx_tokens(b) for b in request.system_blocks[1:]) + sum(
            _approx_tokens(t) for _, t in request.followups
        )
        return LLMResponse(
            text=text,
            model_id=self._model_override or request.model,
            usage=Usage(
                input_tokens=rest,
                output_tokens=_approx_tokens(text),
                cache_read_tokens=0,
                cache_write_tokens=prefix,
            ),
            stop_reason="end_turn",
        )


class RecordingClient:
    """Wraps a live client and saves every response text per agent to a JSON file."""

    def __init__(self, inner: LLMClient, path: Path) -> None:
        self.inner = inner
        self.path = path
        self.recorded: dict[str, list[Any]] = {}

    def complete(self, request: LLMRequest) -> LLMResponse:
        resp = self.inner.complete(request)
        try:
            item: Any = json.loads(resp.text)
        except json.JSONDecodeError:
            item = resp.text
        self.recorded.setdefault(request.agent, []).append(item)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.recorded, indent=1, sort_keys=True))
        return resp
