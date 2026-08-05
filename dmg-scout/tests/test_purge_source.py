"""purge-source must survive a source whose documents were already extracted."""
import pytest
from sqlmodel import select
from typer.testing import CliRunner

from app.cli import app
from app.models import (
    Category, MatchCandidate, Project, ProjectSignal, RawDocument, Signal,
    SignalType, Stage, TriageResult,
)


def _seed(db_session):
    doc = RawDocument(source="ceqanet", source_uid="u1", url="https://x/1", title="t",
                      raw_text="a data center in Riverside", content_hash="h1",
                      triage_result=TriageResult.relevant)
    keep = RawDocument(source="goed", source_uid="u2", url="https://x/2", title="t2",
                       raw_text="unrelated", content_hash="h2",
                       triage_result=TriageResult.relevant)
    db_session.add(doc); db_session.add(keep); db_session.commit()
    sig = Signal(raw_document_id=doc.id, signal_type=SignalType.ceqa_nop,
                 category=Category.data_center, stage=Stage.entitlement,
                 project_name="Purge Me", county="Riverside", state="CA")
    other = Signal(raw_document_id=keep.id, signal_type=SignalType.abatement_application,
                   category=Category.industrial, stage=Stage.permitting,
                   project_name="Keep Me", county="Washoe", state="NV")
    db_session.add(sig); db_session.add(other); db_session.commit()
    proj = Project(name="Purge Me", category=Category.data_center, county="Riverside")
    db_session.add(proj); db_session.commit()
    db_session.add(ProjectSignal(project_id=proj.id, signal_id=sig.id))
    db_session.add(MatchCandidate(signal_id=sig.id, project_id=proj.id, similarity=0.7,
                                  status="pending"))
    db_session.commit()
    return doc, sig, other


def test_purge_deletes_derived_signals_instead_of_raising(db_session, monkeypatch):
    """Regression: the bulk DELETE hit signals_raw_document_id_fkey (NO ACTION) and
    raised ForeignKeyViolation whenever a purged document had been extracted. It
    never fired on EDGAR because those stubs were never extracted."""
    import app.cli as cli_mod
    from contextlib import contextmanager

    @contextmanager
    def _scope():
        yield db_session
    monkeypatch.setattr(cli_mod, "session_scope", _scope)

    _seed(db_session)
    result = CliRunner().invoke(app, ["purge-source", "--source", "ceqanet", "--yes"])
    assert result.exit_code == 0, result.output
    assert "1 documents" in result.output and "1 signals" in result.output

    assert db_session.exec(select(RawDocument).where(
        RawDocument.source == "ceqanet")).all() == []
    assert db_session.exec(select(MatchCandidate)).all() == []
    assert db_session.exec(select(ProjectSignal)).all() == []
    # The other source is untouched, documents and signals both.
    kept = db_session.exec(select(Signal)).all()
    assert [s.project_name for s in kept] == ["Keep Me"]
    assert db_session.exec(select(RawDocument).where(
        RawDocument.source == "goed")).all() != []


def test_purge_dry_run_reports_derived_counts(db_session, monkeypatch):
    import app.cli as cli_mod
    from contextlib import contextmanager

    @contextmanager
    def _scope():
        yield db_session
    monkeypatch.setattr(cli_mod, "session_scope", _scope)

    _seed(db_session)
    result = CliRunner().invoke(app, ["purge-source", "--source", "ceqanet"])
    assert result.exit_code == 0
    assert "would delete 1 documents, 1 signals and 1 project links" in result.output
    # Nothing actually removed without --yes.
    assert db_session.exec(select(RawDocument).where(
        RawDocument.source == "ceqanet")).all() != []
