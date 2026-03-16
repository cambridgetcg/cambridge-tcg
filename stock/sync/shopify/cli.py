"""CLI entry point for Shopify listing metadata sync.

Usage:
    # Preview all changes (dry run)
    python -m stock.sync.shopify --dry-run

    # Sync only titles
    python -m stock.sync.shopify --title-only

    # Sync only descriptions
    python -m stock.sync.shopify --description-only

    # Sync only tags
    python -m stock.sync.shopify --tags-only

    # Sync specific SKUs
    python -m stock.sync.shopify --sku OP-OP01-062-JP --sku OP-EB01-012-JP

    # Sync everything (titles + descriptions + tags)
    python -m stock.sync.shopify
"""

import argparse
import json
import logging
import sys

from stock.sync.shopify.client import ShopifyClient
from stock.sync.shopify.sync import sync_listings
from stock.sync.shopify.add_preorder_variants import run as ensure_preorder_variants


def main():
    parser = argparse.ArgumentParser(
        prog='stock.sync.shopify',
        description='Sync Shopify listing metadata (titles, descriptions, tags)',
    )
    parser.add_argument('--dry-run', action='store_true',
                        help='Preview changes without pushing to Shopify')
    parser.add_argument('--sku', action='append', metavar='SKU',
                        help='Sync only these SKUs (can be repeated)')
    parser.add_argument('--sku-prefix', metavar='PREFIX',
                        help='Filter by SKU prefix, e.g. "OP01"')
    parser.add_argument('--title-only', action='store_true',
                        help='Sync only titles')
    parser.add_argument('--description-only', action='store_true',
                        help='Sync only descriptions')
    parser.add_argument('--tags-only', action='store_true',
                        help='Sync only tags')
    parser.add_argument('--metafields-only', action='store_true',
                        help='Sync only metafields (card_number_, rarity, condition_)')
    parser.add_argument('--no-preorder', action='store_true',
                        help='Skip auto-adding Pre-Order variants to new products')
    parser.add_argument('--verbose', '-v', action='store_true',
                        help='Enable verbose logging')
    parser.add_argument('--json', action='store_true', dest='json_output',
                        help='Output results as JSON')
    args = parser.parse_args()

    # Setup logging
    level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format='%(asctime)s %(levelname)s %(message)s',
        datefmt='%H:%M:%S',
    )

    # Determine which fields to sync
    if args.title_only:
        do_title, do_description, do_tags, do_metafields = True, False, False, False
    elif args.description_only:
        do_title, do_description, do_tags, do_metafields = False, True, False, False
    elif args.tags_only:
        do_title, do_description, do_tags, do_metafields = False, False, True, False
    elif args.metafields_only:
        do_title, do_description, do_tags, do_metafields = False, False, False, True
    else:
        do_title, do_description, do_tags, do_metafields = True, True, True, True

    try:
        client = ShopifyClient()
    except Exception as e:
        print(f"Error: Failed to initialize Shopify client: {e}", file=sys.stderr)
        print("Ensure SHOPIFY_STORE, SHOPIFY_CLIENT_ID, SHOPIFY_CLIENT_SECRET env vars are set.", file=sys.stderr)
        sys.exit(1)

    result = sync_listings(
        client=client,
        dry_run=args.dry_run,
        skus=args.sku,
        sku_prefix=args.sku_prefix,
        title=do_title,
        description=do_description,
        tags=do_tags,
        metafields=do_metafields,
    )

    if args.json_output:
        print(json.dumps(result, indent=2, default=str))
        return

    # Pretty-print results
    print(f"\n{'=' * 60}")
    mode = '[DRY RUN] ' if args.dry_run else ''
    print(f"{mode}Shopify Listing Metadata Sync")
    print(f"{'=' * 60}")
    print(f"Total variants:    {result['total']}")
    print(f"Products checked:  {result['checked']}")
    print(f"Updated:           {result['updated']}")
    print(f"Unchanged:         {result['skipped']}")
    print(f"Errors:            {result['errors']}")

    if result['changes']:
        print(f"\n{'\u2500' * 60}")
        print(f"{'Changes':^60}")
        print(f"{'\u2500' * 60}")

        for change in result['changes']:
            print(f"\n  SKU: {change['sku']}  (Product: {change['product_id']})")
            print(f"  Field: {change['field']}")
            if change['field'] == 'tags':
                print(f"    Old: {', '.join(change['old']) if change['old'] else '(none)'}")
                print(f"    New: {', '.join(change['new']) if change['new'] else '(none)'}")
            elif change['field'] == 'metafields':
                for mf in change['new']:
                    key = f"{mf['namespace']}.{mf['key']}"
                    old_val = change['old'].get(key, '(not set)')
                    print(f"    {key}: {old_val} \u2192 {mf['value']}")
            else:
                old_val = change['old']
                new_val = change['new']
                if isinstance(old_val, str) and len(old_val) > 80:
                    old_val = old_val[:80] + '...'
                if isinstance(new_val, str) and len(new_val) > 80:
                    new_val = new_val[:80] + '...'
                print(f"    Old: {old_val}")
                print(f"    New: {new_val}")

    if result.get('error_details'):
        print(f"\n{'\u2500' * 60}")
        print(f"{'Errors':^60}")
        print(f"{'\u2500' * 60}")
        for err in result['error_details'][:20]:
            print(f"  Product {err['product_id']}: {'; '.join(err['errors'])}")

    print()

    # Ensure all card products have Pre-Order variants (default step)
    if not args.no_preorder:
        print(f"{'=' * 60}")
        print("Ensuring Pre-Order variants on all card products...")
        print(f"{'=' * 60}")
        ensure_preorder_variants(dry_run=args.dry_run)


if __name__ == '__main__':
    main()
