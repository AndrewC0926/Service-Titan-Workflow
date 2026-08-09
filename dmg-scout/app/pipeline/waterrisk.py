"""Water-source risk read for data center projects.

The public objection at an entitlement hearing is to POTABLE water, not water in
general. A cooling tower drawing on recycled/reclaimed supply is a different
argument at the podium than one drawing on municipal potable — and Los Angeles
in particular has extensive purple-pipe (recycled water) infrastructure a
project can sit on. A binary "water contested" flag erases exactly the
distinction that decides whether an evaporative design survives entitlement, so
this does not build one. It captures the underlying facts (stated source,
whether recycled/reclaimed supply is identified, any stated water-use/WUE
figure) and derives a read from those, not from "contested" alone.

Why this matters for THIS territory specifically, not water risk in general:
LA County's installed data center base is large and nearly static — 776.5 MW
at roughly 0.27% CAGR (as stated to this system, not independently measured
here) — so there is almost nothing new there to generate a water fight over in
the first place. Storey County, NV, the other anchor of this territory, mostly
entitles data centers over the counter, which often produces no public hearing
at all. The water fight, where it happens, is loudest in the markets this
territory does not sell into and quietest in the ones it does. That does not
make the signal worthless — a project can still die to a water objection in
LA — it means the read below should inform confidence project-by-project, not
shape a territory-wide prior that this market is systematically water-risky.

This is a CONFIDENCE caveat, never a ranking input. Water controversy is a
reason a project dies before it buys anything, not a reason to chase it harder
— nothing here touches app.pipeline.scoring.priority_score or Project.score.
See Project.water_risk_flag / water_risk_basis, set in
app.pipeline.resolve._absorb, and the "elevated" case rendered as a caveat on
the project page — same pattern as Project.estimate_low_confidence, never
blended into the number that orders the board.
"""
from __future__ import annotations

# "noise": opposition is stated, but a recycled/reclaimed supply is identified,
#   so the objection most likely to survive contact with the actual filing (to
#   POTABLE use) does not appear to apply. Worth noting, not worth discounting.
# "elevated": opposition is stated with NO recycled/reclaimed supply on file to
#   answer it. That is a real, unrebutted objection — genuinely downgrade
#   confidence that this project proceeds as filed.
RISK_NOISE = "noise"
RISK_ELEVATED = "elevated"


def water_risk_read(reclaimed_identified: bool | None,
                    opposition_stated: bool | None) -> tuple[str | None, str | None]:
    """(flag, basis) from what the filings actually state. Null unless
    opposition is stated at all — this never fires on a quiet project, because
    a quiet project has nothing to downgrade confidence about."""
    if not opposition_stated:
        return None, None
    if reclaimed_identified:
        return RISK_NOISE, (
            "Water opposition is stated on this project, but a recycled/reclaimed supply is "
            "identified for the site. Public objections are almost always to POTABLE water use "
            "specifically, and this project does not appear to rely on it — treat the "
            "opposition as mostly noise, not a reason to discount confidence."
        )
    return RISK_ELEVATED, (
        "Water opposition is stated on this project with NO recycled/reclaimed supply "
        "identified anywhere in the filings on file. That potable-water objection has nothing "
        "on record to rebut it. Downgrades confidence that this project proceeds as filed — "
        "never a reason to rank it lower or higher; it is a viability caveat, not a score input."
    )
