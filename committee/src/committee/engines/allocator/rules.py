"""Capital allocator (DESIGN 3): resizes the satellite from evidence, quarterly.

- Freeze at the current size during the first 36 live (not paper) months.
- +5 points (cap 35%) only if ALL hold over >= 36 live months: deflated Sharpe of
  satellite active returns vs the factor-matched benchmark > 0.95; after-tax,
  after-cost cumulative excess > 0 vs BOTH SPY and the factor-matched benchmark;
  satellite Brier better than the base-rate forecaster; satellite max drawdown
  <= 1.5x the benchmark's.
- -5 points (floor 10%) if ANY hold: 24-month after-tax excess < -7% vs the
  factor-matched benchmark; Brier worse than base rate 4 straight quarters; two or
  more guardrail breaches in the quarter.
- Decrease wins over increase. Output is a recommendation; a human approves it and
  the new target goes through the 7-day config change control.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from committee.journal.store import Journal, JournalEntry

STEP = 5.0
FREEZE_MONTHS = 36


@dataclass(frozen=True)
class AllocatorInputs:
    live_months: int
    current_pct: float
    min_pct: float
    max_pct: float
    deflated_sharpe_vs_fmb: float | None
    cum_excess_after_tax_vs_spy: float | None
    cum_excess_after_tax_vs_fmb: float | None
    satellite_brier: float | None
    base_rate_brier: float | None
    satellite_max_dd: float | None  # positive fraction, e.g. 0.32
    benchmark_max_dd: float | None
    excess_24m_after_tax_vs_fmb: float | None
    quarters_brier_worse_than_base: int  # consecutive, most recent first
    guardrail_breaches_this_quarter: int


@dataclass(frozen=True)
class AllocatorDecision:
    action: str  # FREEZE | INCREASE | DECREASE | HOLD
    current_pct: float
    recommended_pct: float
    reasons: list[str] = field(default_factory=list)


def _known(*xs: float | None) -> bool:
    return all(x is not None for x in xs)


def decide(i: AllocatorInputs) -> AllocatorDecision:
    if i.live_months < FREEZE_MONTHS:
        return AllocatorDecision(
            "FREEZE",
            i.current_pct,
            i.current_pct,
            [f"{i.live_months} live months < {FREEZE_MONTHS}: size frozen regardless of results"],
        )
    down: list[str] = []
    if i.excess_24m_after_tax_vs_fmb is not None and i.excess_24m_after_tax_vs_fmb < -0.07:
        down.append(
            f"24-month after-tax excess vs factor-matched benchmark {i.excess_24m_after_tax_vs_fmb:.1%} < -7%"
        )
    if i.quarters_brier_worse_than_base >= 4:
        down.append(
            f"Brier worse than base rate for {i.quarters_brier_worse_than_base} straight quarters"
        )
    if i.guardrail_breaches_this_quarter >= 2:
        down.append(f"{i.guardrail_breaches_this_quarter} guardrail breaches this quarter")
    if down:
        new = max(i.min_pct, i.current_pct - STEP)
        return AllocatorDecision(
            "DECREASE" if new < i.current_pct else "HOLD", i.current_pct, new, down
        )
    checks: list[tuple[bool, str]] = []
    if not _known(
        i.deflated_sharpe_vs_fmb,
        i.cum_excess_after_tax_vs_spy,
        i.cum_excess_after_tax_vs_fmb,
        i.satellite_brier,
        i.base_rate_brier,
        i.satellite_max_dd,
        i.benchmark_max_dd,
    ):
        return AllocatorDecision(
            "HOLD", i.current_pct, i.current_pct, ["missing evidence: cannot justify an increase"]
        )
    assert i.deflated_sharpe_vs_fmb is not None and i.cum_excess_after_tax_vs_spy is not None
    assert i.cum_excess_after_tax_vs_fmb is not None and i.satellite_brier is not None
    assert (
        i.base_rate_brier is not None
        and i.satellite_max_dd is not None
        and i.benchmark_max_dd is not None
    )
    checks.append(
        (i.deflated_sharpe_vs_fmb > 0.95, f"deflated Sharpe {i.deflated_sharpe_vs_fmb:.3f} > 0.95")
    )
    checks.append(
        (
            i.cum_excess_after_tax_vs_spy > 0,
            f"after-tax excess vs SPY {i.cum_excess_after_tax_vs_spy:.1%} > 0",
        )
    )
    checks.append(
        (
            i.cum_excess_after_tax_vs_fmb > 0,
            f"after-tax excess vs factor-matched {i.cum_excess_after_tax_vs_fmb:.1%} > 0",
        )
    )
    checks.append(
        (
            i.satellite_brier < i.base_rate_brier,
            f"Brier {i.satellite_brier:.4f} < base rate {i.base_rate_brier:.4f}",
        )
    )
    checks.append(
        (
            i.satellite_max_dd <= 1.5 * i.benchmark_max_dd,
            f"max drawdown {i.satellite_max_dd:.1%} <= 1.5x benchmark {i.benchmark_max_dd:.1%}",
        )
    )
    failed = [t for ok, t in checks if not ok]
    if not failed:
        new = min(i.max_pct, i.current_pct + STEP)
        return AllocatorDecision(
            "INCREASE" if new > i.current_pct else "HOLD",
            i.current_pct,
            new,
            [t for _, t in checks],
        )
    return AllocatorDecision(
        "HOLD", i.current_pct, i.current_pct, ["not met: " + t for t in failed]
    )


def journal_decision(
    journal: Journal, inputs: AllocatorInputs, decision: AllocatorDecision
) -> JournalEntry:
    """Every allocator decision is a journal entry with the inputs that produced it."""
    return journal.append(
        "allocator_decision",
        {
            "inputs": inputs.__dict__,
            "action": decision.action,
            "current_pct": decision.current_pct,
            "recommended_pct": decision.recommended_pct,
            "reasons": decision.reasons,
            "requires_human_approval": decision.recommended_pct != decision.current_pct,
        },
    )
