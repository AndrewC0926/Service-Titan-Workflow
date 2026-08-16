"""Display constants for Project.delivery_method -- see that field's
docstring in app/models.py for what gets stored and why (extracted from
filing text only, never inferred, null far more often than not).

DELIVERY_METHOD_NOTES answers the question this field exists to answer: who
actually selects the mechanical equipment, and is the right call to the
engineer of record or to the mechanical contractor. General industry
practice (AIA/DBIA project-delivery definitions), not project-specific --
see app/assumptions.py's "Project delivery method" entry for the source
classification.
"""
from __future__ import annotations

DELIVERY_METHOD_ORDER = (
    "design_bid_build", "design_build", "design_assist", "cm_at_risk",
    "progressive_design_build",
)

DELIVERY_METHOD_LABELS = {
    "design_bid_build": "Design-bid-build",
    "design_build": "Design-build",
    "design_assist": "Design-assist",
    "cm_at_risk": "CM at risk",
    "progressive_design_build": "Progressive design-build",
}

# Short enough to sit as a badge next to a project name on the board.
DELIVERY_METHOD_ABBR = {
    "design_bid_build": "DBB",
    "design_build": "DB",
    "design_assist": "DA",
    "cm_at_risk": "CMAR",
    "progressive_design_build": "PDB",
}

DELIVERY_METHOD_NOTES = {
    "design_bid_build": "Owner hires the engineer to design the project fully, then bids the "
        "finished design to a contractor. The engineer of record writes Division 23 and "
        "names a basis of design — the right call is to the MEP firm.",
    "design_build": "One entity holds both design and construction. The mechanical "
        "contractor typically selects the equipment, often before a specification exists "
        "at all — the right call is to the contractor.",
    "design_assist": "The mechanical contractor joins the design team early, before a full "
        "specification exists, and typically drives the equipment selection working "
        "alongside the engineer — the right call is to the contractor.",
    "cm_at_risk": "A construction manager holds the GC contract and often bids trade "
        "packages, including mechanical, before design is complete. Selection can land "
        "with either the CM's preferred mechanical sub or the engineer, depending on how "
        "that trade package is structured — confirm which before calling.",
    "progressive_design_build": "Design-build where scope and price are negotiated in "
        "phases rather than fixed at award. The design-build team (often paired with a "
        "mechanical contractor from day one) usually drives equipment selection early — "
        "confirm who on the team owns Division 23.",
}
