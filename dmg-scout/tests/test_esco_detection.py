"""ESCO award detection on the agendas already being read.

A public agency selecting an Energy Services Company for a performance contract
means equipment gets bought in 12 to 24 months, and — unlike an architect or CM
award — the ESCO actually selects the equipment. These items appear in the
Legistar and CivicPlus agendas the pipeline already pulls, so this is a keyword
list and a tag, not a new source.

`keyword_match` is the narrowest funnel in the system: a term missing here makes a
project invisible to the entire pipeline, and a term too loose here puts junk
through every stage downstream. Both directions are tested.
"""
import pytest

from app.sources.base import keyword_category, keyword_match

# Real agenda-item phrasings.
AWARDS = [
    "Approve an Energy Services Agreement with Ameresco, Inc. for the Civic Center",
    "Resolution authorizing an Energy Savings Performance Contract with Johnson Controls",
    "Award of ESPC for municipal facilities energy retrofit",
    "Approve Guaranteed Energy Savings Agreement with Trane U.S. Inc.",
    "Authorize a Guaranteed Energy Cost Savings contract for the wastewater plant",
    "Approve an Investment Grade Audit for county facilities",
    "Issue an RFQ for Energy Services for the district's HVAC systems",
    "Request for Proposals for Energy Services — comprehensive facility upgrades",
    "Contract with an energy services company for chiller plant replacement",
    "Selection of ESCO for the Public Works building modernization",
]

NOT_AWARDS = [
    "Approve payment of monthly utility invoices to Southern California Edison",
    "Public hearing on the Escondido Boulevard corridor improvement plan",
    "Award of contract for landscaping services at Tesco Plaza",
    "Approve minutes of the regular meeting of July 14",
    "Adopt a resolution proclaiming Fire Prevention Week",
    "Consider a fresco restoration grant for the historic courthouse",
    "Approve escrow instructions for the sale of surplus property",
]


@pytest.mark.parametrize("text", AWARDS)
def test_esco_awards_pass_the_fetch_gate(text, cfg):
    assert keyword_match(text, cfg)
    assert keyword_category(text, cfg) == "esco"


@pytest.mark.parametrize("text", NOT_AWARDS)
def test_ordinary_agenda_business_does_not(text, cfg):
    assert not keyword_match(text, cfg)
    assert keyword_category(text, cfg) is None


@pytest.mark.parametrize("text", [
    "Escondido General Plan amendment",     # contains "esco" as a substring
    "Tesco Extra store frontage variance",
    "Fresco mural conservation contract",
    "Approve escrow agreement for parcel 123",
])
def test_esco_is_matched_on_word_boundaries(text, cfg):
    """"esco" is four letters that live inside ordinary agenda words.

    Escondido alone appears constantly in Southern California agendas. Substring
    matching would put all of it through the narrowest funnel in the system.
    """
    assert not keyword_match(text, cfg)


def test_building_keywords_still_win_the_category(cfg):
    """An ESPC award at a warehouse is still best described by the existing board.

    esco is checked last on purpose: routing a keyword-matched building project to
    a retrofit category it has no board for would quietly lose it.
    """
    text = "Energy Services Agreement for the municipal distribution center"
    assert keyword_match(text, cfg)
    assert keyword_category(text, cfg) == "industrial"

    text = "Energy Services Agreement for the county data center"
    assert keyword_category(text, cfg) == "data_center"


def test_existing_building_keywords_are_unaffected(cfg):
    """Adding a third list must not disturb the two the boards run on."""
    assert keyword_category("Proposed hyperscale data center campus", cfg) == "data_center"
    assert keyword_category("New 500,000 sf fulfillment center", cfg) == "industrial"
    assert not keyword_match("Sidewalk repair on Elm Street", cfg)


# --- the tag reaches the stored document -----------------------------------

def test_legistar_records_which_list_matched(cfg):
    """Without the tag, finding an ESCO award later means re-reading every item."""
    from app.sources.legistar import LegistarAdapter

    matter = {"MatterId": 4242, "MatterTitle": "Approve an Energy Services Agreement "
                                               "with Ameresco, Inc.",
              "MatterName": "", "MatterBodyName": "Board of Supervisors",
              "MatterTypeName": "Contract", "MatterFile": "25-1234",
              "MatterIntroDate": "2026-07-01T00:00:00"}
    doc = LegistarAdapter()._to_doc(matter, "clark", "Clark County", cfg)
    assert doc is not None
    assert doc.meta["keyword_category"] == "esco"


def test_legistar_still_drops_unrelated_matters(cfg):
    from app.sources.legistar import LegistarAdapter

    matter = {"MatterId": 1, "MatterTitle": "Proclaim Arbor Day", "MatterName": "",
              "MatterBodyName": "Council", "MatterIntroDate": "2026-07-01T00:00:00"}
    assert LegistarAdapter()._to_doc(matter, "clark", "Clark County", cfg) is None


# ---- esco is a triage CATEGORY, not just a keyword tag ---------------------
#
# Detection that ends in `other` captures nothing. Triage keeps only what it can
# name: every label outside the Category enum falls to `other`, and `other` means
# discarded. The keyword gate was already storing these documents and triage was
# about to throw every one of them away — the same problem the industrial board
# had before it was split out.

def test_esco_is_a_category():
    from app.models import Category
    assert Category("esco") is Category.esco
    assert Category.esco is not Category.other


def test_esco_is_offered_to_the_model():
    """A category the tool schema does not list cannot be returned."""
    import re

    from app.llm import TRIAGE_SYSTEM, TRIAGE_TOOL
    assert "esco" in TRIAGE_TOOL["input_schema"]["properties"]["category"]["enum"]
    # Whitespace-normalised: the prompt is hard-wrapped, so a phrase can straddle
    # a newline and a plain substring check would fail on formatting alone.
    flat = re.sub(r"\s+", " ", TRIAGE_SYSTEM.lower())
    assert "energy savings performance contract" in flat
    assert "espc" in flat
    # and it must say what makes the category worth keeping, not merely name it
    assert "selects the equipment" in flat


def test_esco_survives_triage(db_session, monkeypatch):
    """End to end: an ESPC verdict is kept as relevant, not dropped as other."""
    from app.models import RawDocument, TriageResult
    from app.pipeline.triage import run_triage

    doc = RawDocument(source="legistar", source_uid="clark:9001",
                      url="https://clark.legistar.com/x", title="ESPC award",
                      raw_text="Approve an Energy Services Agreement with Ameresco",
                      content_hash="esco1",
                      meta={"keyword_category": "esco"})
    db_session.add(doc)
    db_session.commit()
    monkeypatch.setattr("app.pipeline.triage.triage",
                        lambda text, title="", source="": {
                            "category": "esco", "names_location": True,
                            "reason": "ESPC award, Clark County facilities",
                            "_coverage": 1.0})
    stats = run_triage(db_session, load_cfg := __import__(
        "app.config", fromlist=["load_config"]).load_config(), limit=10)
    del load_cfg
    db_session.refresh(doc)
    assert doc.triage_result is TriageResult.relevant
    assert doc.meta["triage_category"] == "esco"
    assert stats["esco"] == 1


def test_esco_never_takes_the_data_center_sizing_path(cfg):
    """A retrofit has no IT load, and any megawatt figure in the document is far
    more likely to be the agency's annual electricity purchase.

    Falling through to the data-center branch would turn such a number into
    thousands of tons of chiller on a building that may need none.
    """
    from app.models import Category
    from app.pipeline.sizing import estimate_tons

    # a stray MW figure must NOT become tonnage
    est = estimate_tons(cfg, mw_it=25.0, category=Category.esco)
    assert est.low is None and est.high is None

    # a data center with the same number does get sized — the branch is real
    dc = estimate_tons(cfg, mw_it=25.0, category=Category.data_center)
    assert dc.low is not None and dc.low > 0

    # with a stated area, esco uses the envelope path like industrial
    sized = estimate_tons(cfg, building_sqft=250_000, category=Category.esco)
    ind = estimate_tons(cfg, building_sqft=250_000, category=Category.industrial)
    assert sized.low == ind.low and sized.high == ind.high


def test_esco_does_not_dilute_the_construction_boards():
    """"Both boards" means the two construction boards, not every category."""
    from app.models import Category
    assert Category.boards() == (Category.data_center, Category.industrial)
    assert Category.esco not in Category.boards()
