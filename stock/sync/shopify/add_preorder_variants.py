"""Add Pre-Order variants to all card products on Shopify.

For each card product (SKU starts with OP- or PKMN-) that doesn't already
have an "Availability" option, this script:

1. Adds an "Availability" product option with values "In Stock" / "Pre-Order"
   (existing variant auto-maps to "In Stock" via LEAVE_AS_IS strategy)
2. Creates a "Pre-Order" variant with:
   - Same price as the In Stock variant
   - SKU: {original_sku}-PO
   - inventoryPolicy: CONTINUE (sells past 0)
   - Metafield custom.order_limit_qty = "4" for downstream enforcement

Usage:
    python -m stock.sync.shopify.add_preorder_variants [--dry-run] [--sku SKU]

Options:
    --dry-run   Preview which products would be updated (no mutations)
    --sku SKU   Only process the product containing this SKU
"""

import argparse
import logging
import sys

from stock.sync.shopify.client import ShopifyClient

logger = logging.getLogger(__name__)

OPTION_NAME = 'Availability'
IN_STOCK_VALUE = 'In Stock'
PREORDER_VALUE = 'Pre-Order'
PREORDER_SKU_SUFFIX = '-PO'
ORDER_LIMIT = '4'


def _is_card_sku(sku):
    """True if SKU belongs to a card product."""
    return sku.startswith('OP-') or sku.startswith('PKMN-')


def _has_availability_option(options):
    """True if product already has an Availability option."""
    return any(o.get('name') == OPTION_NAME for o in options)


def _has_preorder_variant(variants):
    """True if any variant SKU ends with the pre-order suffix."""
    return any(v['sku'].endswith(PREORDER_SKU_SUFFIX) for v in variants)


def _group_by_product(variants):
    """Group variants by product_id. Returns {product_id: [variant, ...]}."""
    by_product = {}
    for v in variants:
        by_product.setdefault(v['product_id'], []).append(v)
    return by_product


def run(dry_run=False, sku_filter=None, prefix=None):
    client = ShopifyClient()

    print('Fetching all variants...')
    all_variants = client.get_all_variants()
    print(f'  {len(all_variants)} variants fetched')

    # Group by product and filter
    by_product = _group_by_product(all_variants)

    eligible = []
    skipped_non_card = 0
    skipped_complete = 0

    for product_id, variants in by_product.items():
        # Check if any variant is a card SKU
        card_variants = [v for v in variants if _is_card_sku(v['sku'])]
        if not card_variants:
            skipped_non_card += 1
            continue

        # If sku_filter given, only process matching product
        if sku_filter:
            if not any(v['sku'] == sku_filter for v in card_variants):
                continue

        # If prefix given, only process SKUs starting with it
        if prefix:
            if not any(v['sku'].startswith(prefix) for v in card_variants):
                continue

        # Skip if already fully set up (has option AND pre-order variant)
        options = card_variants[0].get('options', [])
        has_option = _has_availability_option(options)
        has_po = _has_preorder_variant(variants)
        if has_option and has_po:
            skipped_complete += 1
            continue

        # Use the first non-PO variant as the "source" for price
        source = next((v for v in card_variants if not v['sku'].endswith(PREORDER_SKU_SUFFIX)), card_variants[0])
        eligible.append({
            'product_id': product_id,
            'title': source['title'],
            'sku': source['sku'],
            'price': source['price'],
            'needs_option': not has_option,
            'needs_variant': not has_po,
        })

    print(f'\nEligible: {len(eligible)} products')
    print(f'Skipped (non-card): {skipped_non_card}')
    print(f'Skipped (already complete): {skipped_complete}')

    if not eligible:
        print('Nothing to do.')
        return

    if dry_run:
        print('\n--- DRY RUN (no changes) ---')
        for p in eligible:
            print(f"  {p['sku']:30s}  {p['title'][:50]:50s}  £{p['price']:.2f}")
        print(f'\nWould update {len(eligible)} products.')
        return

    # Process
    updated = 0
    errors = 0

    for i, p in enumerate(eligible, 1):
        product_id = p['product_id']
        sku = p['sku']
        price = p['price']
        label = f"[{i}/{len(eligible)}] {sku}"

        # Step 1: Add Availability option (if needed)
        if p['needs_option']:
            options_result = client.create_product_options(
                product_id,
                options=[{
                    'name': OPTION_NAME,
                    'values': [{'name': IN_STOCK_VALUE}, {'name': PREORDER_VALUE}],
                }],
                variant_strategy='LEAVE_AS_IS',
            )
            if not options_result['success']:
                print(f"  {label} ERROR (options): {options_result['errors']}")
                errors += 1
                continue

        # Step 2: Create Pre-Order variant (if needed)
        if p['needs_variant']:
            preorder_sku = sku + PREORDER_SKU_SUFFIX
            variant_result = client.create_variants_bulk(
                product_id,
                variants=[{
                    'price': str(price),
                    'inventoryItem': {'sku': preorder_sku, 'tracked': True},
                    'inventoryPolicy': 'CONTINUE',
                    'optionValues': [
                        {'optionName': OPTION_NAME, 'name': PREORDER_VALUE},
                    ],
                    'metafields': [{
                        'namespace': 'custom',
                        'key': 'order_limit_qty',
                        'value': ORDER_LIMIT,
                        'type': 'number_integer',
                    }],
                }],
            )
            if not variant_result['success']:
                print(f"  {label} ERROR (variant): {variant_result['errors']}")
                errors += 1
                continue

        updated += 1
        preorder_sku = sku + PREORDER_SKU_SUFFIX
        print(f"  {label} OK  →  {preorder_sku}")

    print(f'\nDone: {updated} updated, {errors} errors')


def main():
    parser = argparse.ArgumentParser(
        description='Add Pre-Order variants to Shopify card products',
    )
    parser.add_argument('--dry-run', action='store_true',
                        help='Preview changes without mutating')
    parser.add_argument('--sku', type=str, default=None,
                        help='Only process the product with this SKU')
    parser.add_argument('--prefix', type=str, default=None,
                        help='Only process products whose SKU starts with this prefix (e.g. OP-OP01)')
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format='%(levelname)s %(name)s: %(message)s',
    )

    try:
        run(dry_run=args.dry_run, sku_filter=args.sku, prefix=args.prefix)
    except KeyboardInterrupt:
        print('\nAborted.')
        sys.exit(1)
    except Exception as e:
        logger.error(f'Fatal: {e}', exc_info=True)
        sys.exit(1)


if __name__ == '__main__':
    main()
