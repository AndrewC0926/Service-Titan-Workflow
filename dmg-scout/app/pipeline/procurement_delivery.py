"""WS3.1 (Build Plan v2.1): config-driven, no-LLM classification of
Project.delivery_method_class / Project.pen_holder_role.

NAMING CONFLICT, RESOLVED (Build Plan v2.1, Block 2): `Project.delivery_method`
(plain string) already existed when this module was first written, populated
by the universal LLM extraction loop (app.pipeline.extract) from ANY
triaged-relevant document, entitlement filings included, and read live by
app.call_target's R2 rule -- exactly the "Delivery method from entitlement
documents" practice the Build Plan's own Kill List (section 5) names as
something not to rebuild. Block 1 disclosed the conflict without touching
that column; Block 2's own human decision resolved it: delivery_method_class
and pen_holder_role are now the ONLY delivery fields any rule may read, R2
was switched over (app.call_target), and the legacy column was renamed to
Project.delivery_method_llm_hint and made display-only -- migration
a3d719c04b5e, every rule-code read removed, guarded by
tests/test_call_target.py's test_no_rule_code_reads_the_legacy_delivery_method_hint.

RULE: a Project's delivery_method_class/pen_holder_role only ever comes from
a Signal whose RawDocument.source is on the procurement_delivery.sources /
pen_holder_role.sources allowlist in config.yaml (today: just `legistar`
-- courts.ca.gov and Cal eProcure are named in the plan but neither is
actually ingested, checked directly against app/sources/). A signal from
any other source -- which, for delivery method, means every entitlement
source that exists in this codebase today -- always classifies ABSTAIN,
never guessed at, never inferred from project type, agency, or stage.

`unknown` vs `ABSTAIN`: ABSTAIN means classification was never attempted
(source not eligible). `unknown` means the source WAS eligible and the text
was checked but no keyword in config.yaml matched -- a real, attempted
classification that came up empty. This distinction matters for the board
distribution report: a low `unknown` share means the keyword map is doing
its job on the sources that reach it; a high `unknown` share is a signal to
widen the map, whereas ABSTAIN dominating just means most sources aren't
procurement solicitations at all, which is expected and not a gap.
"""
from __future__ import annotations

from app.config import Config
from app.models import DeliveryMethodClass, PenHolderRole


def classify_delivery_method_class(source: str, text: str, cfg: Config) -> DeliveryMethodClass:
    """source is the RawDocument/Signal's origin (e.g. "legistar"), text is
    the raw document text to scan. First keyword-map entry (in config.yaml
    order) whose phrase appears in `text` (case-insensitive substring) wins
    -- config.yaml's own comment documents why order matters (more specific
    phrasings must be checked before the generic ones they're substrings
    of)."""
    allowed_sources = cfg.get("procurement_delivery.sources") or []
    if source not in allowed_sources:
        return DeliveryMethodClass.ABSTAIN

    lower = text.lower()
    keyword_map: dict = cfg.get("procurement_delivery.keywords") or {}
    for method_name, phrases in keyword_map.items():
        if any(str(phrase).lower() in lower for phrase in phrases):
            try:
                return DeliveryMethodClass(method_name)
            except ValueError:
                continue  # config names a method_name this enum doesn't have -- never guess
    return DeliveryMethodClass.unknown


def classify_pen_holder_role(source: str, text: str, cfg: Config) -> PenHolderRole:
    """Same source-gating and first-match-wins discipline as
    classify_delivery_method_class above, its own config.yaml section
    (pen_holder_role.sources / pen_holder_role.keywords)."""
    allowed_sources = cfg.get("pen_holder_role.sources") or []
    if source not in allowed_sources:
        return PenHolderRole.ABSTAIN

    lower = text.lower()
    keyword_map: dict = cfg.get("pen_holder_role.keywords") or {}
    for role_name, phrases in keyword_map.items():
        if any(str(phrase).lower() in lower for phrase in phrases):
            try:
                return PenHolderRole(role_name)
            except ValueError:
                continue
    return PenHolderRole.unknown
