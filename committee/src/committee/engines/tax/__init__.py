"""Tax engine (DESIGN 9): lot ledger, wash-sale guard, harvesting, location, hurdle.

Deterministic Python only. Every human-facing output carries
"Not tax advice. Confirm with a CPA."
"""

from committee.engines.tax.advice import HoldingAdvice, holding_advice, long_term_warnings
from committee.engines.tax.equivalence import DEFAULT_EQUIVALENCE_GROUPS, Equivalence
from committee.engines.tax.harvest import (
    HarvestProposal,
    HarvestScan,
    HarvestSkip,
    harvest_scan,
    journal_harvest_scan,
)
from committee.engines.tax.holding import (
    anniversary,
    days_until_long_term,
    first_long_term_date,
    is_long_term,
    term_of,
)
from committee.engines.tax.hurdle import Hurdle, after_tax_hurdle
from committee.engines.tax.labels import DISCLAIMER
from committee.engines.tax.ledger import LedgerError, LotLedger
from committee.engines.tax.location import LocationRecommendation, recommend_location
from committee.engines.tax.models import (
    LotPick,
    Purchase,
    RealizedLot,
    SaleRequest,
    TaxLot,
    WashMatch,
)
from committee.engines.tax.rates import TaxRates
from committee.engines.tax.report import realized_csv, summarize, write_realized_csv
from committee.engines.tax.selection import LotSelection, order_lots, select_lots
from committee.engines.tax.store import TaxLedgerStore
from committee.engines.tax.wash_sale import (
    PurchaseVerdict,
    WashSaleGuard,
    check_purchase,
    wash_sale_block_list,
)

__all__ = [
    "DEFAULT_EQUIVALENCE_GROUPS",
    "DISCLAIMER",
    "Equivalence",
    "HarvestProposal",
    "HarvestScan",
    "HarvestSkip",
    "HoldingAdvice",
    "Hurdle",
    "LedgerError",
    "LocationRecommendation",
    "LotLedger",
    "LotPick",
    "LotSelection",
    "Purchase",
    "PurchaseVerdict",
    "RealizedLot",
    "SaleRequest",
    "TaxLedgerStore",
    "TaxLot",
    "TaxRates",
    "WashMatch",
    "WashSaleGuard",
    "after_tax_hurdle",
    "anniversary",
    "check_purchase",
    "days_until_long_term",
    "first_long_term_date",
    "harvest_scan",
    "holding_advice",
    "is_long_term",
    "journal_harvest_scan",
    "long_term_warnings",
    "order_lots",
    "realized_csv",
    "recommend_location",
    "select_lots",
    "summarize",
    "term_of",
    "wash_sale_block_list",
    "write_realized_csv",
]
