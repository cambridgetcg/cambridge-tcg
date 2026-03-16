"""Data structures for the stock purchase pipeline."""

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class ParsedItem:
    """A single item parsed from a supplier purchase list."""
    card_number: str          # e.g. "OP05-001"
    name_jp: str              # e.g. "サボ(パラレル/漫画絵)"
    rarity: str               # e.g. "L/P"
    condition: Optional[str]  # e.g. "A-" or None (mint)
    quantity: int
    price_yen: int
    subtotal_yen: int
    reprint_set: Optional[str] = None  # e.g. "OP03" from {OP01-051[OP03]}
    raw_text: str = ""


@dataclass
class ResolvedItem:
    """A parsed item with SKU resolution applied."""
    parsed: ParsedItem
    sku: Optional[str] = None
    resolved: bool = False
    ambiguous: bool = False
    warnings: list = field(default_factory=list)


@dataclass
class StockUpdate:
    """An aggregated stock update for a single SKU."""
    sku: str
    quantity_to_add: int
    cost_yen_total: int  # total cost for this batch (qty * price may vary across lines)


@dataclass
class SaleReduction:
    """A stock reduction from a sale on Shopify or eBay."""
    sku: str
    quantity_sold: int       # always positive
    platform: str = ''       # 'shopify' or 'ebay'
    order_id: str = ''


@dataclass
class StockRecord:
    """A single SKU's stock state in the store."""
    sku: str
    quantity: int
    total_cost_yen: int
    last_updated: str  # ISO timestamp
    purchased_qty: int = 0
