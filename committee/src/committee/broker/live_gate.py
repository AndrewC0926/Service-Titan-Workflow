"""Live-trading gate: live mode needs BOTH the env flag and a signed live-gate file
whose hash is recorded in a journaled, human-signed ``live_gate`` entry (DESIGN 13)."""

from __future__ import annotations

import hashlib
from pathlib import Path

from committee.journal.store import Journal


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def live_allowed(flag_enabled: bool, gate_file: Path, journal: Journal) -> tuple[bool, str]:
    if not flag_enabled:
        return False, "LIVE_TRADING_ENABLED is not true"
    if not gate_file.exists():
        return False, f"live-gate file {gate_file} not found"
    h = file_sha256(gate_file)
    for e in journal.entries("live_gate"):
        if e.payload.get("file_sha256") == h and e.payload.get("signed_by_human") is True:
            return True, f"live gate signed at seq {e.seq}"
    return False, "live-gate file is not signed in the journal"
