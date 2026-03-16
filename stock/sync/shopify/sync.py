"""Core sync orchestration for Shopify listing metadata.

Fetches variants from Shopify, normalizes titles/descriptions/tags/metafields,
and pushes updates back. Shared between CLI and Lambda entry points.

Reuses the same normalizer and description generator as eBay sync
(stock.sync.ebay.normalizer, stock.sync.ebay.description) since the
listing content should be consistent across platforms.

Metafield sync: card_number_, rarity, condition_ derived from SKU + existing data.
"""

import logging

from stock.sync.ebay.normalizer import normalize_title, RARITY_MAP
from stock.sync.ebay.description import generate_description
from stock.sync.ebay.item_specifics import parse_sku

logger = logging.getLogger(__name__)

# Metafield definitions — namespace.key → type
METAFIELD_DEFS = {
    'custom.card_number_': 'single_line_text_field',
    'custom.rarity': 'single_line_text_field',
    'custom.condition_': 'single_line_text_field',
}

# Canonical condition for all cards
CONDITION_VALUE = 'Mint'


def sync_listings(client, dry_run=False, skus=None, sku_prefix=None,
                  title=True, description=True, tags=True, metafields=True):
    """
    Fetch variants from Shopify, normalize metadata, push updates.

    Args:
        client: ShopifyClient instance
        dry_run: Preview changes without pushing
        skus: Optional set/list of SKUs to filter (None = all)
        sku_prefix: Optional SKU prefix to filter, e.g. "OP01"
        title: Whether to sync titles
        description: Whether to sync descriptions
        tags: Whether to sync tags
        metafields: Whether to sync metafields (card_number_, rarity, condition_)

    Returns:
        {total, checked, updated, skipped, errors, changes: [{sku, product_id, field, old, new}]}
    """
    logger.info("Fetching all variants from Shopify...")
    variants = client.get_all_variants()
    logger.info(f"Fetched {len(variants)} variants")

    # Filter by SKU if specified
    if skus:
        sku_set = set(skus)
        variants = [v for v in variants if v['sku'] in sku_set]
        logger.info(f"Filtered to {len(variants)} variants matching requested SKUs")

    if sku_prefix:
        variants = [v for v in variants if v.get('sku', '').startswith(sku_prefix)]
        logger.info(f"Filtered to {len(variants)} variants matching prefix '{sku_prefix}'")

    # Skip variants without SKU or non-card SKUs
    variants_with_sku = [v for v in variants if _is_card_sku(v.get('sku', ''))]
    skipped_no_sku = len(variants) - len(variants_with_sku)
    if skipped_no_sku:
        logger.warning(f"Skipped {skipped_no_sku} variants with no card SKU")

    # Deduplicate by product_id (multiple variants can share same product)
    # We sync at product level, so group by product_id
    products = {}
    for v in variants_with_sku:
        pid = v['product_id']
        if pid not in products:
            products[pid] = v

    # Compute changes for each product
    changes = []
    for variant in products.values():
        item_changes = _compute_changes(variant, title, description, tags, metafields)
        changes.extend(item_changes)

    products_to_update = {}
    for change in changes:
        pid = change['product_id']
        if pid not in products_to_update:
            products_to_update[pid] = {'product_id': pid, 'sku': change['sku']}
        entry = products_to_update[pid]

        if change['field'] == 'title':
            entry['title'] = change['new']
        elif change['field'] == 'description':
            entry['description_html'] = change['new']
        elif change['field'] == 'tags':
            entry['tags'] = change['new']
        elif change['field'] == 'metafields':
            entry.setdefault('metafields', []).extend(change['new'])

    result = {
        'total': len(variants),
        'checked': len(products),
        'updated': 0,
        'skipped': len(products) - len(products_to_update),
        'errors': 0,
        'changes': changes,
        'error_details': [],
    }

    if dry_run:
        logger.info(f"[DRY RUN] {len(changes)} changes across {len(products_to_update)} products")
        result['updated'] = len(products_to_update)
        return result

    if not products_to_update:
        logger.info("No changes needed — all listings are up to date")
        return result

    # Push updates
    logger.info(f"Pushing updates to {len(products_to_update)} products...")
    for entry in products_to_update.values():
        try:
            resp = client.update_product(
                product_id=entry['product_id'],
                title=entry.get('title'),
                description_html=entry.get('description_html'),
                tags=entry.get('tags'),
                metafields=entry.get('metafields'),
            )
            if resp['success']:
                result['updated'] += 1
            else:
                result['errors'] += 1
                result['error_details'].append({
                    'product_id': entry['product_id'],
                    'errors': resp['errors'],
                })
        except Exception as e:
            result['errors'] += 1
            result['error_details'].append({
                'product_id': entry['product_id'],
                'errors': [str(e)],
            })

    logger.info(
        f"Done: {result['updated']} updated, {result['errors']} errors, "
        f"{result['skipped']} unchanged"
    )
    return result


def _is_card_sku(sku):
    """Check if a SKU looks like a card product (OP-* or PKMN-*)."""
    return sku.startswith('OP-') or sku.startswith('PKMN-')


def _compute_changes(variant, do_title, do_description, do_tags, do_metafields):
    """Compute changes needed for a single product (via its variant)."""
    changes = []
    sku = variant['sku']
    product_id = variant['product_id']
    parsed = parse_sku(sku)

    if do_title:
        current_title = variant.get('title', '')
        new_title = normalize_title(current_title, sku)
        if new_title != current_title:
            changes.append({
                'sku': sku,
                'product_id': product_id,
                'field': 'title',
                'old': current_title,
                'new': new_title,
            })

    if do_description:
        current_desc = variant.get('description_html', '')
        new_desc = generate_description(sku, variant.get('title', ''))
        if new_desc.strip() != current_desc.strip():
            changes.append({
                'sku': sku,
                'product_id': product_id,
                'field': 'description',
                'old': current_desc[:100] + '...' if len(current_desc) > 100 else current_desc,
                'new': new_desc,
            })

    if do_tags:
        current_tags = variant.get('tags', [])
        new_tags = _build_tags(sku, parsed, current_tags)
        if new_tags and sorted(new_tags) != sorted(current_tags):
            changes.append({
                'sku': sku,
                'product_id': product_id,
                'field': 'tags',
                'old': current_tags,
                'new': new_tags,
            })

    if do_metafields:
        current_mfs = variant.get('metafields', {})
        mf_changes = _compute_metafield_changes(sku, parsed, current_mfs)
        if mf_changes:
            changes.append({
                'sku': sku,
                'product_id': product_id,
                'field': 'metafields',
                'old': {f"{m['namespace']}.{m['key']}": current_mfs.get(f"{m['namespace']}.{m['key']}", '(not set)') for m in mf_changes},
                'new': mf_changes,
            })

    return changes


def _compute_metafield_changes(sku, parsed, current_metafields):
    """Compute metafield updates for card_number_, rarity, condition_.

    Args:
        sku: Product SKU
        parsed: Output of parse_sku()
        current_metafields: {'namespace.key': value} from Shopify

    Returns:
        List of metafield input dicts for productUpdate mutation, or empty list.
    """
    updates = []

    # card_number_: derive from SKU (strip prefix and language suffix)
    # OP-OP01-062-JP → OP01-062, PKMN-SV1A-074-JP → SV1A-074
    card_number = _derive_card_number(sku)
    if card_number:
        current = current_metafields.get('custom.card_number_', '')
        if current != card_number:
            updates.append({
                'namespace': 'custom',
                'key': 'card_number_',
                'value': card_number,
                'type': 'single_line_text_field',
            })

    # rarity: preserve existing but normalize capitalization
    current_rarity = current_metafields.get('custom.rarity', '')
    if current_rarity:
        canonical = _normalize_rarity(current_rarity)
        if canonical != current_rarity:
            updates.append({
                'namespace': 'custom',
                'key': 'rarity',
                'value': canonical,
                'type': 'single_line_text_field',
            })

    # condition_: always "Mint"
    current_condition = current_metafields.get('custom.condition_', '')
    if current_condition != CONDITION_VALUE:
        updates.append({
            'namespace': 'custom',
            'key': 'condition_',
            'value': CONDITION_VALUE,
            'type': 'single_line_text_field',
        })

    return updates


def _derive_card_number(sku):
    """Extract card number from SKU.

    OP-OP01-062-JP → OP01-062
    OP-OP11-SP-OP07-085-JP → OP11-SP-OP07-085
    PKMN-SV1A-074-JP → SV1A-074
    """
    if not sku:
        return ''

    # Strip game prefix (OP- or PKMN-) and language suffix (-JP)
    for prefix in ('OP-', 'PKMN-'):
        if sku.startswith(prefix):
            rest = sku[len(prefix):]
            if rest.endswith('-JP'):
                rest = rest[:-3]
            return rest
    return ''


def _normalize_rarity(rarity):
    """Normalize rarity capitalization using the canonical RARITY_MAP."""
    lower = rarity.strip().lower()
    if lower in RARITY_MAP:
        return RARITY_MAP[lower]
    # Title-case fallback for unknown rarities
    return rarity.strip().title() if rarity.strip().islower() else rarity.strip()


def _build_tags(sku, parsed, existing_tags):
    """Build canonical tag list from SKU metadata. Returns None if no changes needed."""
    desired = set(existing_tags)

    # Add game tag
    game = parsed.get('game', '')
    if game:
        desired.add(game)

    # Add set code tag
    set_code = parsed.get('set_code', '')
    if set_code:
        desired.add(set_code)

    # Add language tag
    language = parsed.get('language', '')
    if language:
        desired.add(language)

    if desired == set(existing_tags):
        return None

    return sorted(desired)
