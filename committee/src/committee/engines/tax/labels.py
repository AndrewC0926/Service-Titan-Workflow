"""The label every human-facing tax output carries (DESIGN 9)."""

from __future__ import annotations

DISCLAIMER = "Not tax advice. Confirm with a CPA."


def labeled(text: str) -> str:
    """Append the disclaimer to an explanation string (idempotent)."""
    return text if DISCLAIMER in text else f"{text} {DISCLAIMER}"
