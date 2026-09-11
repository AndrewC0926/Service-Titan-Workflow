"""WS3.1 (Build Plan v2.1): app.pipeline.procurement_delivery's config-driven
classifier for Project.delivery_method_class / pen_holder_role. Every test
here fails against pre-WS3.1 code -- there was no such module, no such
enum, no such Project column at all; these are the regression tests the
Build Plan item asked for, guarding the ABSTAIN-first discipline going
forward."""
from app.models import DeliveryMethodClass, PenHolderRole, Project
from app.pipeline.procurement_delivery import (
    classify_delivery_method_class, classify_pen_holder_role,
)


# --- Project column defaults ------------------------------------------------


def test_project_delivery_method_class_defaults_to_abstain():
    p = Project(name="Test")
    assert p.delivery_method_class == DeliveryMethodClass.ABSTAIN


def test_project_pen_holder_role_defaults_to_abstain():
    p = Project(name="Test")
    assert p.pen_holder_role == PenHolderRole.ABSTAIN


# --- classify_delivery_method_class: source gating --------------------------


def test_delivery_method_ineligible_source_always_abstains(cfg):
    """An entitlement source -- ceqanet is not on procurement_delivery.sources
    -- ABSTAINs even when the text is unambiguous design-build language.
    This is the exact rule the item was written to enforce: entitlement
    documents always ABSTAIN, never inferred, however clear the text."""
    text = "This project uses a design-build delivery method with a single entity."
    assert classify_delivery_method_class("ceqanet", text, cfg) == DeliveryMethodClass.ABSTAIN


def test_delivery_method_unlisted_source_abstains(cfg):
    assert classify_delivery_method_class("some_future_source", "design-build", cfg) \
        == DeliveryMethodClass.ABSTAIN


# --- classify_delivery_method_class: eligible source, keyword matching ------


def test_delivery_method_eligible_source_no_keyword_is_unknown(cfg):
    """legistar IS eligible, but nothing in the text matches any configured
    phrase -- unknown (attempted, inconclusive), not ABSTAIN (never tried)."""
    assert classify_delivery_method_class("legistar", "Approve the parking lot resurfacing contract.", cfg) \
        == DeliveryMethodClass.unknown


def test_delivery_method_design_bid_build_matches(cfg):
    assert classify_delivery_method_class(
        "legistar", "Award of contract under design-bid-build delivery.", cfg
    ) == DeliveryMethodClass.design_bid_build


def test_delivery_method_design_build_gc_matches(cfg):
    assert classify_delivery_method_class(
        "legistar", "Approve the design-build contractor agreement for the new wing.", cfg
    ) == DeliveryMethodClass.design_build_gc


def test_delivery_method_design_build_trade_matches(cfg):
    assert classify_delivery_method_class(
        "legistar", "Mechanical trade design-build package for HVAC replacement.", cfg
    ) == DeliveryMethodClass.design_build_trade


def test_delivery_method_cmar_matches(cfg):
    assert classify_delivery_method_class(
        "legistar", "Retain a construction manager at risk for the new facility.", cfg
    ) == DeliveryMethodClass.cmar


def test_delivery_method_p3_matches(cfg):
    assert classify_delivery_method_class(
        "legistar", "Enter into a public-private partnership for the parking structure.", cfg
    ) == DeliveryMethodClass.p3


def test_delivery_method_progressive_design_build_matches(cfg):
    """Regression guard for the substring-collision fix: progressive
    design-build text must not misclassify as design_build_gc just because
    "design-build" is a literal substring of "progressive design-build".
    Config order (checked in app/pipeline/procurement_delivery.py) is what
    prevents this -- see config.yaml's procurement_delivery.keywords
    comment."""
    assert classify_delivery_method_class(
        "legistar", "Board approves a progressive design-build contract for the annex.", cfg
    ) == DeliveryMethodClass.progressive_design_build


# --- classify_pen_holder_role -----------------------------------------------


def test_pen_holder_role_ineligible_source_abstains(cfg):
    assert classify_pen_holder_role(
        "ceqanet", "The mechanical engineer of record prepared the basis of design.", cfg
    ) == PenHolderRole.ABSTAIN


def test_pen_holder_role_eligible_source_no_keyword_is_unknown(cfg):
    assert classify_pen_holder_role("legistar", "Approve the parking lot contract.", cfg) \
        == PenHolderRole.unknown


def test_pen_holder_role_consulting_me_matches(cfg):
    assert classify_pen_holder_role(
        "legistar", "The consulting mechanical engineer completed the design.", cfg
    ) == PenHolderRole.consulting_me


def test_pen_holder_role_design_builder_matches(cfg):
    assert classify_pen_holder_role(
        "legistar", "The design-build entity will select all mechanical equipment.", cfg
    ) == PenHolderRole.design_builder


def test_pen_holder_role_owner_standards_matches(cfg):
    assert classify_pen_holder_role(
        "legistar", "All work must follow the owner's standard specification.", cfg
    ) == PenHolderRole.owner_standards
