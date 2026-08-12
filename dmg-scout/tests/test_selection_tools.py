"""selection_tools registry: one row per ProductLine, firm read via join
(never duplicated), and the null-vs-unchecked-vs-none_exists distinction the
whole feature exists to preserve."""
from sqlmodel import select

from app.accounts import (
    lines_needing_selection_tool_research,
    seed_product_lines,
    seed_selection_tools,
)
from app.models import ProductLine, SelectionTool


def seed(db_session, cfg):
    seed_product_lines(db_session, cfg)
    return seed_selection_tools(db_session, cfg)


def test_every_line_gets_exactly_one_row(db_session, cfg):
    seed(db_session, cfg)
    lines = db_session.exec(select(ProductLine)).all()
    tools = db_session.exec(select(SelectionTool)).all()
    assert len(tools) == len(lines) == 70
    line_ids = {t.product_line_id for t in tools}
    assert len(line_ids) == 70  # exactly one per line, no dupes, none missing


def test_unresearched_line_is_null_and_unchecked_not_guessed(db_session, cfg):
    seed(db_session, cfg)
    line = db_session.exec(select(ProductLine).where(ProductLine.name == "Nailor")).one()
    tool = db_session.exec(select(SelectionTool).where(SelectionTool.product_line_id == line.id)).one()
    assert tool.verification_status == "unchecked"
    assert tool.tool_name is None
    assert tool.access_level is None
    # UNCHECKED must never be confused with a confirmed "no tool exists"
    assert tool.access_level != "none_exists"


def test_marley_and_recold_share_coolspec_confirmed(db_session, cfg):
    seed(db_session, cfg)
    for name in ("Marley", "Recold"):
        line = db_session.exec(select(ProductLine).where(ProductLine.name == name)).one()
        tool = db_session.exec(select(SelectionTool).where(SelectionTool.product_line_id == line.id)).one()
        assert tool.tool_name == "CoolSpec"
        assert tool.verification_status == "confirmed"
        assert "spxcooling.com" in tool.vendor_url


def test_aaon_titus_twin_city_fan_search_verified_not_confirmed(db_session, cfg):
    """web research is SEARCH_VERIFIED, not CONFIRMED -- only Marley/Recold
    (a live vendor page naming both lines by name) earned CONFIRMED."""
    seed(db_session, cfg)
    for name in ("AAON", "Titus", "TCF/Twin City Fan"):
        line = db_session.exec(select(ProductLine).where(ProductLine.name == name)).one()
        tool = db_session.exec(select(SelectionTool).where(SelectionTool.product_line_id == line.id)).one()
        assert tool.verification_status == "search_verified"
        assert tool.verified_by == "web research"


def test_firm_is_not_duplicated_on_selection_tool(db_session, cfg):
    """firm lives only on ProductLine -- SelectionTool must not carry its
    own copy that could drift out of sync with a firm correction."""
    assert not hasattr(SelectionTool, "firm")
    seed(db_session, cfg)
    titus = db_session.exec(select(ProductLine).where(ProductLine.name == "Titus")).one()
    assert titus.firm == "ToroAire"  # read through the join, not stored twice


def test_needs_research_list_is_everyone_except_the_five_seeded(db_session, cfg):
    seed(db_session, cfg)
    needs_research = lines_needing_selection_tool_research(db_session)
    names = {l.name for l in needs_research}
    assert len(needs_research) == 65
    assert names.isdisjoint({"AAON", "Marley", "Recold", "TCF/Twin City Fan", "Titus"})


def test_reseed_is_idempotent_and_updates_in_place(db_session, cfg):
    added1 = seed(db_session, cfg)
    assert added1 == 70
    added2 = seed_selection_tools(db_session, cfg)
    assert added2 == 0  # no new rows on a re-run
    tools = db_session.exec(select(SelectionTool)).all()
    assert len(tools) == 70  # still exactly one per line, not doubled
