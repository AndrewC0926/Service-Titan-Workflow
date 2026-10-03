"""Daily anchor: the latest hash written to a dated file (and emailed).

The anchor directory should be a separate, object-locked cloud bucket mount;
tampering would then require altering both the journal and the anchor.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from committee.journal.store import Journal

EmailHook = Callable[[str, str], None]


def _no_email(subject: str, body: str) -> None:
    """Stub: real delivery is wired in ops (Prompt 16)."""


def write_anchor(journal: Journal, anchor_dir: Path, email: EmailHook = _no_email) -> Path:
    anchor = journal.export_anchor()
    anchor_dir.mkdir(parents=True, exist_ok=True)
    day = str(anchor["anchored_at_utc"])[:10]
    path = anchor_dir / f"anchor-{day}.json"
    if path.exists():  # several anchors in a day: never overwrite, add a suffix
        i = 2
        while (anchor_dir / f"anchor-{day}-{i}.json").exists():
            i += 1
        path = anchor_dir / f"anchor-{day}-{i}.json"
    path.write_text(json.dumps(anchor, indent=2, sort_keys=True) + "\n")
    email(f"Committee journal anchor {day}", json.dumps(anchor, sort_keys=True))
    journal.append(
        "anchor",
        {"file": path.name, "anchored_seq": anchor["seq"], "anchored_hash": anchor["hash"]},
    )
    return path


def check_anchors(journal: Journal, anchor_dir: Path) -> list[str]:
    """Every anchored (seq, hash) must still be present in the journal."""
    problems: list[str] = []
    for f in sorted(anchor_dir.glob("anchor-*.json")):
        a: dict[str, Any] = json.loads(f.read_text())
        if a["seq"] == 0:
            continue
        e = journal.get(int(a["seq"]))
        if e is None or e.hash != a["hash"]:
            problems.append(f"{f.name}: anchored seq {a['seq']} no longer matches the journal")
    return problems
