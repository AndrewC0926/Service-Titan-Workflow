"""Service life lookup: is this equipment due, and on whose calendar.

The replacement module (charter product B) does not exist yet. This is the piece
of it that could be settled now, because the constants came out of a customer
conversation and constants are worth writing down while the conversation is fresh.

The one idea here is that **ASHRAE service life cannot be applied uniformly**.
Private commercial owners run to failure, so ASHRAE's median is the right
predictor. Federal and government owners replace on a planned 7-10 year cycle
because there is little to no service agreement business — replacement is
programmed, not condition-based. Those are different mechanisms, not different
numbers for the same mechanism, which is why this branches rather than scaling.

Concretely: a federal rooftop at year 8 is a live candidate and a commercial one
is not until year 15. Scoring a federal portfolio on ASHRAE numbers would show
nothing due until year 15, by which point the job was bought and installed years
earlier.
"""
from __future__ import annotations

from dataclasses import dataclass

from app.config import Config

OWNERSHIP_TYPES = ("federal", "state_municipal", "private_commercial")


class UnknownEquipment(KeyError):
    """No service life on file for this equipment type."""


@dataclass
class ServiceLife:
    low: int
    high: int
    ownership: str
    equipment: str
    basis: str
    source: str
    verified: bool

    @property
    def is_quotable(self) -> bool:
        """Whether a figure may be put in front of a customer.

        Nothing in this table is verified yet: the private-commercial rows have
        not been checked against a published ASHRAE edition, the federal rows are
        one contractor's field report, and the state/municipal rows are
        interpolation with no source at all. The flag exists so that stays
        visible when the replacement module starts consuming this.
        """
        return self.verified

    def due(self, age_years: float) -> bool:
        """Is equipment of this age inside the replacement window?"""
        return age_years >= self.low

    def status(self, age_years: float) -> str:
        if age_years >= self.high:
            return "overdue"
        if age_years >= self.low:
            return "due"
        if age_years >= self.low - 2:
            return "approaching"
        return "not_due"


def service_life(cfg: Config, equipment: str, ownership: str | None = None) -> ServiceLife:
    """Expected service life for `equipment` under `ownership`.

    An unknown ownership falls back to the configured default, which is
    private_commercial — the LONGEST cycle, deliberately. Guessing short would put
    buildings on a call list years before anyone is buying, and a rep who is early
    twice stops trusting the list. Missing a federal building costs one
    opportunity; inventing live ones costs the list its credibility.
    """
    table = cfg.get("replacement.service_life", {}) or {}
    owners = table.get("ownership", {}) or {}
    default = table.get("default_ownership", "private_commercial")

    key = ownership if ownership in owners else default
    entry = owners.get(key)
    if entry is None:
        raise UnknownEquipment(f"no service life table for ownership {key!r}")

    band = (entry.get("equipment") or {}).get(equipment)
    if band is None:
        # Never substitute another equipment type's life. A boiler is not a
        # rooftop, and a plausible wrong number here becomes a call at the wrong
        # time on a building nobody is replacing.
        raise UnknownEquipment(
            f"no service life for equipment {equipment!r} under {key!r}; "
            f"known: {', '.join(sorted((entry.get('equipment') or {}).keys()))}")

    return ServiceLife(
        low=int(band["low"]), high=int(band["high"]), ownership=key,
        equipment=equipment, basis=str(entry.get("basis", "")),
        source=str(entry.get("source", "unsourced")),
        verified=bool(entry.get("verified", False)),
    )


def replacement_basis(sl: ServiceLife, age_years: float) -> str:
    """One line a rep can read out, carrying the source with the number.

    The source travels with the figure for the same reason it does on
    equipment_value: a contractor's field report and an ASHRAE median are not the
    same kind of claim, and a board that prints them identically invites someone
    to quote the weaker one.
    """
    return (f"{age_years:.0f}yr old {sl.equipment.replace('_', ' ')} under "
            f"{sl.ownership.replace('_', ' ')} ownership; expected life "
            f"{sl.low}-{sl.high}yr -> {sl.status(age_years).replace('_', ' ')} "
            f"[{'VERIFIED' if sl.verified else 'UNVERIFIED'}: {sl.source}]")
