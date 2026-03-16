"""HTML description generator for eBay listings.

Generates standardized, clean HTML descriptions from card metadata
(SKU, title, item specifics). Applied uniformly across all listings
for consistent branding.
"""

from stock.sync.ebay.item_specifics import parse_sku

# Store name used in description footer
STORE_NAME = 'Cambridge TCG'

DESCRIPTION_TEMPLATE = """\
<div style="font-family: Arial, Helvetica, sans-serif; max-width: 600px; margin: 0 auto; padding: 16px;">
  <h2 style="margin: 0 0 12px 0; font-size: 18px; color: #333;">{card_name}</h2>
  <table style="border-collapse: collapse; width: 100%; margin-bottom: 16px;">
    <tr><td style="padding: 6px 12px; background: #f5f5f5; font-weight: bold; width: 140px;">Card Game</td><td style="padding: 6px 12px;">{game}</td></tr>
    <tr><td style="padding: 6px 12px; background: #f5f5f5; font-weight: bold;">Set</td><td style="padding: 6px 12px;">{set_code}</td></tr>
    <tr><td style="padding: 6px 12px; background: #f5f5f5; font-weight: bold;">Card Number</td><td style="padding: 6px 12px;">{card_number}</td></tr>{rarity_row}
    <tr><td style="padding: 6px 12px; background: #f5f5f5; font-weight: bold;">Language</td><td style="padding: 6px 12px;">{language}</td></tr>
  </table>
  <div style="padding: 12px; background: #fafafa; border: 1px solid #eee; border-radius: 4px; margin-bottom: 16px; font-size: 13px; color: #555;">
    <strong>Authenticity:</strong> 100% authentic, sourced directly from Japanese distributors.
  </div>
  <div style="padding: 12px; background: #fafafa; border: 1px solid #eee; border-radius: 4px; margin-bottom: 16px; font-size: 13px; color: #555;">
    <strong>Condition:</strong> Mint &mdash; equivalent to PSA 9&ndash;10. All cards are pulled directly from sealed product and immediately double-sleeved. Please see photos for exact condition.
  </div>
  <div style="padding: 12px; background: #fafafa; border: 1px solid #eee; border-radius: 4px; margin-bottom: 16px; font-size: 13px; color: #555;">
    <strong>Shipping:</strong> Orders are processed within 24 hours. Every card is shipped in a protective sleeve and top loader, sealed in a waterproof team bag, and sent in a bubble-wrapped mailer or cardboard-backed envelope.
  </div>
  <div style="padding: 12px; background: #fafafa; border: 1px solid #eee; border-radius: 4px; margin-bottom: 16px; font-size: 13px; color: #555;">
    <strong>Raffles &amp; Rewards:</strong> All customers are eligible for exclusive raffles and giveaways on our website. Check your order confirmation for details!
  </div>
  <div style="padding: 12px; background: #fafafa; border: 1px solid #eee; border-radius: 4px; margin-bottom: 16px; font-size: 13px; color: #555;">
    <strong>Business Buyers:</strong> VAT-registered businesses can save 20% on VAT. Please contact us for more information.
  </div>
  <div style="text-align: center; font-size: 12px; color: #999; border-top: 1px solid #eee; padding-top: 12px;">
    {store_name} &mdash; Japanese Trading Card Games
  </div>
</div>"""

RARITY_ROW_TEMPLATE = """
    <tr><td style="padding: 6px 12px; background: #f5f5f5; font-weight: bold;">Rarity</td><td style="padding: 6px 12px;">{rarity}</td></tr>"""


def generate_description(sku, title='', item_specifics=None):
    """
    Generate standardized HTML description from card metadata.

    Args:
        sku: Product SKU (e.g. "OP-OP01-062-JP")
        title: Current listing title (used to extract card name)
        item_specifics: Existing item specifics dict {name: value}

    Returns:
        HTML description string.
    """
    parsed = parse_sku(sku)
    specifics = item_specifics or {}

    # Card name: strip language tag and card number prefix from title
    card_name = _extract_card_name(title, parsed)

    # Use parsed data, fall back to existing item specifics
    game = parsed.get('game') or specifics.get('Card Game', 'Trading Card Game')
    set_code = parsed.get('set_code') or specifics.get('Set', '')
    card_number = parsed.get('card_number') or specifics.get('Card Number', '')
    language = parsed.get('language') or specifics.get('Language', 'Japanese')
    rarity = specifics.get('Rarity', '')

    rarity_row = ''
    if rarity:
        rarity_row = RARITY_ROW_TEMPLATE.format(rarity=_escape_html(rarity))

    return DESCRIPTION_TEMPLATE.format(
        card_name=_escape_html(card_name),
        game=_escape_html(game),
        set_code=_escape_html(set_code),
        card_number=_escape_html(card_number),
        rarity_row=rarity_row,
        language=_escape_html(language),
        store_name=STORE_NAME,
    )


def _extract_card_name(title, parsed):
    """Extract the card name from a listing title, removing set/number/language metadata."""
    if not title:
        return parsed.get('set_code', '') + '-' + parsed.get('card_number', '')

    name = title
    # Remove [Japanese] suffix
    import re
    name = re.sub(r'\s*\[Japanese\]\s*$', '', name, flags=re.IGNORECASE)
    name = re.sub(r'\s*\[JP\]\s*$', '', name, flags=re.IGNORECASE)
    # Remove leading card number pattern (e.g. "OP01-062 Card Name" → "Card Name")
    name = re.sub(r'^[A-Z]{2,4}-?\d{2,3}-\d{2,4}\s+', '', name)
    return name.strip() or title


def _escape_html(text):
    """Escape HTML special characters."""
    return (
        str(text)
        .replace('&', '&amp;')
        .replace('<', '&lt;')
        .replace('>', '&gt;')
        .replace('"', '&quot;')
    )
