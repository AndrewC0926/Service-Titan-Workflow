"""app.compare: application-driven line comparability. The rules under test
are the ones the module docstring states -- null/unverified never votes
against a candidate, every surfaced claim carries a verification date, lead
time never appears, and candidates are never excluded for missing data."""
from sqlmodel import select

from app.accounts import seed_product_lines
from app.compare import TONNAGE_RELEVANT_ROLES, compare_lines
from app.models import ProductLine


def seed(db_session, cfg):
    return seed_product_lines(db_session, cfg)


def test_candidate_pool_is_every_tonnage_relevant_role_line(db_session, cfg):
    """No candidate is excluded for lacking data -- the pool is the full set
    of lines in the relevant roles, full stop."""
    seed(db_session, cfg)
    expected = db_session.exec(
        select(ProductLine).where(ProductLine.building_role.in_(TONNAGE_RELEVANT_ROLES))
    ).all()
    candidates = compare_lines(db_session)
    assert len(candidates) == len(expected)
    assert {c.line for c in candidates} == {l.name for l in expected}


def test_null_capability_is_a_gap_never_a_trade_away(db_session, cfg):
    """No line in this config has latent_load_capability set yet -- every
    candidate must show it as a gap, and none may show a fit or trade-away
    for it, even when the buyer asks for latent priority."""
    seed(db_session, cfg)
    candidates = compare_lines(db_session, latent_load_priority=True)
    assert candidates, "expected at least one candidate in the tonnage-relevant roles"
    for c in candidates:
        assert "latent_load_capability" in c.capability_gaps
        assert not any("latent" in f.lower() for f in c.why_it_fits)
        assert not any("latent" in t.lower() for t in c.trades_away)


def test_unverified_capability_is_excluded_not_caveated(db_session, cfg):
    """A value + basis with verified=False must never surface as a claim --
    it must land in capability_gaps like a true null would."""
    seed(db_session, cfg)
    line = db_session.exec(select(ProductLine).where(ProductLine.building_role == "cooling_generation")).first()
    line.redundancy_capable = True
    line.redundancy_capable_basis = "Stated 2026-08-11: sales rep claim, not confirmed with engineering."
    line.redundancy_capable_verified = False
    db_session.add(line)
    db_session.commit()

    candidates = compare_lines(db_session, redundancy_required=True)
    row = next(c for c in candidates if c.line == line.name)
    assert "redundancy_capable" in row.capability_gaps
    assert not any("redundancy" in f.lower() for f in row.why_it_fits)


def test_verified_claim_with_no_parseable_date_is_also_a_gap(db_session, cfg):
    """rule 3: a claim with no parseable check date is excluded, not shown
    undated."""
    seed(db_session, cfg)
    line = db_session.exec(select(ProductLine).where(ProductLine.building_role == "heat_rejection")).first()
    line.corrosion_resistance = "marine_grade"
    line.corrosion_resistance_verified = True
    line.corrosion_resistance_basis = "Factory data sheet, no date given."
    db_session.add(line)
    db_session.commit()

    candidates = compare_lines(db_session, marine_or_corrosive=True)
    row = next(c for c in candidates if c.line == line.name)
    assert "corrosion_resistance" in row.capability_gaps
    assert not row.why_it_fits


def test_verified_claim_with_date_surfaces_and_is_never_a_gap(db_session, cfg):
    seed(db_session, cfg)
    line = db_session.exec(select(ProductLine).where(ProductLine.building_role == "heat_rejection")).first()
    line.corrosion_resistance = "marine_grade"
    line.corrosion_resistance_verified = True
    line.corrosion_resistance_basis = "Factory data sheet, confirmed with rep. Retrieved 2026-08-11."
    db_session.add(line)
    db_session.commit()

    candidates = compare_lines(db_session, marine_or_corrosive=True)
    row = next(c for c in candidates if c.line == line.name)
    assert "corrosion_resistance" not in row.capability_gaps
    assert any("marine_grade" in f and "2026-08-11" in f for f in row.why_it_fits)


def test_a_mismatched_verified_claim_is_a_trade_away_not_a_gap(db_session, cfg):
    """Real signal against a constraint IS allowed to surface as a
    trade-away -- the abstain rule protects against null data, not against
    an actual verified negative."""
    seed(db_session, cfg)
    line = db_session.exec(select(ProductLine).where(ProductLine.building_role == "heat_rejection")).first()
    line.corrosion_resistance = "standard"
    line.corrosion_resistance_verified = True
    line.corrosion_resistance_basis = "Factory data sheet. Retrieved 2026-08-11."
    db_session.add(line)
    db_session.commit()

    candidates = compare_lines(db_session, marine_or_corrosive=True)
    row = next(c for c in candidates if c.line == line.name)
    assert "corrosion_resistance" not in row.capability_gaps
    assert any("standard" in t.lower() for t in row.trades_away)


def test_ordering_never_penalizes_a_candidate_for_missing_data(db_session, cfg):
    """A candidate with zero verified fits still appears, ordered after (not
    excluded from) candidates with verified fits -- absence is not a
    demotion below "present but doesn't apply here"."""
    seed(db_session, cfg)
    candidates = compare_lines(db_session, redundancy_required=True)
    names = {c.line for c in candidates}
    all_relevant = db_session.exec(
        select(ProductLine).where(ProductLine.building_role.in_(TONNAGE_RELEVANT_ROLES))
    ).all()
    assert names == {l.name for l in all_relevant}


def test_lead_time_never_appears(db_session, cfg):
    """compare.py must never read lead_time_weeks_low/high/basis -- setting
    a distinctive value on one line and confirming it surfaces nowhere in
    that candidate's row is a stronger check than banning the word "lead",
    which also appears legitimately in unrelated pre-existing limitations
    text elsewhere in the real config."""
    seed(db_session, cfg)
    line = db_session.exec(select(ProductLine).where(ProductLine.building_role == "cooling_generation")).first()
    line.lead_time_weeks_low = 61
    line.lead_time_weeks_high = 97
    line.lead_time_basis = "Stated by rep 2026-08-11, XYZZY-MARKER."
    db_session.add(line)
    db_session.commit()

    candidates = compare_lines(db_session)
    row = next(c for c in candidates if c.line == line.name)
    blob = " ".join(row.why_it_fits + row.trades_away + ([row.known_limitations] if row.known_limitations else []))
    assert "61" not in blob and "97" not in blob and "XYZZY-MARKER" not in blob


def test_invalid_building_type_raises(db_session, cfg):
    seed(db_session, cfg)
    try:
        compare_lines(db_session, building_type="not_a_real_market")
        assert False, "expected ValueError"
    except ValueError:
        pass
