"""A digest that did not send must not mark anything as sent.

build_digest committed a DigestLog row for every item as it collected it, and
send_digest ran afterwards. An SMTP timeout or a 500 from Resend therefore left
every item in that digest permanently marked as reported — and "already reported"
is the only thing DigestLog means, so those items never reappear. A lead would
vanish between two runs with nothing anywhere to say it had existed.

The failure direction is now deliberate: a send that succeeds and a commit that
then fails repeats a few items next time, which a human notices and shrugs at.
Losing the lead is silent, and this system's entire value is that somebody hears
about the project.
"""
import pytest
from sqlmodel import select

from app.models import Category, DigestLog, Project, Stage, Window
from app.pipeline.notify import build_digest, run_notify


@pytest.fixture()
def board(db_session):
    for i in range(3):
        db_session.add(Project(name=f"Campus {i}", category=Category.data_center,
                               stage=Stage.entitlement, status="active", score=0.8,
                               in_territory=True, window=Window.PRE_BOD,
                               county="Los Angeles", state="CA"))
    db_session.commit()
    return db_session


def _sent(session) -> list[DigestLog]:
    return session.exec(select(DigestLog)).all()


def test_failed_send_reports_nothing_as_sent(board, cfg, monkeypatch):
    """The bug. A send failure must leave every item still unreported."""
    monkeypatch.setattr("app.pipeline.notify.send_digest",
                        lambda cfg, body: (_ for _ in ()).throw(
                            RuntimeError("resend returned 500")))
    with pytest.raises(RuntimeError, match="resend returned 500"):
        run_notify(board, cfg)
    assert _sent(board) == []


def test_items_survive_a_failed_send_and_go_out_next_run(board, cfg, monkeypatch):
    """The consequence that matters: the lead is still there tomorrow."""
    monkeypatch.setattr("app.pipeline.notify.send_digest",
                        lambda cfg, body: (_ for _ in ()).throw(RuntimeError("boom")))
    with pytest.raises(RuntimeError):
        run_notify(board, cfg)

    bodies = []
    monkeypatch.setattr("app.pipeline.notify.send_digest",
                        lambda cfg, body: bodies.append(body) or "console")
    result = run_notify(board, cfg)
    assert result["sent"] is True
    assert result["new_projects"] == 3
    for i in range(3):
        assert f"Campus {i}" in bodies[0]


def test_successful_send_records_what_went_out(board, cfg, monkeypatch):
    monkeypatch.setattr("app.pipeline.notify.send_digest", lambda cfg, body: "console")
    result = run_notify(board, cfg)
    assert result["sent"] is True
    kinds = {(d.kind, d.ref_id) for d in _sent(board)}
    assert len([k for k in kinds if k[0] == "new_project"]) == 3


def test_second_run_repeats_nothing(board, cfg, monkeypatch):
    monkeypatch.setattr("app.pipeline.notify.send_digest", lambda cfg, body: "console")
    assert run_notify(board, cfg)["new_projects"] == 3
    assert run_notify(board, cfg)["sent"] is False   # nothing new to say


def test_build_digest_leaves_its_marks_uncommitted(board, cfg):
    """Nothing has been reported to a human at this point — only written down."""
    body, stats = build_digest(board, cfg)
    assert stats["new_projects"] == 3
    board.rollback()
    assert _sent(board) == []


def test_empty_digest_keeps_its_stage_observations(board, cfg, monkeypatch):
    """The empty path commits, and rolling it back would be a different bug.

    Two kinds of row ride in DigestLog: "reported to a human", and section 2's
    stage observations, staged for every active project whether or not anything
    is reported. A stage change is only reported once a PREVIOUS stage was
    recorded, so discarding those observations because a digest happened to be
    empty means the project never accumulates a prior stage and its next stage
    change is never reported at all.
    """
    monkeypatch.setattr("app.pipeline.notify.send_digest", lambda cfg, body: "console")
    run_notify(board, cfg)
    assert run_notify(board, cfg) == {"sent": False, "reason": "nothing new"}
    stage_rows = [d for d in _sent(board) if d.kind == "stage_change"]
    assert {d.ref_id for d in stage_rows} == {p.id for p in board.exec(select(Project)).all()}


def test_stage_change_is_reported_after_an_empty_run(board, cfg, monkeypatch):
    """End to end: the observation survives an empty digest and does its job."""
    monkeypatch.setattr("app.pipeline.notify.send_digest", lambda cfg, body: "console")
    run_notify(board, cfg)                                   # reports 3 new projects
    assert run_notify(board, cfg)["sent"] is False           # empty run

    p = board.exec(select(Project)).first()
    p.stage = Stage.design
    board.add(p)
    board.commit()

    bodies = []
    monkeypatch.setattr("app.pipeline.notify.send_digest",
                        lambda cfg, body: bodies.append(body) or "console")
    result = run_notify(board, cfg)
    assert result["stage_changes"] == 1
    assert "now design" in bodies[0]
