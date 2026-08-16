"""Competitor line card map: seeding (idempotent upsert), the per-role
grouping used by /project/{id} and /line/{id}, and the dataset's own
internal consistency (every role a real app.accounts.ROLE_ORDER value,
every status one of confirmed/unconfirmed, unconfirmed rows carry a note).
See app/competitors.py's module docstring for the research method."""
from sqlmodel import select

from app.accounts import ROLE_ORDER
from app.competitors import COMPETITOR_LINES, REP_FIRMS, competing_lines_by_role, seed_competitor_lines
from app.models import CompetitorLine, RepFirm, SourceRun


def test_dataset_roles_are_all_real_role_order_values():
    for manufacturer, rep_firm, role, channel, status, source_url, note in COMPETITOR_LINES:
        assert role is None or role in ROLE_ORDER, f"{manufacturer}: unknown role {role!r}"


def test_dataset_status_is_always_confirmed_or_unconfirmed():
    for manufacturer, rep_firm, role, channel, status, source_url, note in COMPETITOR_LINES:
        assert status in ("confirmed", "unconfirmed"), f"{manufacturer}: bad status {status!r}"


def test_dataset_unconfirmed_rows_always_carry_a_note():
    for manufacturer, rep_firm, role, channel, status, source_url, note in COMPETITOR_LINES:
        if status == "unconfirmed":
            assert note, f"{manufacturer}: unconfirmed with no explanation"


def test_dataset_factory_direct_rows_have_no_rep_firm():
    for manufacturer, rep_firm, role, channel, status, source_url, note in COMPETITOR_LINES:
        if channel == "factory_direct":
            assert rep_firm is None, f"{manufacturer}: factory_direct but has a rep_firm"
        else:
            assert rep_firm is not None, f"{manufacturer}: rep_firm channel but no rep_firm named"


def test_dataset_rep_firm_names_are_all_in_rep_firms():
    for manufacturer, rep_firm, role, channel, status, source_url, note in COMPETITOR_LINES:
        if rep_firm is not None:
            assert rep_firm in REP_FIRMS, f"{manufacturer}: rep firm {rep_firm!r} not in REP_FIRMS"


def test_dataset_every_row_has_a_source_url():
    for manufacturer, rep_firm, role, channel, status, source_url, note in COMPETITOR_LINES:
        assert source_url and source_url.startswith("http"), f"{manufacturer}: missing/bad source_url"


def test_seed_creates_rep_firms_and_lines(db_session):
    stats = seed_competitor_lines(db_session)
    assert stats["rep_firms"] == len(REP_FIRMS)
    assert stats["lines_total"] == len(COMPETITOR_LINES)
    firms = db_session.exec(select(RepFirm)).all()
    assert len(firms) == len(REP_FIRMS)
    lines = db_session.exec(select(CompetitorLine)).all()
    assert len(lines) == len(COMPETITOR_LINES)


def test_seed_is_idempotent_no_duplicate_rows_on_rerun(db_session):
    seed_competitor_lines(db_session)
    seed_competitor_lines(db_session)
    lines = db_session.exec(select(CompetitorLine)).all()
    assert len(lines) == len(COMPETITOR_LINES)
    firms = db_session.exec(select(RepFirm)).all()
    assert len(firms) == len(REP_FIRMS)


def test_seed_writes_a_source_run(db_session):
    seed_competitor_lines(db_session)
    runs = db_session.exec(select(SourceRun).where(SourceRun.source == "competitor_lines")).all()
    assert len(runs) == 1
    assert runs[0].ok is True
    assert runs[0].records_fetched == len(COMPETITOR_LINES)


def test_competing_lines_by_role_excludes_null_role_rows(db_session):
    seed_competitor_lines(db_session)
    by_role = competing_lines_by_role(db_session)
    for role, rows in by_role.items():
        assert role in ROLE_ORDER
        for r in rows:
            assert r.building_role == role


def test_competing_lines_by_role_includes_greenheck_as_unconfirmed(db_session):
    seed_competitor_lines(db_session)
    by_role = competing_lines_by_role(db_session)
    fans = by_role.get("fans_ventilation", [])
    greenheck = [c for c in fans if c.manufacturer == "Greenheck"]
    assert len(greenheck) == 1
    assert greenheck[0].status == "unconfirmed"


def test_trane_appears_factory_direct_with_no_rep_firm(db_session):
    seed_competitor_lines(db_session)
    by_role = competing_lines_by_role(db_session)
    trane_rows = [c for role_rows in by_role.values() for c in role_rows if c.manufacturer == "Trane"]
    assert len(trane_rows) == 2  # cooling_generation and air_handling
    for row in trane_rows:
        assert row.channel == "factory_direct"
        assert row.rep_firm_id is None
