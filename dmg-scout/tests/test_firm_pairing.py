"""WS2 (Build Plan v2.1): FirmPairing table and its seed function
(app.pipeline.firm_pairing.seed_verified_firm_pairings)."""
from sqlmodel import select

from app.models import Firm, FirmPairing
from app.pipeline.firm_pairing import _SEED_PAIRINGS, seed_verified_firm_pairings


def test_seed_creates_firms_that_do_not_exist_yet(db_session):
    stats = seed_verified_firm_pairings(db_session)
    assert stats["firms_created"] > 0
    hensel = db_session.exec(select(Firm).where(Firm.name == "Hensel Phelps")).first()
    assert hensel is not None
    assert hensel.firm_type == "gc"


def test_seed_reuses_an_existing_firm_by_normalized_name(db_session):
    """ACCO Engineered Systems already exists in the roster under that exact
    name in real data -- the seed must match it via normalize_company_name,
    not create a second row for the same company."""
    existing = Firm(name="ACCO Engineered Systems", name_norm="acco engineered systems",
                    firm_type="mech_contractor")
    db_session.add(existing)
    db_session.commit()

    seed_verified_firm_pairings(db_session)

    rows = db_session.exec(select(Firm).where(Firm.name == "ACCO Engineered Systems")).all()
    assert len(rows) == 1
    assert rows[0].id == existing.id


def test_seed_writes_every_pairing_exactly_once(db_session):
    stats = seed_verified_firm_pairings(db_session)
    assert stats["inserted"] == len(_SEED_PAIRINGS)
    assert db_session.exec(select(FirmPairing)).all().__len__() == len(_SEED_PAIRINGS)


def test_seed_is_idempotent_on_rerun(db_session):
    first = seed_verified_firm_pairings(db_session)
    second = seed_verified_firm_pairings(db_session)
    assert first["inserted"] == len(_SEED_PAIRINGS)
    assert second["inserted"] == 0
    assert second["skipped_existing"] == len(_SEED_PAIRINGS)
    # no duplicate Firm rows on the re-run either
    assert second["firms_created"] == 0


def test_hensel_phelps_harbor_ucla_both_partners_present(db_session):
    """The plan's own example: Hensel Phelps pairs with BOTH P2S (MEP) and
    ACCO (mech_contractor) on the same design-builder -- two distinct rows,
    not one overwriting the other."""
    seed_verified_firm_pairings(db_session)
    hensel = db_session.exec(select(Firm).where(Firm.name == "Hensel Phelps")).first()
    pairings = db_session.exec(
        select(FirmPairing).where(FirmPairing.design_builder_firm_id == hensel.id)
    ).all()
    roles_and_partners = {
        (p.partner_role, db_session.get(Firm, p.partner_firm_id).name) for p in pairings
    }
    assert ("mep_engineer", "P2S") in roles_and_partners
    assert ("mech_contractor", "ACCO Engineered Systems") in roles_and_partners
    assert ("architect", "CO Architects") in roles_and_partners
    assert ("architect", "ZGF Architects") in roles_and_partners


def test_rudolph_and_sletten_ws32_pairing_has_a_real_source_url(db_session):
    """Every ws3.2_document row must carry a real source_url -- the plan
    document's own six seed rows are the only ones allowed a null one (no
    URL was given to re-cite for those)."""
    seed_verified_firm_pairings(db_session)
    ws32_rows = db_session.exec(
        select(FirmPairing).where(FirmPairing.source == "ws3.2_document")
    ).all()
    assert len(ws32_rows) > 0
    for row in ws32_rows:
        assert row.source_url and row.source_url.startswith("https://")


def test_plan_seed_rows_have_no_fabricated_source_url(db_session):
    seed_verified_firm_pairings(db_session)
    plan_rows = db_session.exec(
        select(FirmPairing).where(FirmPairing.source == "build_plan_v2.1_seed")
    ).all()
    assert len(plan_rows) == 6
    for row in plan_rows:
        assert row.source_url is None
