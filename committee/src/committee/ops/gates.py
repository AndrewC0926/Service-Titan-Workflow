"""Live-trading gate check (DESIGN 13, Prompt 18). Never enables live trading.

Evaluates each criterion with evidence from the journal and repo, writes
docs/LIVE_GATE.md, and — only if every gate needed for live core (G0-G2) passes —
writes an UNSIGNED live-gate file. A human signs it (`committee gate sign`), which
journals a ``live_gate`` entry with the file hash; the human then sets
LIVE_TRADING_ENABLED=true by hand.
"""

from __future__ import annotations

import datetime as dt
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from committee.broker.live_gate import file_sha256
from committee.journal.store import Journal

BURN_IN_DAYS = 90
P1_QUIET_DAYS = 60
MIN_REVIEWS = 30


@dataclass
class Criterion:
    text: str
    ok: bool
    evidence: str


@dataclass
class Gate:
    id: str
    name: str
    criteria: list[Criterion] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return bool(self.criteria) and all(c.ok for c in self.criteria)


def _notes(journal: Journal, kind: str) -> list[dict[str, object]]:
    return [e.payload for e in journal.entries("note") if e.payload.get("kind") == kind]


def evaluate(journal: Journal, root: Path, now: dt.datetime) -> list[Gate]:
    gates: list[Gate] = []
    # ---- G0 build complete
    g0 = Gate("G0", "Build complete")
    review = root / "docs" / "REVIEW.md"
    if review.exists():
        txt = review.read_text()
        open_items = len(re.findall(r"(?im)^\s*[-|].*\bstatus\b\W*open\b", txt)) + len(
            re.findall(r"(?i)\|\s*open\s*\|", txt)
        )
        g0.criteria.append(
            Criterion(
                "Red-team review exists with all findings closed",
                open_items == 0,
                f"docs/REVIEW.md, {open_items} open",
            )
        )
    else:
        g0.criteria.append(
            Criterion(
                "Red-team review exists with all findings closed", False, "docs/REVIEW.md missing"
            )
        )
    ci = _notes(journal, "ci_green")
    g0.criteria.append(
        Criterion(
            "CI green on the release commit (journal note kind=ci_green)",
            bool(ci),
            f"{len(ci)} ci_green notes",
        )
    )
    gates.append(g0)

    # ---- G1 operational burn-in (paper)
    g1 = Gate("G1", "Operational burn-in (paper)")
    orders = list(journal.entries("order"))
    paper = [o for o in orders if o.payload.get("mode") == "paper"]
    first = paper[0].created_at if paper else None
    days = (now - first).days if first else 0
    g1.criteria.append(
        Criterion(
            f"{BURN_IN_DAYS} days of paper operation",
            days >= BURN_IN_DAYS,
            f"first paper order {first.date() if first else 'none'} ({days} days)",
        )
    )
    filled_ids = {e.payload.get("broker_order_id") for e in journal.entries("fill")}
    placed = [
        o
        for o in paper
        if o.payload.get("status") not in ("rejected", "canceled", "manual_ticket")
        and o.payload.get("broker_order_id")
    ]
    recon = sum(1 for o in placed if o.payload["broker_order_id"] in filled_ids)
    g1.criteria.append(
        Criterion(
            "100% fill reconciliation",
            bool(placed) and recon == len(placed),
            f"{recon}/{len(placed)} placed paper orders have journaled fills",
        )
    )
    p1 = [
        e
        for e in journal.entries("incident")
        if e.payload.get("level") == "P1" and now - e.created_at <= dt.timedelta(days=P1_QUIET_DAYS)
    ]
    g1.criteria.append(
        Criterion(
            f"Zero P1 incidents in the final {P1_QUIET_DAYS} days",
            not p1,
            f"{len(p1)} P1 incidents",
        )
    )
    rep = journal.verify()
    anchors_days = {
        e.created_at.date()
        for e in journal.entries("anchor")
        if now - e.created_at <= dt.timedelta(days=BURN_IN_DAYS)
    }
    g1.criteria.append(
        Criterion(
            "Journal verifies; daily anchors during burn-in",
            rep.ok and len(anchors_days) >= BURN_IN_DAYS - 5,
            f"verify {'OK' if rep.ok else 'FAILED'}; anchors on {len(anchors_days)} of the last {BURN_IN_DAYS} days",
        )
    )
    reviews = sum(1 for _ in journal.entries("briefing")) + sum(
        1 for e in journal.entries("state_transition") if e.payload.get("to") == "VETOED"
    )
    g1.criteria.append(
        Criterion(
            f"At least {MIN_REVIEWS} committee reviews logged",
            reviews >= MIN_REVIEWS,
            f"{reviews} reviews",
        )
    )
    gates.append(g1)

    # ---- G2 investment policy statement
    g2 = Gate("G2", "Investment policy statement")
    ips = _notes(journal, "ips_signed")
    g2.criteria.append(
        Criterion(
            "Written IPS signed (journal note kind=ips_signed)", bool(ips), f"{len(ips)} entries"
        )
    )
    adv = _notes(journal, "adviser_review")
    g2.criteria.append(
        Criterion(
            "Adviser and CPA review noted (kind=adviser_review with adviser and cpa)",
            any(a.get("adviser") and a.get("cpa") for a in adv),
            f"{len(adv)} entries",
        )
    )
    gates.append(g2)
    return gates


def render(gates: list[Gate], now: dt.datetime) -> str:
    lines = [
        "# Live-trading gate check",
        "",
        f"Generated {now:%Y-%m-%d %H:%M} UTC. This report never enables live trading.",
        "",
    ]
    for g in gates:
        lines += [
            f"## {g.id} {g.name}: {'PASS' if g.ok else 'FAIL'}",
            "",
            "| Criterion | Result | Evidence |",
            "|---|---|---|",
        ]
        lines += [f"| {c.text} | {'PASS' if c.ok else 'FAIL'} | {c.evidence} |" for c in g.criteria]
        lines.append("")
    allok = all(g.ok for g in gates)
    lines += [
        "## Next step",
        "",
        "All gates for live core pass. An unsigned live-gate file was written; review it, then run "
        "`committee gate sign`. Set LIVE_TRADING_ENABLED=true yourself afterwards. G3+ (satellite live) follow "
        "their own waiting periods."
        if allok
        else "Not ready for real money. Passing a gate is about operations and discipline, not returns.",
    ]
    return "\n".join(lines) + "\n"


def run_gate_check(
    journal: Journal, root: Path, gate_file: Path, now: dt.datetime
) -> tuple[list[Gate], Path]:
    gates = evaluate(journal, root, now)
    out = root / "docs" / "LIVE_GATE.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render(gates, now))
    if all(g.ok for g in gates):
        gate_file.parent.mkdir(parents=True, exist_ok=True)
        gate_file.write_text(
            json.dumps(
                {
                    "generated_at_utc": now.isoformat(),
                    "gates": {g.id: g.ok for g in gates},
                    "journal_head": journal.export_anchor(),
                    "scope": "live core (G3); satellite stays paper until G4",
                },
                indent=2,
                sort_keys=True,
            )
        )
    elif gate_file.exists():
        gate_file.unlink()  # a stale gate file must never outlive a failing check
    return gates, out


def sign(journal: Journal, gate_file: Path, signer: str, statement: str) -> str:
    """Journal the human signature over the gate file's hash."""
    if not gate_file.exists():
        raise FileNotFoundError("no live-gate file: run the gate check first")
    data = json.loads(gate_file.read_text())
    if not all(data.get("gates", {}).values()):
        raise ValueError("gate file records a failing gate")
    if len(statement.strip()) < 20:
        raise ValueError("write a signing statement (at least one sentence)")
    h = file_sha256(gate_file)
    journal.append(
        "live_gate",
        {
            "file_sha256": h,
            "signed_by_human": True,
            "signer": signer,
            "statement": statement.strip(),
        },
    )
    return h
