"""Title normalization for eBay listings.

Standardizes listing titles for consistency across ~700 active listings.
SKU format: OP-{SET}-{NUM}-JP or PKMN-{SET}-{NUM}-JP.

Rules:
- Trim/collapse whitespace
- Consistent language tag: [Japanese] suffix
- Consistent set code formatting (hyphenated, e.g. "OP-01")
- Consistent capitalization for rarity keywords
- Remove duplicate card numbers
- Keep within eBay 80-char title limit
"""

import re

# Rarity keywords — canonical capitalization
RARITY_MAP = {
    'super rare': 'Super Rare',
    'sr': 'SR',
    'secret rare': 'Secret Rare',
    'sec': 'SEC',
    'leader': 'Leader',
    'l': 'L',
    'rare': 'Rare',
    'r': 'R',
    'common': 'Common',
    'c': 'C',
    'uncommon': 'Uncommon',
    'uc': 'UC',
    'promo': 'Promo',
    'p': 'P',
    'special art': 'Special Art',
    'manga rare': 'Manga Rare',
    'alternate art': 'Alternate Art',
    'alt art': 'Alt Art',
    'parallel': 'Parallel',
    'comic rare': 'Comic Rare',
    'treasure rare': 'Treasure Rare',
    'illustration rare': 'Illustration Rare',
    'special illustration rare': 'Special Illustration Rare',
    'hyper rare': 'Hyper Rare',
    'ultra rare': 'Ultra Rare',
    'double rare': 'Double Rare',
    'art rare': 'Art Rare',
    'sar': 'SAR',
    'sir': 'SIR',
    'ur': 'UR',
    'hr': 'HR',
    'ar': 'AR',
    'rr': 'RR',
    'tr': 'TR',
}

# Language tags to normalize
LANGUAGE_TAGS = re.compile(
    r'\s*\[?\s*(JP|Japanese|japan|jpn|日本語)\s*\]?\s*$',
    re.IGNORECASE,
)

# Existing condition strings to strip before re-adding canonical form
CONDITION_TAGS = re.compile(
    r'\s*[-–]\s*(Mint|Near Mint|NM|NM/M|M)\s*$',
    re.IGNORECASE,
)

EBAY_TITLE_MAX = 80


def normalize_title(title, sku=''):
    """
    Standardize an eBay listing title.

    Returns normalized title, or the original if no changes needed.
    """
    if not title:
        return title

    original = title
    result = title

    # 1. Trim and collapse whitespace
    result = ' '.join(result.split())

    # 2. Remove existing language tag and condition tag (we'll re-add them)
    result = LANGUAGE_TAGS.sub('', result).strip()
    result = CONDITION_TAGS.sub('', result).strip()

    # 3. Fix rarity capitalization (case-insensitive word replacement)
    #    Only replace whole words / known abbreviations
    for lower_form, canonical in RARITY_MAP.items():
        pattern = re.compile(r'\b' + re.escape(lower_form) + r'\b', re.IGNORECASE)
        result = pattern.sub(canonical, result)

    # 4. Strip erroneous hyphens in set codes (revert OP-01 → OP01, EB-01 → EB01)
    result = re.sub(
        r'\b(OP|EB|ST|PRB?)-(\d{2,3})\b',
        lambda m: f"{m.group(1)}{m.group(2)}",
        result,
    )

    # 5. Remove duplicate card number patterns
    #    e.g. "OP01-062 OP01-062" → keep first occurrence only
    card_num_pattern = re.compile(r'((?:OP|EB|ST|SV|PKMN|PRB?)-?\d{2,3}-\d{2,4})')
    found_numbers = card_num_pattern.findall(result)
    if len(found_numbers) > 1:
        # Normalize found numbers for comparison
        seen = set()
        for num in found_numbers:
            normalized_num = num.replace('-', '')
            if normalized_num in seen:
                # Remove the duplicate occurrence
                result = result.replace(num, '', 1).strip()
                result = ' '.join(result.split())  # clean up spacing
            seen.add(normalized_num)

    # 6. Add condition + [Japanese] suffix if SKU ends with -JP
    if sku.endswith('-JP') or sku.endswith('-jp'):
        result = f"{result} - Mint [Japanese]"

    # 7. Enforce 80-char limit
    if len(result) > EBAY_TITLE_MAX:
        result = result[:EBAY_TITLE_MAX - 1].rstrip() + '…'

    # 8. Final cleanup
    result = ' '.join(result.split())

    # Only return changed title
    if result == original:
        return original

    return result
