"""Abstract base class for supplier purchase list parsers."""

from abc import ABC, abstractmethod
from typing import List

from stock.count.models import ParsedItem


class SupplierParser(ABC):
    """Base class for supplier-specific purchase list parsers."""

    @abstractmethod
    def parse(self, text: str) -> List[ParsedItem]:
        """Parse raw purchase list text into structured items.

        Args:
            text: Raw text copied from supplier order confirmation.

        Returns:
            List of ParsedItem dataclass instances.
        """
        ...

    def validate_item(self, item: ParsedItem) -> List[str]:
        """Validate a parsed item. Returns list of warning strings."""
        warnings = []
        expected = item.price_yen * item.quantity
        if item.subtotal_yen != expected:
            warnings.append(
                f"Subtotal mismatch for {item.card_number}: "
                f"expected {expected} (={item.price_yen}x{item.quantity}), "
                f"got {item.subtotal_yen}"
            )
        if item.quantity <= 0:
            warnings.append(f"Invalid quantity for {item.card_number}: {item.quantity}")
        if item.price_yen <= 0:
            warnings.append(f"Invalid price for {item.card_number}: {item.price_yen}")
        return warnings
