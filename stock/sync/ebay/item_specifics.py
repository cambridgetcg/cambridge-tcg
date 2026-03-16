"""SKU parser and eBay item specifics builder.

SKU formats:
    OP-{SET}-{NUM}-JP     → One Piece (e.g. OP-OP01-062-JP, OP-EB01-012-JP, OP-ST13-001-JP)
    PKMN-{SET}-{NUM}-JP   → Pokemon (e.g. PKMN-SV6-001-JP, PKMN-S12a-044-JP)

Derives Card Game, Set, Card Number, and Language from SKU.
Rarity cannot be derived from SKU — preserved from existing item specifics.
"""

import re


# SKU pattern: PREFIX-SET-NUMBER-LANG
SKU_PATTERN = re.compile(
    r'^(OP|PKMN)-([A-Za-z0-9]+)-(\d{2,4})-([A-Z]{2})$'
)

GAME_MAP = {
    'OP': 'One Piece Card Game',
    'PKMN': 'Pokemon',
}

FRANCHISE_MAP = {
    'OP': 'One Piece',
    'PKMN': 'Pokemon',
}


def parse_sku(sku):
    """
    Parse a SKU into structured card data.

    Returns dict with keys: game, set_code, card_number, language.
    Returns partial dict with available info if SKU doesn't match pattern exactly.
    """
    if not sku:
        return {}

    match = SKU_PATTERN.match(sku.strip())
    if not match:
        # Try partial parse
        return _partial_parse(sku)

    prefix, set_code, card_number, lang_code = match.groups()

    return {
        'game': GAME_MAP.get(prefix, prefix),
        'franchise': FRANCHISE_MAP.get(prefix, prefix),
        'set_code': set_code,
        'card_number': card_number,
        'language': 'Japanese' if lang_code == 'JP' else lang_code,
    }


def _partial_parse(sku):
    """Best-effort parse for non-standard SKUs."""
    result = {}

    if sku.startswith('OP-'):
        result['game'] = 'One Piece Card Game'
        result['franchise'] = 'One Piece'
    elif sku.startswith('PKMN-'):
        result['game'] = 'Pokemon'
        result['franchise'] = 'Pokemon'

    if sku.endswith('-JP'):
        result['language'] = 'Japanese'

    return result


def build_item_specifics(sku, existing=None):
    """
    Build item specifics dict from SKU, merging with existing specifics.

    Parsed SKU data takes precedence for Card Game, Set, Card Number, Language.
    Existing data is preserved for fields not derivable from SKU (e.g. Rarity).

    Args:
        sku: Product SKU string
        existing: Current item specifics {name: value} from eBay listing

    Returns:
        {name: value} dict of item specifics to set.
        Only includes fields that differ from existing (to avoid unnecessary updates).
    """
    existing = existing or {}
    parsed = parse_sku(sku)

    if not parsed:
        return {}

    # Build target specifics
    target = {}

    if 'game' in parsed:
        target['Card Game'] = parsed['game']
    if 'franchise' in parsed:
        target['Franchise'] = parsed['franchise']
    if 'set_code' in parsed:
        target['Set'] = parsed['set_code']
    if 'card_number' in parsed:
        target['Card Number'] = parsed['card_number']
    if 'language' in parsed:
        target['Language'] = parsed['language']

    # Preserve existing fields not derivable from SKU
    for key in ('Rarity', 'Character', 'Type', 'Color'):
        if key in existing and key not in target:
            target[key] = existing[key]

    # Only return fields that differ from existing
    changed = {}
    for name, value in target.items():
        if existing.get(name) != value:
            changed[name] = value

    return changed
