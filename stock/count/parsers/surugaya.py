"""Suruga-ya supplier purchase list parser.

Parses the text format from Suruga-ya order confirmations:

    OP07-019[L]：(パラレル)ジュエリー・ボニー
    Price: ¥ 1480
    Quantity: 10
    Item subtotal: ¥ 14800

Variant with repeated card info:

    OP07-019[L]：【OP07-019】ジュエリー・ボニー(金箔押し)「L」
    Price: ¥ 1280
    Quantity: 1
    Item subtotal: ¥ 1280

No condition field — all items treated as mint.
No reprint set bracket — SP cards not distinguished in this format.
"""

import re
from typing import List

from stock.count.models import ParsedItem
from stock.count.parsers.base import SupplierParser

# Matches the card description line:
#   Card number: OP07-019
#   Rarity in brackets: [L]
#   Full-width colon separator: ：
#   Rest is the name (with optional variant info)
CARD_LINE_RE = re.compile(
    r'^([A-Z0-9-]+)\[([^\]]+)\]：(.+)$'
)

PRICE_RE = re.compile(r'^Price:\s*¥\s*([\d,]+)$')
QTY_RE = re.compile(r'^Quantity:\s*(\d+)$')
SUBTOTAL_RE = re.compile(r'^Item subtotal:\s*¥\s*([\d,]+)$')


class SurugayaParser(SupplierParser):
    """Parser for Suruga-ya order confirmation text."""

    def parse(self, text: str) -> List[ParsedItem]:
        items = []
        lines = [line.strip() for line in text.strip().splitlines()]

        i = 0
        while i < len(lines):
            line = lines[i]
            if not line:
                i += 1
                continue

            card_match = CARD_LINE_RE.match(line)
            if not card_match:
                i += 1
                continue

            card_number = card_match.group(1).strip()
            rarity = card_match.group(2).strip()
            name_jp = card_match.group(3).strip()

            # Expect Price, Quantity, Subtotal on the next 3 non-empty lines
            price_yen = None
            quantity = None
            subtotal_yen = None
            raw_lines = [line]

            j = i + 1
            while j < len(lines) and (price_yen is None or quantity is None or subtotal_yen is None):
                next_line = lines[j].strip()
                if not next_line:
                    j += 1
                    continue

                raw_lines.append(next_line)

                if price_yen is None:
                    pm = PRICE_RE.match(next_line)
                    if pm:
                        price_yen = int(pm.group(1).replace(',', ''))
                        j += 1
                        continue

                if quantity is None:
                    qm = QTY_RE.match(next_line)
                    if qm:
                        quantity = int(qm.group(1))
                        j += 1
                        continue

                if subtotal_yen is None:
                    sm = SUBTOTAL_RE.match(next_line)
                    if sm:
                        subtotal_yen = int(sm.group(1).replace(',', ''))
                        j += 1
                        continue

                break

            if price_yen is not None and quantity is not None and subtotal_yen is not None:
                item = ParsedItem(
                    card_number=card_number,
                    name_jp=name_jp,
                    rarity=rarity,
                    condition=None,  # Suruga-ya has no condition field
                    quantity=quantity,
                    price_yen=price_yen,
                    subtotal_yen=subtotal_yen,
                    reprint_set=None,  # Suruga-ya has no reprint set brackets
                    raw_text='\n'.join(raw_lines),
                )
                warnings = self.validate_item(item)
                if warnings:
                    for w in warnings:
                        print(f"  WARN: {w}")
                items.append(item)
                i = j
            else:
                i += 1

        return items
