"""Canonical JSON: one byte-stable encoding per value, so hashes are reproducible."""

from __future__ import annotations

import dataclasses
import datetime as dt
import enum
import json
import math
from decimal import Decimal
from pathlib import PurePath
from typing import Any

from pydantic import BaseModel


def _default(o: Any) -> Any:
    if isinstance(o, BaseModel):
        return o.model_dump(mode="json")
    if dataclasses.is_dataclass(o) and not isinstance(o, type):
        return dataclasses.asdict(o)
    if isinstance(o, dt.datetime):
        if o.tzinfo is None:
            raise ValueError("naive datetimes are not allowed in the journal; use UTC")
        return o.astimezone(dt.UTC).isoformat()
    if isinstance(o, dt.date):
        return o.isoformat()
    if isinstance(o, Decimal):
        return str(o)
    if isinstance(o, enum.Enum):
        return o.value
    if isinstance(o, PurePath):
        return str(o)
    if isinstance(o, set | frozenset):
        return sorted(o)
    raise TypeError(f"not JSON serializable: {type(o).__name__}")


def _check_floats(o: Any) -> None:
    if isinstance(o, float) and not math.isfinite(o):
        raise ValueError("NaN/Infinity cannot be journaled")
    if isinstance(o, dict):
        for v in o.values():
            _check_floats(v)
    elif isinstance(o, list | tuple):
        for v in o:
            _check_floats(v)


def canonical_json(value: Any) -> str:
    """Sorted keys, no insignificant whitespace, UTF-8, finite numbers only."""
    normalized = json.loads(json.dumps(value, default=_default, allow_nan=True))
    _check_floats(normalized)
    return json.dumps(
        normalized, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )
