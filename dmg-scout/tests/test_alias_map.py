"""alias_map() must be exactly canonical_developer(), one query instead of N.

The developer blocking key in _blocked_candidates scanned the whole active board
and resolved each row's developer with its own indexed lookup — one database
round trip per project, per signal. Against a remote database that dominated
resolve's wall clock and got worse as the board grew.

These tests pin the equivalence, not the speed: whatever canonical_developer()
answers for a name, canonical_with(alias_map(...)) must answer the same. The
interesting case is an alias learned mid-run, which a cached map would miss.
"""
from sqlmodel import select

from app.models import DeveloperAlias
from app.pipeline.resolve import (
    alias_map,
    canonical_developer,
    canonical_with,
    seed_aliases,
)


def _add(session, canonical, alias, norm):
    session.add(DeveloperAlias(canonical=canonical, alias=alias, alias_norm=norm))
    session.commit()


def test_matches_canonical_developer_for_every_alias(db_session, cfg):
    seed_aliases(db_session, cfg)
    aliases = alias_map(db_session)
    rows = db_session.exec(select(DeveloperAlias)).all()
    assert rows, "config seeded no aliases; this test would prove nothing"
    for row in rows:
        assert canonical_with(aliases, row.alias) == canonical_developer(db_session, row.alias)


def test_unknown_name_passes_through_unchanged(db_session, cfg):
    aliases = alias_map(db_session)
    name = "Some Developer Nobody Has Aliased LLC"
    assert canonical_with(aliases, name) == name
    assert canonical_with(aliases, name) == canonical_developer(db_session, name)


def test_none_and_empty_are_none(db_session, cfg):
    aliases = alias_map(db_session)
    for empty in (None, ""):
        assert canonical_with(aliases, empty) is None
        assert canonical_with(aliases, empty) == canonical_developer(db_session, empty)


def test_alias_learned_mid_run_is_visible(db_session, cfg):
    """The reason the map is rebuilt per call rather than cached for the run.

    _learn_alias() adds rows while resolve is iterating. A map built once at the
    top of the run would stop matching them, silently.
    """
    from app.normalize import normalize_name

    name = "Tech Core PY B, LLC"
    before = alias_map(db_session)
    assert canonical_with(before, name) == name  # not yet aliased

    _add(db_session, "Tract Development", name, normalize_name(name))

    after = alias_map(db_session)
    assert canonical_with(after, name) == "Tract Development"
    assert canonical_with(after, name) == canonical_developer(db_session, name)


def test_uncommitted_alias_is_visible_via_autoflush(db_session, cfg):
    """Matches canonical_developer(), which sees pending rows through autoflush."""
    from app.normalize import normalize_name

    name = "Pending Alias Holdings LLC"
    db_session.add(DeveloperAlias(canonical="Real Owner", alias=name,
                                  alias_norm=normalize_name(name)))
    # deliberately no commit
    assert canonical_with(alias_map(db_session), name) == "Real Owner"
    assert canonical_developer(db_session, name) == "Real Owner"


def test_blocked_candidates_query_count_does_not_grow_with_the_board(db_session, cfg):
    """The regression itself: cost per signal must be flat in board size.

    Before the fix this grew by one query per active project, which is what made
    resolve slower every hour it ran. Asserting flatness rather than an absolute
    count, so the test survives someone adding an unrelated blocking key.
    """
    from sqlalchemy import event

    from app.models import Category, Project, Signal, SignalType, Stage
    from app.pipeline.resolve import _blocked_candidates

    def count_queries_with(n_projects: int) -> int:
        for i in range(n_projects):
            db_session.add(Project(name=f"Filler {i}", category=Category.data_center,
                                   developer=f"Developer {i} LLC", county="Storey",
                                   state="NV", status="active", stage=Stage.entitlement))
        db_session.commit()

        sig = Signal(raw_document_id=None, signal_type=SignalType.ceqa_nop,
                     category=Category.data_center, project_name="Probe",
                     developer_or_owner="Probe Developer LLC", county="Storey",
                     state="NV", stage=Stage.entitlement, summary_one_line="x")
        db_session.add(sig)
        db_session.commit()

        engine = db_session.get_bind()
        n = 0

        def on_exec(*_a, **_k):
            nonlocal n
            n += 1

        event.listen(engine, "before_cursor_execute", on_exec)
        try:
            _blocked_candidates(db_session, sig, radius_km=5)
        finally:
            event.remove(engine, "before_cursor_execute", on_exec)
        return n

    small = count_queries_with(5)
    large = count_queries_with(60)   # 12x the board
    assert large <= small + 2, (
        f"query count grew with the board: {small} at 5 projects, {large} at 65. "
        "The developer blocking key is doing a lookup per project again."
    )


def test_blocked_candidates_still_blocks_on_a_shared_alias(db_session, cfg):
    """End to end: the optimised path must still surface the aliased project."""
    from app.models import Category, Project, Signal, SignalType, Stage
    from app.normalize import normalize_name
    from app.pipeline.resolve import _blocked_candidates

    _add(db_session, "Tract Development", "Tech Core PY B, LLC",
         normalize_name("Tech Core PY B, LLC"))
    p = Project(name="Tract DC", category=Category.data_center,
                developer="Tract Development", county="Storey", state="NV",
                status="active", stage=Stage.entitlement)
    db_session.add(p)
    db_session.commit()

    sig = Signal(raw_document_id=None, signal_type=SignalType.ceqa_nop,
                 category=Category.data_center, project_name="Tract DC Phase 2",
                 developer_or_owner="Tech Core PY B, LLC", county="Storey",
                 state="NV", stage=Stage.entitlement, summary_one_line="x")
    db_session.add(sig)
    db_session.commit()

    got = _blocked_candidates(db_session, sig, radius_km=5)
    assert p.id in {c.id for c in got}
