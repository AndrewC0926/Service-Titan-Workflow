"""Entity-name normalization for matching. 'Vantage Data Centers NV11, LLC' -> 'vantage data centers'."""
from __future__ import annotations

import re

_LEGAL_SUFFIXES = [
    "llc", "inc", "incorporated", "corp", "corporation", "co", "company",
    "lp", "llp", "ltd", "limited", "holdings", "partners", "trust",
]
_ROMAN = re.compile(r"\b[ivxlcdm]{1,6}\b")
_PHASE = re.compile(r"\b(phase|bldg|building|campus|site|parcel)\s*[\w-]{0,4}\b")
# Trailing project/SPE designators like "NV11", "PY A", "CA-2", "DC3"
_SPE_CODE = re.compile(r"\b[a-z]{1,3}[\s-]?\d{1,4}[a-z]?\b")


def normalize_name(name: str) -> str:
    s = name.lower().strip()
    s = s.replace(".", "")                   # collapse dotted abbreviations: L.P. -> lp
    s = re.sub(r"[^\w\s-]", " ", s)          # punctuation
    for suf in _LEGAL_SUFFIXES:
        s = re.sub(rf"\b{re.escape(suf)}\b", " ", s)
    s = _PHASE.sub(" ", s)
    s = _SPE_CODE.sub(" ", s)
    s = _ROMAN.sub(" ", s)
    s = re.sub(r"[\s-]+", " ", s).strip()
    return s


def normalize_county(county: str | None) -> str | None:
    if not county:
        return None
    c = county.strip()
    c = re.sub(r"\s+county$", "", c, flags=re.IGNORECASE)
    return c.title()
