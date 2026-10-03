"""Substantially identical securities for the wash-sale rule.

Two symbols are substantially identical when they are the same symbol or sit
in the same equivalence group (for example two share classes of one issuer).
The harvest ``replacements`` map is deliberately *not* an equivalence: a
replacement ETF tracks a different index and is chosen to be correlated but
not substantially identical. A replacement pair that falls inside one group
is a configuration error and is rejected by ``check_replacements``.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

DEFAULT_EQUIVALENCE_GROUPS: tuple[frozenset[str], ...] = (
    frozenset({"GOOG", "GOOGL"}),
    frozenset({"BRK.A", "BRK.B"}),
    frozenset({"FOX", "FOXA"}),
    frozenset({"NWS", "NWSA"}),
)


def _norm(symbol: str) -> str:
    return symbol.strip().upper()


@dataclass(frozen=True)
class Equivalence:
    """Maps a symbol to the set of symbols treated as substantially identical."""

    groups: tuple[frozenset[str], ...] = DEFAULT_EQUIVALENCE_GROUPS
    _index: Mapping[str, frozenset[str]] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        index: dict[str, frozenset[str]] = {}
        for g in self.groups:
            norm = frozenset(_norm(s) for s in g)
            for s in norm:
                if s in index and index[s] != norm:
                    raise ValueError(f"symbol {s} is in two equivalence groups")
                index[s] = norm
        object.__setattr__(self, "_index", index)

    @classmethod
    def from_lists(cls, groups: Iterable[Iterable[str]]) -> Equivalence:
        return cls(tuple(frozenset(g) for g in groups))

    def members(self, symbol: str) -> frozenset[str]:
        s = _norm(symbol)
        return self._index.get(s, frozenset({s}))

    def key(self, symbol: str) -> str:
        """A canonical identity: the alphabetically first member of the group."""
        return min(self.members(symbol))

    def identical(self, a: str, b: str) -> bool:
        return self.key(a) == self.key(b)

    def check_replacements(self, replacements: Mapping[str, str]) -> list[str]:
        """Return problems: replacement pairs that are substantially identical."""
        return [
            f"replacement {src}->{dst} is substantially identical"
            for src, dst in sorted(replacements.items())
            if self.identical(src, dst)
        ]
