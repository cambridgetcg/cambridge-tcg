"""CLI entry point for eBay listing metadata sync.

Usage:
    # Preview all changes (dry run)
    python -m stock.sync.ebay --dry-run

    # Sync only titles
    python -m stock.sync.ebay --title-only

    # Sync only descriptions
    python -m stock.sync.ebay --description-only

    # Sync only item specifics
    python -m stock.sync.ebay --specifics-only

    # Sync specific SKUs
    python -m stock.sync.ebay --sku OP-OP01-062-JP --sku OP-EB01-012-JP

    # Sync everything (titles + descriptions + specifics)
    python -m stock.sync.ebay
"""

import argparse
import json
import logging
import sys

from stock.sync.ebay.client import EbayClient
from stock.sync.ebay.sync import sync_listings


def main():
    parser = argparse.ArgumentParser(
        prog='stock.sync.ebay',
        description='Sync eBay listing metadata (titles, descriptions, item specifics)',
    )
    parser.add_argument('--dry-run', action='store_true',
                        help='Preview changes without pushing to eBay')
    parser.add_argument('--sku', action='append', metavar='SKU',
                        help='Sync only these SKUs (can be repeated)')
    parser.add_argument('--title-only', action='store_true',
                        help='Sync only titles')
    parser.add_argument('--description-only', action='store_true',
                        help='Sync only descriptions')
    parser.add_argument('--specifics-only', action='store_true',
                        help='Sync only item specifics')
    parser.add_argument('--workers', type=int, default=5,
                        help='Number of concurrent API workers (default: 5)')
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
        do_title, do_description, do_specifics = True, False, False
    elif args.description_only:
        do_title, do_description, do_specifics = False, True, False
    elif args.specifics_only:
        do_title, do_description, do_specifics = False, False, True
    else:
        do_title, do_description, do_specifics = True, True, True

    try:
        client = EbayClient()
    except Exception as e:
        print(f"Error: Failed to initialize eBay client: {e}", file=sys.stderr)
        print("Ensure AWS credentials are configured (for Secrets Manager access).", file=sys.stderr)
        sys.exit(1)

    result = sync_listings(
        client=client,
        dry_run=args.dry_run,
        skus=args.sku,
        title=do_title,
        description=do_description,
        specifics=do_specifics,
        workers=args.workers,
    )

    if args.json_output:
        print(json.dumps(result, indent=2, default=str))
        return

    # Pretty-print results
    print(f"\n{'=' * 60}")
    mode = '[DRY RUN] ' if args.dry_run else ''
    print(f"{mode}eBay Listing Metadata Sync")
    print(f"{'=' * 60}")
    print(f"Total listings:  {result['total']}")
    print(f"Checked (w/SKU): {result['checked']}")
    print(f"Updated:         {result['updated']}")
    print(f"Unchanged:       {result['skipped']}")
    print(f"Errors:          {result['errors']}")

    if result['changes']:
        print(f"\n{'─' * 60}")
        print(f"{'Changes':^60}")
        print(f"{'─' * 60}")

        for change in result['changes']:
            print(f"\n  SKU: {change['sku']}  (Item: {change['item_id']})")
            print(f"  Field: {change['field']}")
            if change['field'] == 'item_specifics':
                for k, v in change['new'].items():
                    old_v = change['old'].get(k, '(not set)')
                    print(f"    {k}: {old_v} → {v}")
            else:
                print(f"    Old: {change['old'][:80] if isinstance(change['old'], str) else change['old']}")
                print(f"    New: {change['new'][:80] if isinstance(change['new'], str) else change['new']}")

    if result['error_details']:
        print(f"\n{'─' * 60}")
        print(f"{'Errors':^60}")
        print(f"{'─' * 60}")
        for err in result['error_details'][:20]:
            print(f"  Item {err['item_id']}: {'; '.join(err['errors'])}")

    print()


if __name__ == '__main__':
    main()
