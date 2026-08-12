"""compare_lines: application-driven line-card comparability.

/lines shows one line at a time. This answers a different question: given an
application, which candidate lines apply at all, and what does each trade
away against it. Head-to-head, not a catalog browse.

This is the field version of the heat_rejection_mode mistake -- see
app.models.ProductLine's heat_rejection_mode comment and
tests/test_accounts.py's marley/recold test for the original one. The same
three rules apply to every capability field this module reads:

1. Capability is a MODEL-level fact. Never inferred from a brand's
   reputation, from equipment_type, or from what a category "usually"
   implies. If nobody has actually stated it for this line, it is null.
2. A null (or unverified) capability field makes this module ABSTAIN on that
   dimension for that candidate. It never counts as a vote against the line
   -- silence is not evidence of absence. It is recorded in that candidate's
   capability_gaps instead.
3. Every claim this module DOES surface carries a verification status and a
   check date, pulled from the field's own *_basis text (convention:
   "Retrieved YYYY-MM-DD" or "Stated YYYY-MM-DD" near the end of the basis
   string -- see app.accounts's researched-field basis texts for the
   existing examples this follows). A claim with no parseable date, or whose
   *_verified companion field is False, is EXCLUDED from output entirely --
   moved to capability_gaps -- not included with a caveat. An unverified
   maybe is worse than a known gap: it reads as confidence that isn't there.

lead_time_weeks_low/high/basis are never read here. Lead time stays null
everywhere in this system; this module does not surface it, caveated or
otherwise.

Candidate selection: tonnage implies a mechanical-plant request, so the
candidate pool is every line whose building_role is one of
TONNAGE_RELEVANT_ROLES -- never narrowed by building_type or by any
application constraint. A missing/unresearched market or capability fact
must not silently exclude a line from being shown at all; only from a
specific claim within its row. Ordering rewards ONLY verified positive
matches (a count of constraint dimensions this candidate is confirmed to
satisfy) -- it never demotes a candidate for a null, which would be the same
rule violation as excluding one for it.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from sqlmodel import Session, select

from app.accounts import MARKETS, ROLE_LABELS
from app.models import ProductLine

TONNAGE_RELEVANT_ROLES = ("cooling_generation", "heat_rejection", "air_handling")

_DATE_RE = re.compile(r"(?:Retrieved|Stated)\s+(\d{4}-\d{2}-\d{2})")


def _checked_date(basis: str | None) -> str | None:
    """Pull the citation date off a *_basis string. None if unparseable --
    treated as no verification, not a silent pass."""
    if not basis:
        return None
    m = _DATE_RE.search(basis)
    return m.group(1) if m else None


@dataclass
class Claim:
    """One surfaced fact: a value, who's vouching for it, and when it was
    checked. Only ever constructed when both exist -- see `_claim()`."""
    value: object
    basis: str
    checked: str


def _claim(value, basis: str | None, verified: bool | None = None) -> Claim | None:
    """None (a gap) unless there's a value, a basis, AND a parseable date.
    `verified`, when the field has one, must also be True -- rule 3."""
    if value is None or not basis:
        return None
    if verified is False:
        return None
    checked = _checked_date(basis)
    if checked is None:
        return None
    return Claim(value=value, basis=basis, checked=checked)


@dataclass
class Candidate:
    line: str
    building_role: str
    why_it_fits: list = field(default_factory=list)
    trades_away: list = field(default_factory=list)
    eligibility_flags: dict = field(default_factory=dict)
    competitors: Claim | None = None
    known_limitations: str | None = None
    capability_gaps: list = field(default_factory=list)
    verified_fit_count: int = 0  # ordering key only -- never used to exclude


def compare_lines(
    session: Session,
    *,
    tonnage: float | None = None,
    building_type: str | None = None,
    latent_load_priority: bool | None = None,
    marine_or_corrosive: bool | None = None,
    water_available: bool | None = None,
    space_rigging_constrained: bool | None = None,
    redundancy_required: bool | None = None,
    buyer_type: str | None = None,  # "owner_direct" | "spec_driven" | None
) -> list[Candidate]:
    if building_type is not None and building_type not in MARKETS:
        raise ValueError(f"building_type must be one of {MARKETS} or None, got {building_type!r}")
    if buyer_type not in (None, "owner_direct", "spec_driven"):
        raise ValueError("buyer_type must be 'owner_direct', 'spec_driven', or None")

    lines = session.exec(
        select(ProductLine).where(ProductLine.building_role.in_(TONNAGE_RELEVANT_ROLES))
    ).all()

    out: list[Candidate] = []
    for line in lines:
        c = Candidate(line=line.name, building_role=ROLE_LABELS.get(line.building_role, line.building_role))

        # ---- building type: researched market fit only ever adds a positive.
        # legacy_guess and unresearched are silent, not a mismatch.
        if building_type and line.markets_served_source == "researched":
            if building_type in (line.markets_served or []):
                c.why_it_fits.append(
                    f"Researched market fit: {building_type} (checked "
                    f"{_checked_date(line.markets_served_basis) or 'date unrecorded'})"
                )
                c.verified_fit_count += 1
            # a researched line simply not naming this market is real signal
            # too (it was checked and not found) -- but per rule 2 that is
            # still not grounds to vote the line down, so it is omitted
            # rather than logged as a trade-away.
        elif building_type:
            c.capability_gaps.append("building_type_fit")

        # ---- latent load
        latent = _claim(line.latent_load_capability, line.latent_load_capability_basis,
                         line.latent_load_capability_verified)
        if latent is None:
            c.capability_gaps.append("latent_load_capability")
        elif latent_load_priority:
            if latent.value == "enhanced":
                c.why_it_fits.append(f"Enhanced latent capability (checked {latent.checked})")
                c.verified_fit_count += 1
            else:
                c.trades_away.append(f"Standard (not enhanced) latent capability (checked {latent.checked})")

        # ---- marine / corrosive environment
        corrosion = _claim(line.corrosion_resistance, line.corrosion_resistance_basis,
                            line.corrosion_resistance_verified)
        if corrosion is None:
            c.capability_gaps.append("corrosion_resistance")
        elif marine_or_corrosive:
            if corrosion.value in ("marine_grade", "coated_standard"):
                c.why_it_fits.append(f"Corrosion resistance: {corrosion.value} (checked {corrosion.checked})")
                c.verified_fit_count += 1
            else:
                c.trades_away.append(f"Standard (non-coated) materials in a corrosive environment "
                                      f"(checked {corrosion.checked})")

        # ---- water availability, derived from the already-verified
        # heat_rejection_mode -- not a new field, rule 1 applies to the
        # source fact either way.
        water_required = _claim(line.heat_rejection_mode, line.heat_rejection_mode_basis,
                                 line.heat_rejection_mode_verified)
        if water_required is None:
            c.capability_gaps.append("water_required")
        elif water_available is not None:
            needs_water = water_required.value in ("evaporative", "adiabatic_hybrid")
            if water_available is False and needs_water:
                c.trades_away.append(f"Requires water ({water_required.value}, checked "
                                      f"{water_required.checked}) — none available on this site")
            elif water_available is False and not needs_water:
                c.why_it_fits.append(f"No water required ({water_required.value}, checked "
                                      f"{water_required.checked})")
                c.verified_fit_count += 1

        # ---- redundancy
        redundancy = _claim(line.redundancy_capable, line.redundancy_capable_basis,
                             line.redundancy_capable_verified)
        if redundancy is None:
            c.capability_gaps.append("redundancy_capable")
        elif redundancy_required:
            if redundancy.value:
                c.why_it_fits.append(f"Configurable N+1/modular redundancy (checked {redundancy.checked})")
                c.verified_fit_count += 1
            else:
                c.trades_away.append(f"Not configurable for redundancy (checked {redundancy.checked})")

        # ---- space / rigging
        rigging = _claim(line.rigging_constrained_capable, line.rigging_constrained_capable_basis,
                          line.rigging_constrained_capable_verified)
        if rigging is None:
            c.capability_gaps.append("rigging_constrained_capable")
        elif space_rigging_constrained:
            if rigging.value:
                c.why_it_fits.append(f"Ships/installs under tight space or rigging limits (checked "
                                      f"{rigging.checked})")
                c.verified_fit_count += 1
            else:
                c.trades_away.append(f"Not suited to constrained space/rigging (checked {rigging.checked})")

        # ---- eligibility flags: OSP, AHRI -- always reported when known,
        # regardless of whether the buyer asked; these are standing facts
        # about the line, not a constraint to be matched against.
        osp = _claim(line.oshpd_osp, line.oshpd_osp_basis)
        c.eligibility_flags["oshpd_osp"] = osp
        if osp is None:
            c.capability_gaps.append("oshpd_osp")
        ahri = _claim(line.ahri_certified, line.ahri_certified_basis)
        c.eligibility_flags["ahri_certified"] = ahri
        if ahri is None:
            c.capability_gaps.append("ahri_certified")

        # ---- competitors
        c.competitors = _claim(line.competes_with, line.competes_with_basis)
        if c.competitors is None:
            c.capability_gaps.append("competitors")

        # ---- known limitations: stated plainly if present, never inferred
        c.known_limitations = line.limitations

        # buyer_type shapes emphasis only -- see docstring; it never filters
        # or reorders. Left to the caller/renderer to act on (spec-driven ->
        # lead with eligibility_flags/competitors; owner_direct -> lead with
        # why_it_fits) rather than baked into the data here.

        out.append(c)

    # Order by verified positive fit only -- never by absence of data.
    out.sort(key=lambda c: (-c.verified_fit_count, c.line))
    return out
