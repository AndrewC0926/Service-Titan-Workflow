"""One malformed document must cost exactly one document.

The GOED backfill stored nothing because a single PDF carried NUL bytes: the
fetch loop shared one transaction, committed every 25 documents, and called
session.rollback() on any exception. GOED's whole crawl is one chunk, so that
rollback discarded the entire pass — and the run reported progress the whole time.
"""
import pytest
from datetime import datetime

from sqlmodel import select

from app.config import Config
from app.models import BackfillCheckpoint, RawDocument, SignalType, SourceRun
from app.pipeline.backfill import run_backfill
from app.pipeline.fetch import run_fetch, store_document
from app.sources.base import FetchedDoc, SourceAdapter, registry

POISON_UID = "poison"


def make_doc(uid: str, text: str = None) -> FetchedDoc:
    return FetchedDoc(source="flaky", source_uid=uid, url=f"https://x/{uid}",
                      title=f"doc {uid}", raw_text=text or ("data center " * 60),
                      default_signal_type=SignalType.planning_agenda)


class FlakyAdapter(SourceAdapter):
    """Yields good documents with one unstorable document in the middle."""
    name = "flaky"
    n_docs = 7

    def fetch(self, cfg, client, since=None):
        for i in range(self.n_docs):
            yield make_doc(POISON_UID if i == 3 else f"good{i}")

    def backfill_chunks(self, cfg, since):
        return [{"key": "only-chunk"}]

    def fetch_chunk(self, cfg, client, since, chunk):
        return self.fetch(cfg, client, since=since)


@pytest.fixture()
def poison_store(monkeypatch):
    """Make exactly one document fail at store time, as a bad PDF would."""
    import app.pipeline.fetch as fetch_mod
    real = fetch_mod._store

    def flaky_store(session, doc):
        if doc.source_uid == POISON_UID:
            raise ValueError("A string literal cannot contain NUL (0x00) characters")
        return real(session, doc)

    monkeypatch.setattr(fetch_mod, "_store", flaky_store)


@pytest.fixture()
def flaky_cfg():
    return Config({"keywords": {"data_center": ["data center"]},
                   "sources": {"flaky": {"enabled": True}}})


@pytest.fixture(autouse=True)
def _register_flaky(monkeypatch):
    registry["flaky"] = FlakyAdapter
    monkeypatch.setattr("app.pipeline.backfill.BACKFILLABLE",
                        ("ceqanet", "goed", "edgar", "legistar",
                         "civicplus", "primegov", "flaky"))
    yield
    registry.pop("flaky", None)


@pytest.fixture()
def fast_client(monkeypatch):
    from app.http import PoliteClient as PC
    orig = PC.__init__

    def fast_init(self, *a, **k):
        k.update(interval=0, max_retries=0, respect_robots=False)
        orig(self, *a, **k)
    monkeypatch.setattr(PC, "__init__", fast_init)


# ---- the primitive --------------------------------------------------------

def test_store_document_isolates_one_bad_document(db_session, poison_store):
    """Documents on both sides of the failure must survive, and the session must
    still be usable afterwards — a savepoint rollback is what buys that."""
    before, err_before = store_document(db_session, make_doc("before"))
    bad, err_bad = store_document(db_session, make_doc(POISON_UID))
    after, err_after = store_document(db_session, make_doc("after"))

    assert (before, err_before) == (1, None)
    assert bad == 0 and "NUL" in err_bad
    assert (after, err_after) == (1, None), "session must survive the failure"

    uids = {r.source_uid for r in db_session.exec(select(RawDocument)).all()}
    assert uids == {"before", "after"}


def test_store_document_commits_immediately(db_session, poison_store):
    """Committed per document, so a process killed mid-run keeps its work."""
    store_document(db_session, make_doc("kept"))
    db_session.rollback()   # simulate an abrupt abort after the store
    assert db_session.exec(
        select(RawDocument).where(RawDocument.source_uid == "kept")).first() is not None


# ---- the fetch loop -------------------------------------------------------

def test_fetch_keeps_documents_around_a_mid_stream_failure(
        db_session, flaky_cfg, poison_store, fast_client):
    runs = run_fetch(db_session, flaky_cfg, only_source="flaky")
    run = runs["flaky"]

    stored = {r.source_uid for r in db_session.exec(select(RawDocument)).all()}
    assert stored == {f"good{i}" for i in (0, 1, 2, 4, 5, 6)}, \
        "every document except the poisoned one must be stored"
    assert run.records_fetched == 7
    assert run.records_new == 6
    # The failure is reported, not swallowed: a green run would hide it.
    assert run.ok is False
    assert "1/7 documents failed to store" in run.error
    assert POISON_UID in run.error


# ---- the backfill loop ----------------------------------------------------

def test_backfill_keeps_documents_around_a_mid_chunk_failure(
        db_session, flaky_cfg, poison_store, fast_client):
    totals = run_backfill(db_session, flaky_cfg, "flaky", datetime(2024, 8, 1))

    stored = {r.source_uid for r in db_session.exec(select(RawDocument)).all()}
    assert stored == {f"good{i}" for i in (0, 1, 2, 4, 5, 6)}
    assert totals["fetched"] == 7 and totals["new"] == 6
    assert totals["doc_errors"] == 1
    assert totals["chunk_errors"] == 0, "a bad document is not a bad chunk"

    run = db_session.exec(
        select(SourceRun).where(SourceRun.source == "flaky:backfill")).all()[-1]
    assert run.ok is False and "1 documents failed to store" in run.error


def test_backfill_keeps_fetched_documents_when_the_chunk_itself_dies(
        db_session, flaky_cfg, fast_client, monkeypatch):
    """A chunk that raises part-way through — the GOED shape — must keep the
    documents it already fetched, and must NOT checkpoint, so a re-run retries."""

    class DyingAdapter(FlakyAdapter):
        name = "flaky"

        def fetch_chunk(self, cfg, client, since, chunk):
            yield make_doc("kept1")
            yield make_doc("kept2")
            raise RuntimeError("listing page exploded")

    registry["flaky"] = DyingAdapter
    totals = run_backfill(db_session, flaky_cfg, "flaky",
                          datetime(2024, 8, 1))

    stored = {r.source_uid for r in db_session.exec(select(RawDocument)).all()}
    assert stored == {"kept1", "kept2"}, \
        "documents fetched before the crash are kept, not rolled back"
    assert totals["chunk_errors"] == 1
    assert totals["fetched"] == 2
    assert db_session.exec(select(BackfillCheckpoint)).all() == [], \
        "an incomplete chunk must not be checkpointed"


def test_backfill_resume_after_partial_chunk_refetches_but_dedupes(
        db_session, flaky_cfg, fast_client):
    """Second pass over an un-checkpointed chunk re-fetches and stores 0 new."""
    registry["flaky"] = FlakyAdapter
    since = datetime(2024, 8, 1)
    first = run_backfill(db_session, flaky_cfg, "flaky", since)
    assert first["new"] == 7 and first["doc_errors"] == 0
    assert len(db_session.exec(select(BackfillCheckpoint)).all()) == 1

    second = run_backfill(db_session, flaky_cfg, "flaky", since)
    assert second["chunks_skipped"] == 1 and second["chunks_run"] == 0


# ---- concurrency + checkpoint idempotency ---------------------------------

def test_second_backfill_of_same_source_is_refused(db_session, flaky_cfg, fast_client):
    """Two concurrent backfills duplicate every request and race on the
    checkpoint table. Observed for real: a second run collided on the checkpoint
    insert and took the whole pass down 20 chunks in."""
    from app.models import SourceRun
    from app.pipeline.backfill import ConcurrentBackfill

    db_session.add(SourceRun(source="flaky:backfill"))   # finished_at is None
    db_session.commit()

    with pytest.raises(ConcurrentBackfill, match="already running"):
        run_backfill(db_session, flaky_cfg, "flaky", datetime(2024, 8, 1))

    # ...and --force overrides it for a run known to be dead.
    totals = run_backfill(db_session, flaky_cfg, "flaky", datetime(2024, 8, 1), force=True)
    assert totals["chunks_run"] == 1


def test_duplicate_checkpoint_does_not_kill_the_run(db_session, flaky_cfg, fast_client):
    """A chunk already checkpointed by a concurrent run must be tolerated."""
    from app.pipeline.backfill import _checkpoint

    _checkpoint(db_session, "flaky", "only-chunk", 1, 1)
    _checkpoint(db_session, "flaky", "only-chunk", 1, 1)   # must not raise
    assert len(db_session.exec(select(BackfillCheckpoint)).all()) == 1
    # Session still usable afterwards.
    assert run_backfill(db_session, flaky_cfg, "flaky",
                        datetime(2024, 8, 1))["chunks_skipped"] == 1


def test_database_failure_still_records_the_run_outcome(db_session, flaky_cfg,
                                                       fast_client, monkeypatch):
    """The failure handler must not fail. A DB error left the session in a failed
    transaction, so recording the outcome raised PendingRollbackError and the run
    stayed at ok=None with no error — the exact opposite of failing loud."""
    from app.models import SourceRun
    import app.pipeline.backfill as bf

    def boom(*a, **k):
        raise RuntimeError("checkpoint exploded")

    monkeypatch.setattr(bf, "_checkpoint", boom)
    with pytest.raises(RuntimeError, match="checkpoint exploded"):
        run_backfill(db_session, flaky_cfg, "flaky", datetime(2024, 8, 1))

    run = db_session.exec(
        select(SourceRun).where(SourceRun.source == "flaky:backfill")).all()[-1]
    assert run.ok is False, "a crashed run must never be left at ok=None"
    assert run.finished_at is not None
    assert "checkpoint exploded" in run.error


def test_a_killed_run_does_not_wedge_the_source_forever(db_session, flaky_cfg, fast_client):
    """A run killed by SIGKILL never sets finished_at. If the concurrency guard
    treated that as 'still running' the source would be blocked permanently and
    everyone would learn to pass --force reflexively."""
    from datetime import timedelta

    from app.models import SourceRun, utcnow

    corpse = SourceRun(source="flaky:backfill")
    corpse.started_at = utcnow() - timedelta(hours=48)
    db_session.add(corpse)
    db_session.commit()

    # No --force needed: the stale run is recognised as dead.
    assert run_backfill(db_session, flaky_cfg, "flaky",
                        datetime(2024, 8, 1))["chunks_run"] == 1


def test_a_recent_unfinished_run_still_blocks(db_session, flaky_cfg, fast_client):
    """The guard must still catch genuine overlap, which is a minutes-scale
    problem — that is the case that actually corrupted a run."""
    from datetime import timedelta

    from app.models import SourceRun, utcnow
    from app.pipeline.backfill import ConcurrentBackfill

    live = SourceRun(source="flaky:backfill")
    live.started_at = utcnow() - timedelta(minutes=4)
    db_session.add(live)
    db_session.commit()

    with pytest.raises(ConcurrentBackfill, match="already running"):
        run_backfill(db_session, flaky_cfg, "flaky", datetime(2024, 8, 1))
