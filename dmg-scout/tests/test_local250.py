"""UA Local 250 signatory list: HTML parsing, name cleaning, and the
fuzzy-match join onto CSLB contractors. See app/pipeline/local250.py's
module docstring for the compliance check (robots.txt, no ToS restriction)
and why Cal-SMACNA was investigated but not built (no public directory at
all -- gated behind a direct email request).

The fetch HTTP call is mocked (respx); these tests are about parsing and
the match/ambiguous/unmatched logic, not live network."""
import httpx
import respx

from sqlmodel import select

from app.http import PoliteClient
from app.models import Contractor, SourceRun
from app.pipeline.local250 import (
    CONTRACTORS_URL, _clean_name, _parse_contractors, fetch_and_match_local250, match_local250,
)


def fast_client() -> PoliteClient:
    return PoliteClient(interval=0, max_retries=0, respect_robots=False)


def _page(*entries: str) -> str:
    """entries are pre-built <p> blocks, joined into one page like the real
    site's flat <p> sequence -- see _record() below for the normal shape."""
    return "<html><body><div>" + "".join(entries) + "</div></body></html>"


def _record(name: str, address: str | None = None, city: str | None = None,
           state: str = "CA", zip_code: str | None = None, phone: str | None = None,
           website: str | None = None) -> str:
    parts = [f"<p><strong>{name}</strong></p>"]
    if address:
        parts.append(f"<p>{address}</p>")
    if city and zip_code:
        parts.append(f"<p>{city}, {state} {zip_code}</p>")
    if phone:
        parts.append(f"<p>Phone: {phone}</p>")
    if website:
        parts.append(f'<p><a href="{website}">{website}</a></p>')
    parts.append("<p><br></p>")
    return "".join(parts)


def _contractor(business_name: str, city: str = "Anaheim", full_business_name: str | None = None,
                license_no: str = "1000001") -> Contractor:
    return Contractor(license_no=license_no, business_name=business_name,
                      full_business_name=full_business_name, city=city, state="CA")


# --- _parse_contractors: pure HTML parsing -----------------------------------

def test_parses_a_clean_record():
    html = _page(_record("ACCO ENGINEERED SYSTEMS", "888 E Walnut Street", "Pasadena",
                         zip_code="91101", phone="818/244-6571", website="https://www.accoes.com/"))
    records = _parse_contractors(html)
    assert len(records) == 1
    r = records[0]
    assert r["name"] == "ACCO ENGINEERED SYSTEMS"
    assert r["city"] == "Pasadena"
    assert r["state"] == "CA"
    assert r["zip"] == "91101"
    assert r["phone"] == "818/244-6571"
    assert r["website"] == "https://www.accoes.com/"


def test_parses_multiple_records_in_order():
    html = _page(_record("ABM", city="Irvine", zip_code="92618"),
                _record("XCEL MECHANICAL INC.", city="Gardena", zip_code="90249"))
    records = _parse_contractors(html)
    assert [r["name"] for r in records] == ["ABM", "XCEL MECHANICAL INC."]


def test_a_record_with_no_city_still_parses_rather_than_being_dropped():
    # Real gap in the source itself, seen 2026-08-16 ("Air Flow Mechanical
    # (website only)", "Schultz" with no city/state/zip at all) -- the
    # source's own sparseness, not something the parser should paper over
    # by silently dropping the record.
    html = _page(_record("AIR FLOW MECHANICAL", website="https://www.airflowmech.com/"))
    records = _parse_contractors(html)
    assert len(records) == 1
    assert records[0]["city"] is None


def test_empty_page_parses_to_no_records():
    assert _parse_contractors(_page()) == []


# --- _clean_name --------------------------------------------------------------

def test_clean_name_strips_legal_suffixes_and_punctuation():
    assert _clean_name("Advanced Centrifugal Systems, Inc.") == "advanced centrifugal systems"
    assert _clean_name("D. Burke Mechanical Corp.") == "d burke mechanical"


def test_clean_name_strips_parenthetical_aliasing_notes():
    assert _clean_name("AO REED (Formerly THERMA LLC)") == "ao reed"


def test_clean_name_does_not_eat_real_company_names():
    # Regression: app.normalize.normalize_name's project/SPE-name stripping
    # (built for "Building A", "Site 3", trailing codes like NV11) silently
    # reduced this real company name to an empty string, 2026-08-16 -- see
    # the module-level comment on why this function does NOT reuse it.
    assert _clean_name("BUILDING AIRE INC.") == "building aire"


# --- match_local250: the join -------------------------------------------------

def test_clean_single_match_sets_signatory_flag(db_session):
    c = _contractor("ACCO ENGINEERED SYSTEMS", city="Pasadena")
    db_session.add(c)
    db_session.commit()
    stats = match_local250(db_session, [
        {"name": "ACCO ENGINEERED SYSTEMS", "city": "Pasadena"},
    ])
    db_session.refresh(c)
    assert stats == {"local250_total": 1, "matched": 1, "ambiguous": 0, "unmatched": 0}
    assert c.ua_local_250_signatory is True
    assert c.ua_local_250_matched_name == "ACCO ENGINEERED SYSTEMS"
    assert c.ua_local_250_checked_at is not None


def test_no_close_candidate_is_unmatched_not_guessed(db_session):
    db_session.add(_contractor("TOTALLY UNRELATED PLUMBING CO", city="Fresno"))
    db_session.commit()
    stats = match_local250(db_session, [{"name": "ACCO ENGINEERED SYSTEMS", "city": "Pasadena"}])
    assert stats["matched"] == 0
    assert stats["unmatched"] == 1


def test_multiple_tied_candidates_are_ambiguous_not_picked(db_session):
    db_session.add(_contractor("MASTER COOLING", city="Moorpark", license_no="1"))
    db_session.add(_contractor("MASTER COOLING", city="Carson", license_no="2"))
    db_session.commit()
    stats = match_local250(db_session, [{"name": "MASTER COOLING CORP.", "city": "Moorpark"}])
    assert stats["matched"] == 0
    assert stats["ambiguous"] == 1
    for c in db_session.exec(select(Contractor)).all():
        assert c.ua_local_250_signatory is False


def test_rerun_clears_a_signatory_no_longer_on_the_list(db_session):
    c = _contractor("ACCO ENGINEERED SYSTEMS", city="Pasadena")
    c.ua_local_250_signatory = True
    c.ua_local_250_matched_name = "ACCO ENGINEERED SYSTEMS"
    db_session.add(c)
    db_session.commit()
    # This run's fetch no longer lists it -- a full refresh, not an
    # accumulate-forever set.
    match_local250(db_session, [])
    db_session.refresh(c)
    assert c.ua_local_250_signatory is False
    assert c.ua_local_250_matched_name is None


def test_short_degenerate_names_are_never_candidates(db_session):
    # CSLB's own roster carries real short/coded business names (seen
    # 2026-08-16: "DB1", "M C M", "FS3 INC") that carry no discriminative
    # power for fuzzy matching -- must never be treated as a match target.
    db_session.add(_contractor("DB1", city="Agoura Hills"))
    db_session.commit()
    stats = match_local250(db_session, [{"name": "BUILDING AIRE INC.", "city": "Huntington Beach"}])
    assert stats["matched"] == 0
    assert stats["unmatched"] == 1


# --- fetch_and_match_local250: fetch + SourceRun -------------------------------

@respx.mock
def test_fetch_records_a_source_run_and_matches(db_session):
    c = _contractor("ACCO ENGINEERED SYSTEMS", city="Pasadena")
    db_session.add(c)
    db_session.commit()
    html = _page(_record("ACCO ENGINEERED SYSTEMS", "888 E Walnut Street", "Pasadena", zip_code="91101"))
    respx.get(CONTRACTORS_URL).mock(return_value=httpx.Response(200, text=html))

    with fast_client() as client:
        stats = fetch_and_match_local250(db_session, client=client)

    assert stats["ok"] is True
    assert stats["matched"] == 1
    runs = db_session.exec(select(SourceRun)).all()
    assert len(runs) == 1
    assert runs[0].source == "ua_local_250"
    assert runs[0].ok is True
    assert runs[0].records_fetched == 1
    assert runs[0].records_new == 1


@respx.mock
def test_fetch_records_a_failed_source_run_on_zero_records(db_session):
    respx.get(CONTRACTORS_URL).mock(return_value=httpx.Response(200, text=_page()))
    with fast_client() as client:
        stats = fetch_and_match_local250(db_session, client=client)
    assert stats["ok"] is False
    assert "zero" in stats["error"].lower()
