"""Prompt registry: prompts live as files under ``prompts/`` and are versioned by hash.

Files: ``preamble.md`` (shared preamble with {asof}, {anon_id}, {benchmark},
{cost_bps} placeholders), ``schema.json`` (shared analyst output schema) and
one ``<agent>.md`` per agent, all copied verbatim from DESIGN section 7.

The prompt hash covers the preamble template, the agent prompt and the output
specification appended by code, so any change to what the model is told starts
a new evaluation cohort (DESIGN 10/11).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from committee.config.schema import AGENT_NAMES
from committee.journal.store import Journal

PLACEHOLDERS = ("asof", "anon_id", "benchmark", "cost_bps")


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class AgentPrompt:
    agent: str
    text: str

    @property
    def hash(self) -> str:
        return sha256_text(self.text)


@dataclass(frozen=True)
class ComposedPrompt:
    agent: str
    system_blocks: tuple[str, ...]
    prompt_hash: str


class PromptRegistry:
    def __init__(self, preamble: str, schema: str, agents: dict[str, AgentPrompt]) -> None:
        missing = set(AGENT_NAMES) - set(agents)
        if missing:
            raise ValueError(f"prompts missing for agents: {sorted(missing)}")
        for p in PLACEHOLDERS:
            if "{" + p + "}" not in preamble:
                raise ValueError(f"preamble lacks placeholder {{{p}}}")
        self.preamble = preamble
        self.schema = schema
        self.agents = agents

    @classmethod
    def load(cls, directory: Path) -> PromptRegistry:
        if not directory.is_dir():
            raise FileNotFoundError(f"prompt directory not found: {directory}")
        preamble = (directory / "preamble.md").read_text(encoding="utf-8")
        schema = (directory / "schema.json").read_text(encoding="utf-8")
        agents = {
            a: AgentPrompt(a, (directory / f"{a}.md").read_text(encoding="utf-8"))
            for a in AGENT_NAMES
            if (directory / f"{a}.md").exists()
        }
        return cls(preamble, schema, agents)

    def render_preamble(self, *, asof: str, anon_id: str, benchmark: str, cost_bps: int) -> str:
        values = {"asof": asof, "anon_id": anon_id, "benchmark": benchmark, "cost_bps": cost_bps}
        out = self.preamble
        for k, v in values.items():
            out = out.replace("{" + k + "}", str(v))
        return out

    def prompt_hash(self, agent: str, output_spec: str) -> str:
        return sha256_text("\n\x1e".join([self.preamble, self.agents[agent].text, output_spec]))

    def compose(
        self,
        agent: str,
        output_spec: str,
        *,
        asof: str,
        anon_id: str,
        benchmark: str,
        cost_bps: int,
    ) -> ComposedPrompt:
        preamble = self.render_preamble(
            asof=asof, anon_id=anon_id, benchmark=benchmark, cost_bps=cost_bps
        )
        agent_block = self.agents[agent].text + "\nOUTPUT FORMAT\n" + output_spec
        return ComposedPrompt(agent, (preamble, agent_block), self.prompt_hash(agent, output_spec))

    def versions(self, output_specs: dict[str, str]) -> dict[str, str]:
        return {a: self.prompt_hash(a, output_specs[a]) for a in self.agents}

    def register_trials(self, journal: Journal, output_specs: dict[str, str]) -> list[str]:
        """Journal a ``trial`` entry for every prompt version not seen before (DESIGN 10)."""
        seen: set[tuple[Any, Any]] = {
            (e.payload.get("agent"), e.payload.get("prompt_hash"))
            for e in journal.entries("trial")
            if e.payload.get("kind") == "prompt_version"
        }
        new: list[str] = []
        for agent, h in self.versions(output_specs).items():
            if (agent, h) not in seen:
                journal.append(
                    "trial", {"kind": "prompt_version", "agent": agent, "prompt_hash": h}
                )
                new.append(agent)
        return new
