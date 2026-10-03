"""System flags that gate order submission: kill switch, freeze, read-only.

Flags are files under var/flags so any process (CLI, dashboard, scheduler)
sees the same state. Setting or clearing a flag is journaled by the caller.
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass
from pathlib import Path

FLAG_NAMES = ("kill_switch", "frozen")


@dataclass(frozen=True)
class Flags:
    dir: Path

    def _p(self, name: str) -> Path:
        if name not in FLAG_NAMES:
            raise ValueError(name)
        return self.dir / f"{name}.json"

    def set(self, name: str, reason: str) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        self._p(name).write_text(
            json.dumps({"reason": reason, "set_at_utc": dt.datetime.now(dt.UTC).isoformat()})
        )

    def clear(self, name: str) -> None:
        self._p(name).unlink(missing_ok=True)

    def is_set(self, name: str) -> bool:
        return self._p(name).exists()

    def reason(self, name: str) -> str | None:
        p = self._p(name)
        if not p.exists():
            return None
        return str(json.loads(p.read_text()).get("reason"))

    def orders_blocked(self) -> str | None:
        """Why order submission is blocked, or None if it is allowed."""
        for n in FLAG_NAMES:
            if self.is_set(n):
                return f"{n}: {self.reason(n)}"
        return None
