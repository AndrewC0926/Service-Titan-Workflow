"""Fixture review packets with recorded agent responses (dry-run and eval harness).

A fixture file ``fx_NNN.json`` holds the raw as-of evidence for one review (fed
through ``FixtureSource`` so it gets the same anonymization, wrapping and date
shifting as live data), stub engine outputs, and recorded responses per agent.
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from committee.agents.engine_inputs import EngineInputs
from committee.agents.packets import FixtureSource, ReviewPacket, build_review_packet


@dataclass(frozen=True)
class Fixture:
    fixture_id: str
    description: str
    review: ReviewPacket
    engines: EngineInputs
    responses: dict[str, list[Any]]
    raw: dict[str, Any]


def build_fixture_review(data: dict[str, Any]) -> tuple[ReviewPacket, EngineInputs]:
    engines = EngineInputs.model_validate(data["engines"])
    review = build_review_packet(
        FixtureSource(data),
        data["security"]["security_id"],
        dt.date.fromisoformat(data["asof"]),
        data["review_id"],
        bucket_tag=engines.bucket_tag,
        context=data.get("context", {}),
        extras=engines.as_evidence(),
    )
    return review, engines


def load_fixture(path: Path) -> Fixture:
    data = json.loads(path.read_text(encoding="utf-8"))
    review, engines = build_fixture_review(data)
    return Fixture(
        fixture_id=data["fixture_id"],
        description=data.get("description", ""),
        review=review,
        engines=engines,
        responses={k: list(v) for k, v in data.get("responses", {}).items()},
        raw=data,
    )


def fixture_path(directory: Path, fixture_id: str) -> Path:
    p = directory / f"{fixture_id}.json"
    if not p.exists():
        raise FileNotFoundError(f"fixture {fixture_id!r} not found in {directory}")
    return p


def list_fixtures(directory: Path) -> list[Path]:
    return sorted(directory.glob("fx_*.json"))
