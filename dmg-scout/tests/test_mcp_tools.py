"""The ten MCP tools, called directly as plain functions (the @mcp.tool
decorator leaves them callable — see app/mcp_tools.py). Exercises the query
logic against real seeded data; tests/test_mcp.py covers the OAuth/transport
layer separately."""
from datetime import datetime

from sqlmodel import select

from app.accounts import create_account, seed_product_lines, seed_selection_tools
from app.manual import add_manual_signal
from app.mcp_tools import (
    board_summary,
    get_account,
    get_account_page,
    get_project,
    get_selection_tool,
    log_field_intel,
    log_outreach,
    search_firms,
    search_projects,
    source_health,
    who_to_call,
)
from app.models import (
    AccountCoverage, Category, Contractor, Disposition, Firm, FieldIntel, Opportunity, Outcome,
    Outreach, OpscProject, Project, ProductLine, Signal, SignalType, Stage,
)
from app.pipeline.resolve import run_resolve
from app.pipeline.size_score import run_size_score


def _seed_project(db_session, cfg, **kw):
    defaults = dict(project_name="Meridian DC", developer="Vantage Data Centers",
                    county="San Bernardino", state="CA", mw_it=176, stage="design")
    defaults.update(kw)
    add_manual_signal(db_session, "prequal_invite", "ACCO invited prequal", **defaults)
    run_resolve(db_session, cfg, use_llm=False)
    run_size_score(db_session, cfg)
    return db_session.exec(select(Project)).one()


def test_board_summary_reports_real_counts(db_session, cfg):
    _seed_project(db_session, cfg)
    out = board_summary()
    assert "1 active projects" in out
    assert "data_center 1" in out


def test_board_summary_empty_board(db_session, cfg):
    assert board_summary() == "No active projects on the board."


def test_search_projects_filters_and_shows_id(db_session, cfg):
    p = _seed_project(db_session, cfg, county="San Bernardino", state="CA")
    out = search_projects(county="San Bernardino", state="CA")
    assert f"#{p.id}" in out
    assert "Meridian DC" in out

    out_miss = search_projects(county="Nowhere County")
    assert "No projects matched" in out_miss


def test_search_projects_unknown_stage_reports_valid_values(db_session, cfg):
    out = search_projects(stage="not-a-real-stage")
    assert "Unknown stage" in out and "entitlement" in out


def test_search_projects_caps_at_twenty_and_says_how_many_omitted(db_session, cfg):
    # A distinct county per signal means _blocked_candidates finds nothing to
    # compare against — guaranteed 25 independent new projects, not 25 names
    # similar enough to risk a weak-evidence review-queue match against each
    # other (rapidfuzz scored near-identical names sharing a county high
    # enough to queue for review instead of creating a new project).
    for i in range(25):
        add_manual_signal(db_session, "prequal_invite", f"signal {i}",
                          project_name=f"Project {i}", developer=f"Developer {i}",
                          county=f"County{i}", state="CA", mw_it=10 + i, stage="entitlement")
        run_resolve(db_session, cfg, use_llm=False)
    run_size_score(db_session, cfg)
    out = search_projects()
    assert "25 project(s) matched, showing top 20" in out
    assert "5 more not shown" in out


def test_get_project_full_detail(db_session, cfg):
    p = _seed_project(db_session, cfg)
    out = get_project(p.id)
    assert p.name in out
    assert "Stage:" in out and "Contact ladder" in out and "Signal timeline" in out


def test_get_project_missing_id(db_session, cfg):
    assert get_project(99999) == "No project #99999."


def test_who_to_call_skips_unreachable_projects(db_session, cfg):
    _seed_project(db_session, cfg)  # no named contact -> unreachable
    out = who_to_call()
    assert "No project in scope has a reachable contact" in out


def test_who_to_call_lists_reachable_contact(db_session, cfg):
    sig = Signal(signal_type=SignalType.ceqa_nop, category=Category.data_center,
                stage=Stage.design, project_name="Reachable DC", county="Los Angeles",
                state="CA", mw_it=50, summary_one_line="x", confidence=0.9,
                named_people=[{"name": "Jane Doe", "title": "Mechanical Engineer",
                             "org": "Acme MEP", "phone": "555-1234"}])
    db_session.add(sig)
    db_session.commit()
    run_resolve(db_session, cfg, use_llm=False)
    run_size_score(db_session, cfg)
    out = who_to_call()
    assert "Jane Doe" in out and "555-1234" in out


def _opsc(id_=None, county="Los Angeles", district="Test USD", status="Funds Released"):
    return OpscProject(id=id_, county=county, district=district, school_name="Test Elementary",
                       program="Modernization", application_number=f"26/{id_ or 1}-00-001",
                       status=status, in_territory=True, source_url="https://data.ca.gov/test")


def test_who_to_call_lists_in_territory_opsc_row(db_session, cfg):
    _seed_project(db_session, cfg)  # unreachable, so the project list itself is empty
    db_session.add(_opsc())
    db_session.commit()
    out = who_to_call()
    assert "OPSC school funding" in out
    assert "Test USD" in out and "Test Elementary" in out
    assert "Bidding contractors" in out  # Funds Released -> bidding_contractors


def test_who_to_call_opsc_filtered_by_county(db_session, cfg):
    db_session.add(_opsc(id_=1, county="Los Angeles"))
    db_session.add(_opsc(id_=2, county="Orange"))
    db_session.commit()
    out = who_to_call(county="Orange")
    assert "OPSC school funding" in out
    assert "Orange" in out
    # Los Angeles row must not appear -- filtered out by county
    assert out.count("Test USD") == 1


def test_who_to_call_opsc_suppressed_for_non_ca_state(db_session, cfg):
    db_session.add(_opsc())
    db_session.commit()
    out = who_to_call(state="TX")
    assert "OPSC school funding" not in out


def test_get_account_by_name_and_disambiguation(db_session, cfg):
    seed_product_lines(db_session, cfg)
    create_account(db_session, name="Acme Mechanical", account_type="mechanical_contractor")
    create_account(db_session, name="Acme Service Co", account_type="service_contractor")

    ambiguous = get_account(name="Acme")
    assert "2 accounts match" in ambiguous

    exact = get_account(name="Acme Mechanical")
    assert "Acme Mechanical" in exact and "Coverage:" in exact


def test_get_account_shows_gaps_and_windows(db_session, cfg):
    seed_product_lines(db_session, cfg)
    account = create_account(db_session, name="Big MEP Co", account_type="mechanical_contractor")
    line = db_session.exec(select(ProductLine)).first()
    cov = db_session.exec(
        select(AccountCoverage).where(AccountCoverage.account_id == account.id,
                                      AccountCoverage.product_line_id == line.id)).one()
    cov.status = "bought"
    cov.install_year = 1995
    db_session.add(cov)
    db_session.commit()

    out = get_account(account_id=account.id)
    assert "Big MEP Co" in out
    assert "Coverage:" in out


def test_get_account_no_args(db_session, cfg):
    assert get_account() == "Give either account_id or name."


def test_get_account_page_no_args(db_session, cfg):
    assert get_account_page() == "Give either account_id or name."


def test_get_account_page_by_name_and_disambiguation(db_session, cfg):
    create_account(db_session, name="Acme Mechanical", account_type="mechanical_contractor")
    create_account(db_session, name="Acme Service Co", account_type="service_contractor")

    ambiguous = get_account_page(name="Acme")
    assert "2 accounts match" in ambiguous

    exact = get_account_page(name="Acme Mechanical")
    assert "Acme Mechanical" in exact and "Who they are:" in exact


def test_get_account_page_unknown_id(db_session, cfg):
    assert get_account_page(account_id=999999) == "No account #999999."


def test_get_account_page_reports_every_section(db_session, cfg):
    account = create_account(db_session, name="Southland Air Systems LLC")
    db_session.add(Contractor(license_no="55555", business_name="Southland Air Systems Inc.",
                              latitude=34.05, longitude=-118.25, primary_status="CLEAR"))
    db_session.commit()

    out = get_account_page(account_id=account.id)
    assert "Who they are:" in out and "55555" in out
    assert "What we've sold them:" in out
    assert "What I could hand them:" in out
    assert "0 overdue retrofit buildings" in out
    assert "Where they already show up:" in out
    assert "Not on Scout's own firm roster" in out
    assert "What we've said to each other:" in out
    assert "log_outreach(account_id=...)" in out


def test_get_account_page_ambiguous_cslb_lists_candidates(db_session, cfg):
    account = create_account(db_session, name="Southland Air Systems Corp")
    db_session.add(Contractor(license_no="1", business_name="Southland Air Systems Inc."))
    db_session.add(Contractor(license_no="2", business_name="Southland Air Systems LLC"))
    db_session.commit()

    out = get_account_page(account_id=account.id)
    assert "2 CSLB licenses tied" in out
    assert "Southland Air Systems Inc." in out and "Southland Air Systems LLC" in out


def test_search_firms_shows_active_projects_and_account_link(db_session, cfg):
    firm = Firm(name="Test Mech Contractor", name_norm="test mech contractor",
               firm_type="mech_contractor")
    db_session.add(firm)
    db_session.commit()
    db_session.refresh(firm)
    seed_product_lines(db_session, cfg)
    account = create_account(db_session, name="Test Mech Contractor", firm_id=firm.id)

    out = search_firms("Test Mech")
    assert "Test Mech Contractor" in out
    assert f"get_account(account_id={account.id})" in out


def test_search_firms_no_match(db_session, cfg):
    assert "No firm matching" in search_firms("Totally Fictional Firm Name")


def test_log_outreach_writes_and_confirms(db_session, cfg):
    p = _seed_project(db_session, cfg)
    out = log_outreach(project_id=p.id, notes="left voicemail", channel="call",
                       next_action="follow up", next_action_date="2026-09-01")
    assert "left voicemail" in out and "follow up" in out and "2026-09-01" in out
    row = db_session.exec(select(Outreach).where(Outreach.project_id == p.id)).one()
    assert row.channel == "call" and row.next_action_date == datetime(2026, 9, 1)


def test_log_outreach_bad_date(db_session, cfg):
    p = _seed_project(db_session, cfg)
    out = log_outreach(project_id=p.id, notes="x", next_action_date="not-a-date")
    assert "Could not parse" in out


def test_log_outreach_missing_project(db_session, cfg):
    assert log_outreach(project_id=99999, notes="x") == "No project #99999."


def test_log_outreach_account_writes_and_confirms(db_session, cfg):
    account = create_account(db_session, name="Outreach Tool Co")
    out = log_outreach(account_id=account.id, notes="talked shop", channel="meeting")
    assert "Outreach Tool Co" in out and "talked shop" in out
    row = db_session.exec(select(Outreach).where(Outreach.account_id == account.id)).one()
    assert row.channel == "meeting" and row.project_id is None


def test_log_outreach_missing_account(db_session, cfg):
    assert log_outreach(account_id=99999, notes="x") == "No account #99999."


def test_log_outreach_requires_exactly_one_of_project_or_account(db_session, cfg):
    p = _seed_project(db_session, cfg)
    account = create_account(db_session, name="Both Given Co")
    assert "exactly one" in log_outreach(notes="x")
    assert "exactly one" in log_outreach(project_id=p.id, account_id=account.id, notes="x")


def _seed_opportunity(db_session):
    signal = Signal(signal_type=SignalType.ceqa_nop)
    db_session.add(signal)
    db_session.flush()
    opp = Opportunity(signal_id=signal.id, account_id=1, owner_user="andrew")
    db_session.add(opp)
    db_session.commit()
    return opp


def test_log_outreach_opportunity_id_writes_an_outcome_row(db_session, cfg):
    opp = _seed_opportunity(db_session)
    out = log_outreach(opportunity_id=opp.id, disposition="connected", user="andrew", notes="reached the PE")
    assert "connected" in out and str(opp.id) in out and "andrew" in out
    row = db_session.exec(select(Outcome).where(Outcome.opportunity_id == opp.id)).one()
    assert row.disposition == Disposition.connected
    assert row.user == "andrew"
    assert row.source.value == "web"


def test_log_outreach_opportunity_id_requires_disposition(db_session, cfg):
    opp = _seed_opportunity(db_session)
    assert "requires disposition" in log_outreach(opportunity_id=opp.id, user="andrew")


def test_log_outreach_opportunity_id_requires_user(db_session, cfg):
    opp = _seed_opportunity(db_session)
    assert "requires user" in log_outreach(opportunity_id=opp.id, disposition="connected")


def test_log_outreach_opportunity_id_lost_without_reason_code_is_rejected(db_session, cfg):
    opp = _seed_opportunity(db_session)
    out = log_outreach(opportunity_id=opp.id, disposition="lost", user="andrew")
    assert "reason_code" in out
    assert db_session.exec(select(Outcome).where(Outcome.opportunity_id == opp.id)).first() is None


def test_log_outreach_three_way_still_exactly_one(db_session, cfg):
    p = _seed_project(db_session, cfg)
    opp = _seed_opportunity(db_session)
    assert "exactly one" in log_outreach(project_id=p.id, opportunity_id=opp.id, disposition="connected", user="andrew")


def test_log_field_intel_writes_and_confirms(db_session, cfg):
    out = log_field_intel(reported_by="Dave Kim", reported_at="2026-08-20",
                          source_notes="Pursuing a job in Fontana, early design.",
                          owner="Fontana Cold Co", engineer_name="Some Engineer")
    assert "UNVERIFIED" in out
    assert "Dave Kim" in out and "Fontana Cold Co" in out
    row = db_session.exec(select(FieldIntel)).one()
    assert row.reported_by == "Dave Kim" and row.owner == "Fontana Cold Co"
    assert row.engineer_name == "Some Engineer"
    assert "not on Scout's roster yet" in out


def test_log_field_intel_flags_roster_match(db_session, cfg):
    account = create_account(db_session, name="Known Mech Sub")
    out = log_field_intel(reported_by="Dave Kim", reported_at="2026-08-20",
                          source_notes="Same GC, different job.",
                          mech_contractor_name="Known Mech Sub")
    assert "ALREADY on Scout's roster" in out
    row = db_session.exec(select(FieldIntel)).one()
    assert row.mech_contractor_account_id == account.id


def test_log_field_intel_bad_date(db_session, cfg):
    out = log_field_intel(reported_by="Dave Kim", reported_at="not-a-date", source_notes="x")
    assert "YYYY-MM-DD" in out


def test_log_field_intel_requires_reported_by(db_session, cfg):
    out = log_field_intel(reported_by="", reported_at="2026-08-20", source_notes="x")
    assert out and "reported_by" in out.lower()


def test_source_health_reports_budget_and_sources(db_session, cfg):
    out = source_health()
    assert "Source health:" in out
    assert "LLM spend:" in out


def test_get_selection_tool_confirmed(db_session, cfg):
    """Regression test: this crashed with DetachedInstanceError before the
    fix -- accessing ORM attributes after session_scope()'s `with` block had
    already closed. Calling this at all, successfully, is most of the test."""
    seed_product_lines(db_session, cfg)
    seed_selection_tools(db_session, cfg)
    out = get_selection_tool("Marley")
    assert "CoolSpec" in out
    assert "Confirmed" in out
    assert "DMG" in out


def test_get_selection_tool_unchecked_is_distinct_from_none_exists(db_session, cfg):
    seed_product_lines(db_session, cfg)
    seed_selection_tools(db_session, cfg)
    out = get_selection_tool("Nailor")
    assert "unchecked" in out.lower()
    assert "no tool exists" not in out.lower() or "not the same as" in out.lower()


def test_get_selection_tool_unknown_line(db_session, cfg):
    seed_product_lines(db_session, cfg)
    seed_selection_tools(db_session, cfg)
    assert "No line card entry" in get_selection_tool("Not A Real Line")
