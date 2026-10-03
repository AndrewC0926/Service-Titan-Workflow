"""Form 3/4/5 ownership XML -> ``insider_txns`` rows.

One row per reporting owner per transaction line (non-derivative and derivative
tables). Holdings-only filings yield zero rows. The 10b5-1 flag comes from the
``aff10b5One`` checkbox when it is checked, otherwise from any footnote the
transaction references (or the filing remarks) mentioning Rule 10b5-1.
Anything malformed raises ``Form4ParseError`` so the caller can dead-letter it.
"""

from __future__ import annotations

import datetime as dt
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Any

_TEN_B5_1 = re.compile(r"10\s*b\s*5\s*-\s*1|10b-5-1", re.IGNORECASE)
_TRUE = {"1", "true", "y", "yes"}


class Form4ParseError(ValueError):
    pass


@dataclass(frozen=True)
class Owner:
    cik: str
    name: str
    is_director: bool
    is_officer: bool
    is_ten_pct: bool
    is_other: bool
    officer_title: str | None

    @property
    def role(self) -> str:
        roles = [
            r
            for r, on in (
                ("officer", self.is_officer),
                ("director", self.is_director),
                ("ten_pct_owner", self.is_ten_pct),
                ("other", self.is_other),
            )
            if on
        ]
        return ",".join(roles) or "other"


def _strip_ns(root: ET.Element) -> None:
    for el in root.iter():
        if isinstance(el.tag, str) and "}" in el.tag:
            el.tag = el.tag.split("}", 1)[1]


def _text(el: ET.Element | None, path: str) -> str | None:
    if el is None:
        return None
    node = el.find(path)
    if node is None or node.text is None:
        return None
    t = node.text.strip()
    return t or None


def _value(el: ET.Element, path: str) -> str | None:
    """Ownership XML wraps most fields as <field><value>x</value></field>."""
    return _text(el, f"{path}/value") or _text(el, path)


def _flag(el: ET.Element | None, path: str) -> bool:
    return (_text(el, path) or "").lower() in _TRUE


def _float(s: str | None, field: str, required: bool = False) -> float | None:
    if s is None:
        if required:
            raise Form4ParseError(f"missing {field}")
        return None
    try:
        return float(s.replace(",", ""))
    except ValueError:
        raise Form4ParseError(f"bad number in {field}: {s!r}") from None


def _date(s: str | None, field: str) -> dt.date:
    if not s:
        raise Form4ParseError(f"missing {field}")
    try:
        return dt.date.fromisoformat(s[:10])
    except ValueError:
        raise Form4ParseError(f"bad date in {field}: {s!r}") from None


def parse_xml(body: bytes) -> ET.Element:
    upper = body.upper()  # the whole body: a long prolog must not hide a DTD
    if b"<!DOCTYPE" in upper or b"<!ENTITY" in upper:
        raise Form4ParseError("DTD/entity declarations are not allowed")
    try:
        root = ET.fromstring(body)  # noqa: S314 - DTDs rejected above; SEC ownership XML
    except ET.ParseError as e:
        raise Form4ParseError(f"XML parse error: {e}") from None
    _strip_ns(root)
    return root


def _owners(root: ET.Element) -> list[Owner]:
    owners = []
    for ro in root.findall("reportingOwner"):
        cik = _text(ro, "reportingOwnerId/rptOwnerCik")
        if not cik:
            raise Form4ParseError("reporting owner without CIK")
        rel = ro.find("reportingOwnerRelationship")
        owners.append(
            Owner(
                cik=cik.lstrip("0") or "0",
                name=_text(ro, "reportingOwnerId/rptOwnerName") or "",
                is_director=_flag(rel, "isDirector"),
                is_officer=_flag(rel, "isOfficer"),
                is_ten_pct=_flag(rel, "isTenPercentOwner"),
                is_other=_flag(rel, "isOther"),
                officer_title=_text(rel, "officerTitle"),
            )
        )
    if not owners:
        raise Form4ParseError("no reportingOwner")
    return owners


def parse_form4(
    body: bytes,
    *,
    accession: str,
    form: str,
    accepted_at: dt.datetime,
    security_id: str,
) -> list[dict[str, Any]]:
    """Parse ownership XML into insider_txns rows (empty list if holdings-only)."""
    root = parse_xml(body)
    if root.tag != "ownershipDocument":
        raise Form4ParseError(f"unexpected root element {root.tag!r}")
    issuer_cik = _text(root, "issuer/issuerCik")
    if not issuer_cik:
        raise Form4ParseError("missing issuerCik")
    owners = _owners(root)
    checkbox = _flag(root, "aff10b5One")
    notes = {
        fn.get("id", ""): "".join(fn.itertext())
        for fn in root.findall("footnotes/footnote")
        if fn.get("id")
    }
    remarks_10b51 = bool(_TEN_B5_1.search(_text(root, "remarks") or ""))
    is_amendment = form.upper().endswith("/A")

    lines: list[dict[str, Any]] = []
    tables = (
        ("nonDerivativeTable/nonDerivativeTransaction", False),
        ("derivativeTable/derivativeTransaction", True),
    )
    for path, is_deriv in tables:
        for tx in root.findall(path):
            code = _text(tx, "transactionCoding/transactionCode")
            if not code:
                raise Form4ParseError("transaction without transactionCode")
            ad = _value(tx, "transactionAmounts/transactionAcquiredDisposedCode")
            if ad not in ("A", "D"):
                raise Form4ParseError(f"bad acquired/disposed code {ad!r}")
            refs = {f.get("id", "") for f in tx.iter("footnoteId")}
            lines.append(
                {
                    "txn_code": code,
                    "acquired_disposed": ad,
                    "shares": _float(
                        _value(tx, "transactionAmounts/transactionShares"), "shares", True
                    ),
                    "price": _float(
                        _value(tx, "transactionAmounts/transactionPricePerShare"), "price"
                    ),
                    "shares_owned_after": _float(
                        _value(tx, "postTransactionAmounts/sharesOwnedFollowingTransaction"),
                        "sharesOwnedFollowingTransaction",
                    ),
                    "txn_date": _date(_value(tx, "transactionDate"), "transactionDate"),
                    "is_derivative": is_deriv,
                    "is_10b5_1": checkbox
                    or remarks_10b51
                    or any(_TEN_B5_1.search(notes.get(r, "")) for r in refs),
                }
            )

    rows: list[dict[str, Any]] = []
    for owner in owners:
        for ln in lines:
            rows.append(
                {
                    "accession": accession,
                    "line": len(rows),
                    "cik": int(issuer_cik),
                    "security_id": security_id,
                    "insider_id": owner.cik,
                    "insider_name": owner.name,
                    "role": owner.role,
                    "is_officer": owner.is_officer,
                    "is_director": owner.is_director,
                    "officer_title": owner.officer_title,
                    **ln,
                    "is_amendment": is_amendment,
                    "filed_at": accepted_at,
                    "event_time": dt.datetime.combine(ln["txn_date"], dt.time(), tzinfo=dt.UTC),
                    "known_time": accepted_at,
                }
            )
    return rows
