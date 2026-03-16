"""Emergency: Zero out all stock on eBay + Shopify.

Use when prices are wrong and you need to stop selling immediately.
Does NOT modify local stock_data.json — only pushes 0 to platforms.

Usage:
    python -m stock.count.emergency_zero --dry-run   # preview
    python -m stock.count.emergency_zero              # execute
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from stock.sync.ebay.client import EbayClient
from stock.sync.shopify.client import ShopifyClient


def zero_ebay(dry_run):
    print("\n" + "=" * 60)
    print("EBAY — Setting all listings to quantity 0")
    print("=" * 60)

    client = EbayClient()
    listings = client.get_active_listings()
    print(f"Fetched {len(listings)} active listings")

    to_zero = []
    already_zero = 0
    for listing in listings:
        item_id = listing['item_id']
        quantity_sold = listing.get('quantity_sold', 0)
        current_total = listing.get('quantity', 0)
        current_available = current_total - quantity_sold
        sku = listing.get('sku', '(no sku)')

        if current_available == 0:
            already_zero += 1
            continue

        to_zero.append({
            'item_id': item_id,
            'sku': sku,
            'current_available': current_available,
        })

    print(f"  {len(to_zero)} listings to zero out, {already_zero} already at 0")

    if not to_zero:
        print("  Nothing to do.")
        return 0, 0

    if dry_run:
        for item in to_zero[:10]:
            print(f"  [DRY RUN] {item['sku']}: {item['current_available']} -> 0")
        if len(to_zero) > 10:
            print(f"  ... and {len(to_zero) - 10} more")
        return len(to_zero), 0

    success = 0
    failed = 0
    for item in to_zero:
        result = client.revise_item(item['item_id'], quantity=0)
        if result['ack'] in ('Success', 'Warning'):
            success += 1
        else:
            failed += 1
            print(f"  FAILED {item['sku']}: {result['errors']}")

    print(f"  Done: {success} zeroed, {failed} failed")
    return success, failed


def zero_shopify(dry_run):
    print("\n" + "=" * 60)
    print("SHOPIFY — Setting all inventory to 0")
    print("=" * 60)

    client = ShopifyClient()
    variants = client.get_all_variants()
    print(f"Fetched {len(variants)} variants")

    location_ids = set(v['location_id'] for v in variants if v.get('location_id'))
    if not location_ids:
        print("  Error: No inventory locations found.")
        return 0, 0
    location_id = next(v['location_id'] for v in variants if v.get('location_id'))
    print(f"  Location: {location_id}")

    to_zero = []
    already_zero = 0
    for variant in variants:
        current = variant.get('available', 0)
        sku = variant.get('sku', '(no sku)')
        inv_item_id = variant.get('inventory_item_id')

        if current == 0 or not inv_item_id:
            already_zero += 1
            continue

        to_zero.append({
            'inventory_item_id': inv_item_id,
            'quantity': 0,
            'sku': sku,
            'current': current,
        })

    print(f"  {len(to_zero)} variants to zero out, {already_zero} already at 0")

    if not to_zero:
        print("  Nothing to do.")
        return 0, 0

    if dry_run:
        for item in to_zero[:10]:
            print(f"  [DRY RUN] {item['sku']}: {item['current']} -> 0")
        if len(to_zero) > 10:
            print(f"  ... and {len(to_zero) - 10} more")
        return len(to_zero), 0

    batch_items = [
        {'inventory_item_id': item['inventory_item_id'], 'quantity': 0}
        for item in to_zero
    ]
    print(f"  Pushing {len(batch_items)} zero-quantity updates...")
    results = client.set_inventory_quantities(location_id, batch_items)

    total_success = sum(r['count'] for r in results if r['success'])
    total_failed = sum(r['count'] for r in results if not r['success'])
    for r in results:
        if not r['success']:
            print(f"  Batch {r['batch']} FAILED: {'; '.join(r['errors'])}")

    print(f"  Done: {total_success} zeroed, {total_failed} failed")
    return total_success, total_failed


def main():
    parser = argparse.ArgumentParser(
        description='EMERGENCY: Zero out all stock on eBay + Shopify'
    )
    parser.add_argument('--dry-run', action='store_true', help='Preview without updating')
    parser.add_argument('--ebay-only', action='store_true', help='Only zero eBay')
    parser.add_argument('--shopify-only', action='store_true', help='Only zero Shopify')
    args = parser.parse_args()

    print("!" * 60)
    print("EMERGENCY STOCK ZERO-OUT")
    print("This will set all platform stock to 0.")
    print("Local stock_data.json is NOT modified.")
    print("!" * 60)

    if args.dry_run:
        print("\n*** DRY RUN MODE — no changes will be made ***\n")

    do_ebay = not args.shopify_only
    do_shopify = not args.ebay_only

    ebay_ok, ebay_fail = 0, 0
    shopify_ok, shopify_fail = 0, 0

    if do_ebay:
        ebay_ok, ebay_fail = zero_ebay(args.dry_run)

    if do_shopify:
        shopify_ok, shopify_fail = zero_shopify(args.dry_run)

    print("\n" + "=" * 60)
    print("SUMMARY")
    if do_ebay:
        print(f"  eBay:    {ebay_ok} zeroed, {ebay_fail} failed")
    if do_shopify:
        print(f"  Shopify: {shopify_ok} zeroed, {shopify_fail} failed")
    print("=" * 60)

    if args.dry_run:
        print("\nTo execute: python -m stock.count.emergency_zero")


if __name__ == '__main__':
    main()
