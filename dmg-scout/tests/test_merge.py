"""Merging two project rows that are the same project.

Retired, not deleted: the duplicate keeps its id and history at status='merged',
which is outside ACTIVE_STATUSES, so it leaves every board and every rate without
a DELETE. The cases that matter are the ones that would abort a merge halfway —
a link the survivor already has would violate a unique constraint and take the
whole thing down.
"""
import pytest
from sqlmodel import select

from app.merge import MERGED_STATUS, choose_survivor, merge_duplicate_groups, merge_projects
from app.models import (
    ACTIVE_STATUSES,
    Category,
    Contact,
    MatchCandidate,
    Project,
    ProjectContact,
    ProjectSignal,
    Signal,
    SignalType,
    Stage,
)


def _project(session, name, **kw):
    p = Project(name=name, category=kw.pop("category", Category.industrial),
                county=kw.pop("county", "Riverside"), state="CA",
                stage=kw.pop("stage", Stage.entitlement), status="active", **kw)
    session.add(p)
    session.commit()
    return p


def _signal(session, name="Sig"):
    s = Signal(raw_document_id=None, signal_type=SignalType.ceqa_nop,
               category=Category.industrial, project_name=name, county="Riverside",
               state="CA", stage=Stage.entitlement, summary_one_line="x")
    session.add(s)
    session.commit()
    return s


def _link(session, project, signal, method="direct"):
    ps = ProjectSignal(project_id=project.id, signal_id=signal.id,
                       match_confidence=1.0, match_method=method)
    session.add(ps)
    session.commit()
    return ps


def test_duplicate_is_retired_not_deleted(db_session, cfg):
    a, b = _project(db_session, "Same Project"), _project(db_session, "Same Project")
    merge_projects(db_session, a, b)

    assert db_session.get(Project, b.id) is not None, "the row was deleted"
    assert b.status == MERGED_STATUS
    assert b.status not in ACTIVE_STATUSES, "a merged row would still count on the board"
    assert f"#{a.id}" in b.notes and f"#{b.id}" in a.notes


def test_signals_move_to_the_survivor(db_session, cfg):
    a, b = _project(db_session, "P"), _project(db_session, "P")
    s1, s2 = _signal(db_session, "one"), _signal(db_session, "two")
    _link(db_session, a, s1)
    _link(db_session, b, s2)

    merge_projects(db_session, a, b)

    moved = db_session.exec(select(ProjectSignal)
                            .where(ProjectSignal.project_id == a.id)).all()
    assert {m.signal_id for m in moved} == {s1.id, s2.id}
    assert not db_session.exec(select(ProjectSignal)
                               .where(ProjectSignal.project_id == b.id)).all()


def test_a_link_the_survivor_already_has_is_dropped_not_repointed(db_session, cfg):
    """The #961/#963 shape: both rows link the SAME signal.

    Repointing would violate uq_project_signal and abort the merge, so the
    redundant row is dropped instead.
    """
    a, b = _project(db_session, "P"), _project(db_session, "P")
    shared = _signal(db_session, "shared")
    _link(db_session, a, shared)
    _link(db_session, b, shared)

    result = merge_projects(db_session, a, b)

    links = db_session.exec(select(ProjectSignal)
                            .where(ProjectSignal.signal_id == shared.id)).all()
    assert len(links) == 1
    assert links[0].project_id == a.id
    assert result["dropped_as_duplicate_links"]["project_signals"] == 1


def test_contacts_dedupe_on_role(db_session, cfg):
    a, b = _project(db_session, "P"), _project(db_session, "P")
    c = Contact(name="Dana Reyes", company="City")
    db_session.add(c)
    db_session.commit()
    db_session.add(ProjectContact(project_id=a.id, contact_id=c.id, role="developer"))
    db_session.add(ProjectContact(project_id=b.id, contact_id=c.id, role="developer"))
    db_session.add(ProjectContact(project_id=b.id, contact_id=c.id, role="gc"))
    db_session.commit()

    merge_projects(db_session, a, b)

    roles = {pc.role for pc in db_session.exec(
        select(ProjectContact).where(ProjectContact.project_id == a.id)).all()}
    assert roles == {"developer", "gc"}


def test_plain_tables_repoint_wholesale(db_session, cfg):
    a, b = _project(db_session, "P"), _project(db_session, "P")
    s = _signal(db_session)
    db_session.add(MatchCandidate(signal_id=s.id, project_id=b.id, similarity=0.6,
                                  llm_verdict="uncertain", llm_reasoning="x"))
    db_session.commit()

    merge_projects(db_session, a, b)

    mcs = db_session.exec(select(MatchCandidate)).all()
    assert [m.project_id for m in mcs] == [a.id]


def test_survivor_absorbs_gaps_but_keeps_known_values(db_session, cfg):
    a = _project(db_session, "P", developer="Known Developer")
    b = _project(db_session, "P", developer="Other Developer",
                 stage=Stage.construction)
    b.sch_number = "2026010975"
    b.mw_it = 40.0
    a.mw_it = 10.0
    db_session.add_all([a, b])
    db_session.commit()

    merge_projects(db_session, a, b)

    assert a.developer == "Known Developer", "overwrote a known value"
    assert a.sch_number == "2026010975", "did not fill a gap"
    assert a.mw_it == 40.0, "did not take the larger MW"
    assert a.stage == Stage.construction, "stage did not move forward"


def test_refuses_to_merge_across_boards(db_session, cfg):
    a = _project(db_session, "P", category=Category.data_center)
    b = _project(db_session, "P", category=Category.industrial)
    with pytest.raises(ValueError, match="across boards"):
        merge_projects(db_session, a, b)


def test_refuses_to_merge_into_itself_or_remerge(db_session, cfg):
    a, b = _project(db_session, "P"), _project(db_session, "P")
    with pytest.raises(ValueError, match="into itself"):
        merge_projects(db_session, a, a)
    merge_projects(db_session, a, b)
    with pytest.raises(ValueError, match="already merged"):
        merge_projects(db_session, a, b)


def test_survivor_is_the_row_with_more_evidence(db_session, cfg):
    a, b = _project(db_session, "P"), _project(db_session, "P")
    _link(db_session, b, _signal(db_session, "one"))
    _link(db_session, b, _signal(db_session, "two"))
    _link(db_session, a, _signal(db_session, "three"))

    survivor, loser = choose_survivor(a, b, db_session)
    assert survivor.id == b.id and loser.id == a.id


def test_dry_run_changes_nothing(db_session, cfg):
    a = _project(db_session, "First Industrial Commerce Center II")
    b = _project(db_session, "First Industrial Commerce Center II")
    a.sch_number = b.sch_number = "2026010975"
    db_session.add_all([a, b])
    db_session.commit()

    plans = merge_duplicate_groups(db_session, dry_run=True)
    assert len(plans) == 1
    assert all(p.status == "active" for p in (a, b)), "dry run mutated the database"

    merge_duplicate_groups(db_session, dry_run=False)
    statuses = sorted(p.status for p in (a, b))
    assert statuses == ["active", MERGED_STATUS]


def test_merged_rows_leave_the_duplicate_report(db_session, cfg):
    """The point of the whole exercise: the board stops reporting the group."""
    from app.duplicates import find_duplicates

    a = _project(db_session, "Dupe Co")
    b = _project(db_session, "Dupe Co")
    a.sch_number = b.sch_number = "2026010975"
    db_session.add_all([a, b])
    db_session.commit()
    assert find_duplicates(db_session)["n_groups"] == 1

    merge_duplicate_groups(db_session, dry_run=False)
    assert find_duplicates(db_session)["n_groups"] == 0
