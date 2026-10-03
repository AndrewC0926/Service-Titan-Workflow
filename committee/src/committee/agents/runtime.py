"""Agent runtime: compose prompt -> call model -> validate -> one repair -> journal.

Every call attempt (including failed ones) is journaled as an ``agent_output``
entry with agent, model_id, prompt_hash, input_packet_hash, output, tokens,
cost, latency and run_id. Invalid output after one repair retry raises
``AgentOutputInvalid`` (fail closed).

Journal writes happen on the calling thread. For parallel steps, call with
``journal=False`` from worker threads and pass the returned records to
``journal_records`` on the main thread (SQLite connections are per-thread).
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from committee.agents.budget import BudgetGuard
from committee.agents.errors import AgentOutputInvalid
from committee.agents.llm import LLMClient, LLMRequest, LLMResponse, Usage, cost_usd
from committee.agents.packets import EvidencePacket
from committee.agents.prompts import PromptRegistry
from committee.agents.schemas import output_spec
from committee.config.schema import ModelsConfig
from committee.journal.store import Journal

T = TypeVar("T", bound=BaseModel)

_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)
MAX_ERROR_CHARS = 2000


def extract_json(text: str) -> Any:
    """Parse the JSON object in a model reply (tolerates code fences and stray prose)."""
    s = _FENCE.sub("", text.strip()).strip()
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        start, end = s.find("{"), s.rfind("}")
        if start == -1 or end <= start:
            raise
        return json.loads(s[start : end + 1])


@dataclass
class AgentResult[T: BaseModel]:
    agent: str
    output: T
    model_id: str
    prompt_hash: str
    packet_hash: str
    usage: Usage
    cost_usd: float
    attempts: int
    records: list[dict[str, Any]] = field(default_factory=list)


class AgentRuntime:
    def __init__(
        self,
        client: LLMClient,
        models: ModelsConfig,
        registry: PromptRegistry,
        *,
        run_id: str,
        journal: Journal | None = None,
        budget: BudgetGuard | None = None,
        benchmark: str = "SPY",
        cost_bps: int = 100,
        timeout_s: float = 120.0,
    ) -> None:
        self.client = client
        self.models = models
        self.registry = registry
        self.run_id = run_id
        self.journal = journal
        self.budget = budget
        self.benchmark = benchmark
        self.cost_bps = cost_bps
        self.timeout_s = timeout_s

    # --------------------------------------------------------------- helpers
    def spec_for(self, agent: str, schema: type[BaseModel]) -> str:
        return output_spec(agent, schema, self.registry.schema)  # type: ignore[arg-type]

    def check_budget(self) -> None:
        if self.budget is not None:
            self.budget.check()

    def journal_records(self, records: Sequence[Mapping[str, Any]]) -> None:
        if self.journal is None:
            return
        for r in records:
            self.journal.append("agent_output", dict(r))

    def _record(
        self,
        *,
        agent: str,
        tier: str,
        packet: EvidencePacket,
        prompt_hash: str,
        resp: LLMResponse,
        attempt: int,
        output: Any,
        error: str | None,
    ) -> dict[str, Any]:
        price = (
            self.models.pricing.get(resp.model_id)
            or self.models.pricing[self.models.tier_for(agent).model]
        )
        return {
            "run_id": self.run_id,
            "review_id": packet.review_id,
            "agent": agent,
            "model_tier": tier,
            "model_id": resp.model_id,
            "prompt_hash": prompt_hash,
            "input_packet_hash": packet.hash,
            "attempt": attempt,
            "status": "ok" if error is None else "invalid",
            "output": output,
            "validation_error": error,
            "raw_output_sha256": hashlib.sha256(resp.text.encode()).hexdigest(),
            "tokens": resp.usage.as_dict(),
            "cost_usd": round(cost_usd(resp.usage, price), 8),
            "latency_s": round(resp.latency_s, 4),
            "stop_reason": resp.stop_reason,
        }

    def _validate(
        self, schema: type[T], text: str, ctx: Mapping[str, Any]
    ) -> tuple[T | None, Any, str | None]:
        try:
            obj = extract_json(text)
        except (json.JSONDecodeError, ValueError) as e:
            return None, None, f"response is not valid JSON: {e}"
        try:
            return schema.model_validate(obj, context=dict(ctx)), obj, None
        except ValidationError as e:
            return None, obj, str(e)[:MAX_ERROR_CHARS]

    # ------------------------------------------------------------------ call
    def call(
        self,
        agent: str,
        packet: EvidencePacket,
        schema: type[T],
        *,
        context: Mapping[str, Any] | None = None,
        journal: bool = True,
    ) -> AgentResult[T]:
        if journal:
            self.check_budget()
        tier_name = self.models.agents[agent]  # type: ignore[index]
        tier = self.models.tiers[tier_name]
        spec = self.spec_for(agent, schema)
        composed = self.registry.compose(
            agent,
            spec,
            asof=packet.asof,
            anon_id=packet.anon_id,
            benchmark=self.benchmark,
            cost_bps=self.cost_bps,
        )
        ctx: dict[str, Any] = {
            "agent": agent,
            "anon_id": packet.anon_id,
            "evidence_ids": packet.evidence_ids,
            "bucket_tag": packet.bucket_tag,
        }
        if context:
            ctx.update(context)
        request = LLMRequest(
            agent=agent,
            model=tier.model,
            max_tokens=tier.max_tokens,
            temperature=tier.temperature,
            system_blocks=composed.system_blocks,
            packet=packet.render(),
            timeout_s=self.timeout_s,
        )
        records: list[dict[str, Any]] = []
        usage = Usage()
        error: str | None = None
        for attempt in (1, 2):
            resp = self.client.complete(request)
            usage = usage + resp.usage
            parsed, obj, error = self._validate(schema, resp.text, ctx)
            records.append(
                self._record(
                    agent=agent,
                    tier=tier_name,
                    packet=packet,
                    prompt_hash=composed.prompt_hash,
                    resp=resp,
                    attempt=attempt,
                    output=obj if isinstance(obj, dict | list) else None,
                    error=error,
                )
            )
            if parsed is not None:
                if journal:
                    self.journal_records(records)
                return AgentResult(
                    agent=agent,
                    output=parsed,
                    model_id=resp.model_id,
                    prompt_hash=composed.prompt_hash,
                    packet_hash=packet.hash,
                    usage=usage,
                    cost_usd=sum(float(r["cost_usd"]) for r in records),
                    attempts=attempt,
                    records=records,
                )
            request = LLMRequest(
                agent=request.agent,
                model=request.model,
                max_tokens=request.max_tokens,
                temperature=request.temperature,
                system_blocks=request.system_blocks,
                packet=request.packet,
                instruction=request.instruction,
                followups=(
                    ("assistant", resp.text or "(no output)"),
                    (
                        "user",
                        "Your previous output failed validation:\n"
                        f"{error}\nReturn ONLY a corrected JSON object.",
                    ),
                ),
                timeout_s=request.timeout_s,
            )
        if journal:
            self.journal_records(records)
        raise AgentOutputInvalid(agent, error or "unknown", records)
