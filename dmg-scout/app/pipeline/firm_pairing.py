"""WS2 (Build Plan v2.1): seed FirmPairing with the plan's own five verified
public pairings plus every design-builder/architect/MEP pairing WS3.2's
document sample actually surfaced (Block 1 and Block 2 passes both).

Idempotent: re-running skips a pairing already in the table
(uq_firm_pairing on design_builder_firm_id/partner_firm_id/partner_role) and
reuses an existing Firm row (matched via app.firms.match_firm, alias-aware
normalized name) rather than creating a duplicate.

SOURCE DISCIPLINE, by row group below:
  - "build_plan_v2.1_seed" (6 rows): the plan document's own already-
    verified claim. No source_url -- none was given to re-cite, and this
    module does not fabricate one. observed_date is also None for the same
    reason.
  - "ws3.2_document" (10 rows): a document this session directly fetched
    (WebFetch or Read on a saved PDF) and read itself -- source_url is that
    real, checked document.
  - "ws3.2_document_corroborated" (1 row, McCarthy/SmithGroup): the fact
    was NOT directly fetched from a single page (the primary ENR West
    article returned HTTP 403) but was corroborated by multiple independent
    search-result snippets in agreement (McCarthy's own site, SmithGroup's
    own site, PRNewswire, Zweig Group, Tradeline) -- same
    "cross-source corroborated" standard the Build Plan's WS10 constraints
    describe for agent claims, distinguished here from a direct fetch
    rather than blurred into it.
"""
from __future__ import annotations

from datetime import datetime

from sqlmodel import Session, select

from app.firms import match_firm
from app.models import Firm, FirmPairing
from app.normalize import normalize_company_name

# firm_type for a newly-created Firm, by partner_role -- same map
# app.firms.ROLE_TO_TYPE already uses for ProjectFirm roles.
_ROLE_TO_FIRM_TYPE = {
    "architect": "architect",
    "mep_engineer": "mep",
    "mech_contractor": "mech_contractor",
    "structural_engineer": "structural",
    "gc": "gc",
}

# (design_builder_name, partner_name, partner_role, source, source_url, observed_date, note)
_SEED_PAIRINGS: list[tuple[str, str, str, str, str | None, datetime | None, str]] = [
    # -- the plan's own five verified public pairings (six rows: Harbor-UCLA contributes two) --
    ("Hensel Phelps", "P2S", "mep_engineer", "build_plan_v2.1_seed", None, None,
     "Harbor-UCLA"),
    ("Hensel Phelps", "ACCO Engineered Systems", "mech_contractor", "build_plan_v2.1_seed", None, None,
     "Harbor-UCLA"),
    ("Rudolph and Sletten", "WSP", "mep_engineer", "build_plan_v2.1_seed", None, None,
     "San Diego courthouse"),
    ("Clark Construction", "Syska Hennessy Group", "mep_engineer", "build_plan_v2.1_seed", None, None,
     "LA federal courthouse"),
    ("Webcor", "Frank M. Booth", "mech_contractor", "build_plan_v2.1_seed", None, None,
     "UC Merced"),
    ("Hensel Phelps", "CO Architects", "architect", "build_plan_v2.1_seed", None, None,
     "UC higher-ed"),

    # -- WS3.2 document sample, Block 1 (directly fetched: DBIA + UCI's own
    #    Design & Construction Services page) --
    ("Hathaway Dinwiddie Construction Company", "LMN Architects", "architect",
     "ws3.2_document",
     "https://www.designandconstruction.uci.edu/projects/falling-leaves-foundation-medical-innovation-building/index.php",
     None, "UC Irvine Falling Leaves Foundation Medical Innovation Building"),
    ("Hathaway Dinwiddie Construction Company", "Alvine Engineering", "mep_engineer",
     "ws3.2_document",
     "https://www.designandconstruction.uci.edu/projects/falling-leaves-foundation-medical-innovation-building/index.php",
     None, "UC Irvine Falling Leaves Foundation Medical Innovation Building"),

    # -- WS3.2 document sample, Block 2 (directly fetched) --
    ("Rudolph and Sletten", "Nacht & Lewis Architects", "architect",
     "ws3.2_document",
     "https://health.ucdavis.edu/media-resources/facilities/documents/pdfs/CUP/RFQs/rfq-cup-code-peer-review.pdf",
     datetime(2023, 9, 15), "UC Davis Health Central Utility Plant Expansion"),
    ("Suffolk", "CO Architects", "architect",
     "ws3.2_document",
     "https://www.californiaconstructionnews.com/2026/09/07/suffolk-co-architects-selected-for-ucsf-mission-bay-education-center-and-dental-clinics/",
     datetime(2026, 9, 7), "UCSF Mission Bay Education Center and Dental Clinics"),
    ("Gilbane Construction Company", "Gensler", "architect",
     "ws3.2_document",
     "https://w2.csun.edu/FPDC/facilities-planning-design-construction-fpdc/project-information",
     None, "CSUN Sierra Annex"),
    ("DPR Construction", "Steinberg Hart", "architect",
     "ws3.2_document",
     "https://w2.csun.edu/FPDC/facilities-planning-design-construction-fpdc/project-information",
     None, "CSUN Matador Success and Inclusion Center"),
    ("Hensel Phelps", "ZGF Architects", "architect",
     "ws3.2_document",
     "https://www.californiaconstructionnews.com/2025/06/23/uc-riverside-building-socal-oasis-innovation-park-on-3-4-acre-site/",
     datetime(2025, 6, 23), "UC Riverside SoCal OASIS Innovation Park"),

    # -- WS3.2 document sample, Block 2 (cross-source corroborated, not a
    #    single direct fetch -- see module docstring) --
    ("McCarthy Building Companies", "SmithGroup", "architect",
     "ws3.2_document_corroborated", None, None,
     "UC Davis Health California Tower"),
]


def _get_or_create_firm(session: Session, name: str, role: str) -> Firm:
    firm = match_firm(session, name)
    if firm is not None:
        return firm
    norm = normalize_company_name(name)
    firm = Firm(name=name, name_norm=norm, firm_type=_ROLE_TO_FIRM_TYPE.get(role, "unknown"),
               added_from="ws2_firm_pairing_seed")
    session.add(firm)
    session.flush()
    return firm


def seed_verified_firm_pairings(session: Session) -> dict:
    """Idempotent. Returns {"inserted": n, "skipped_existing": n,
    "firms_created": n}."""
    inserted = skipped = firms_created = 0
    for db_name, partner_name, role, source, source_url, observed_date, _note in _SEED_PAIRINGS:
        before = session.exec(select(Firm.id)).all()
        n_firms_before = len(before)

        db_firm = _get_or_create_firm(session, db_name, "gc")
        partner_firm = _get_or_create_firm(session, partner_name, role)

        after = session.exec(select(Firm.id)).all()
        firms_created += len(after) - n_firms_before

        existing = session.exec(
            select(FirmPairing).where(
                FirmPairing.design_builder_firm_id == db_firm.id,
                FirmPairing.partner_firm_id == partner_firm.id,
                FirmPairing.partner_role == role,
            )
        ).first()
        if existing:
            skipped += 1
            continue

        session.add(FirmPairing(
            design_builder_firm_id=db_firm.id, partner_firm_id=partner_firm.id,
            partner_role=role, source=source, source_url=source_url,
            observed_date=observed_date,
        ))
        inserted += 1

    session.commit()
    return {"inserted": inserted, "skipped_existing": skipped, "firms_created": firms_created}
