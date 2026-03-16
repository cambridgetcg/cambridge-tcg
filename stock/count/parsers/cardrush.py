"""CardRush supplier purchase list parser.

Parses TWO Japanese text formats from CardRush order confirmations:

Format A (full receipt):
    〔状態A-〕サボ(パラレル/漫画絵)【L/P】{OP05-001}
    Price: ¥ 680
    Quantity: 4
    Item subtotal: ¥ 2720

Format B (compact list — the raw order table):
    OP01-062[L]：(パラレル)クロコダイル
    Price: ¥ 1,680
    Quantity: 2
    Item subtotal: ¥ 3,360

Items without 〔状態...〕 prefix are mint condition.
Card numbers may include a reprint set bracket: {OP01-051[OP03]}
SP items in format B: OP06-050[SP]：たしぎ (auto-resolved by SKUResolver)
"""

import re
from typing import List

from stock.count.models import ParsedItem
from stock.count.parsers.base import SupplierParser

# Format A: Original full receipt format
# Optional condition: 〔状態A-〕
# Name + variant: サボ(パラレル/漫画絵)
# Rarity: 【L/P】
# Card number with optional reprint set: {OP05-001} or {OP01-051[OP03]}
CARD_LINE_RE = re.compile(
    r'^(?:〔状態(.+?)〕)?(.+?)【(.+?)】\{([A-Z0-9-]+)(?:\[([A-Z0-9]+)\])?\}$'
)

# Format B: Compact list format from CardRush order table
# Card number + rarity bracket + full-width colon + name
# e.g. OP01-062[L]：(パラレル)クロコダイル
#      OP06-050[SP]：たしぎ
COMPACT_LINE_RE = re.compile(
    r'^([A-Z0-9-]+)\[([A-Za-z/]+)\]：(.+)$'
)

PRICE_RE = re.compile(r'^Price:\s*¥\s*([\d,]+)$')
QTY_RE = re.compile(r'^Quantity:\s*(\d+)$')
SUBTOTAL_RE = re.compile(r'^Item subtotal:\s*¥\s*([\d,]+)$')


class CardRushParser(SupplierParser):
    """Parser for CardRush order confirmation text."""

    def __init__(self):
        self.dropped_lines = []

    def parse(self, text: str) -> List[ParsedItem]:
        items = []
        self.dropped_lines = []
        lines = [line.strip() for line in text.strip().splitlines()]

        i = 0
        while i < len(lines):
            line = lines[i]
            if not line:
                i += 1
                continue

            # Try Format A first
            card_match = CARD_LINE_RE.match(line)
            if card_match:
                condition = card_match.group(1)
                name_jp = card_match.group(2).strip()
                rarity = card_match.group(3).strip()
                card_number = card_match.group(4).strip()
                reprint_set = card_match.group(5)
            else:
                # Try Format B (compact)
                compact_match = COMPACT_LINE_RE.match(line)
                if compact_match:
                    card_number = compact_match.group(1).strip()
                    rarity = compact_match.group(2).strip()
                    name_jp = compact_match.group(3).strip()
                    condition = None
                    reprint_set = None  # SP auto-resolved by SKUResolver
                else:
                    # Not a card line — check if it's a data line (Price/Qty/Subtotal)
                    if not (PRICE_RE.match(line) or QTY_RE.match(line) or SUBTOTAL_RE.match(line)):
                        self.dropped_lines.append(line)
                    i += 1
                    continue

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

                # Unrecognized line — stop looking for this item's fields
                break

            if price_yen is not None and quantity is not None and subtotal_yen is not None:
                item = ParsedItem(
                    card_number=card_number,
                    name_jp=name_jp,
                    rarity=rarity,
                    condition=condition,
                    quantity=quantity,
                    price_yen=price_yen,
                    subtotal_yen=subtotal_yen,
                    reprint_set=reprint_set,
                    raw_text='\n'.join(raw_lines),
                )
                # Validate subtotal
                warnings = self.validate_item(item)
                if warnings:
                    for w in warnings:
                        print(f"  WARN: {w}")
                items.append(item)
                i = j
            else:
                # Incomplete item — skip this card line
                self.dropped_lines.append(line)
                i += 1

        return items
