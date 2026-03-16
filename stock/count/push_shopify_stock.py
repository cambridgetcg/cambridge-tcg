"""Push listed stock quantities to Shopify.

Gets stock data (with tier-capped listed_qty) from the admin Lambda,
fetches all Shopify variants with inventory, and batch-updates
quantities via inventorySetQuantities GraphQL mutation.

Shopify inventory: quantity = absolute available (no sold-offset math like eBay).
Batch mutation handles ~100 items per call (vs eBay's 1 per call).

Usage:
    python -m stock.count.push_shopify_stock [--dry-run]
"""

import argparse
import json
import os
import sys

# Add repo root to path for cross-package imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from stock.sync.shopify.client import ShopifyClient


LAMBDA_FUNCTION = 'stock-inventory-api'


def _get_stock_from_lambda():
    """Get inventory data by invoking the admin Lambda.

    Returns dict of {sku: {quantity, listed_qty, selling_price_gbp, ...}}.
    """
    import boto3
    client = boto3.client('lambda', region_name='us-east-1')
    response = client.invoke(
        FunctionName=LAMBDA_FUNCTION,
        Payload=json.dumps({
            'requestContext': {'http': {'method': 'GET', 'path': '/inventory'}},
            'headers': {},
        }),
    )
    result = json.loads(response['Payload'].read())
    body = json.loads(result['body'])
    return {item['sku']: item for item in body['items']}


def main():
    parser = argparse.ArgumentParser(description='Push listed stock quantities to Shopify')
    parser.add_argument('--dry-run', action='store_true', help='Preview without updating')
    args = parser.parse_args()

    # Get stock data from admin Lambda (reads stock_inventory + computes listed_qty)
    print("Loading inventory from admin Lambda...")
    stock_data = _get_stock_from_lambda()
    print(f"Loaded {len(stock_data)} SKUs")

    # Fetch all Shopify variants
    print("\nConnecting to Shopify...")
    client = ShopifyClient()
    variants = client.get_all_variants()
    print(f"Fetched {len(variants)} variants")

    # Detect location — all variants should share the same location
    location_ids = set(v['location_id'] for v in variants if v['location_id'])
    if not location_ids:
        print("Error: No inventory locations found in variant data.")
        sys.exit(1)
    if len(location_ids) > 1:
        print(f"Warning: Multiple locations found: {location_ids}. Using first variant's location.")
    location_id = next(v['location_id'] for v in variants if v['location_id'])
    print(f"Location: {location_id}")

    # Match variants to stock and compute desired quantities
    updates = []
    policy_fixes = []  # variants with CONTINUE that should be DENY
    skipped_no_sku = 0
    skipped_no_stock = 0
    unchanged = 0

    print(f"\n{'SKU':<35} {'Price':>7} {'Stock':>5} {'Listed':>6} {'Avail':>5} {'Action':<10}")
    print(f"{'\u2500'*80}")

    for variant in sorted(variants, key=lambda v: v.get('sku', '')):
        sku = variant.get('sku', '').strip()

        if not sku:
            skipped_no_sku += 1
            continue

        # Track variants that allow overselling (skip Pre-Order variants — they need CONTINUE)
        if variant.get('inventory_policy') == 'CONTINUE' and not sku.endswith('-PO'):
            policy_fixes.append({
                'variant_id': variant['variant_id'],
                'product_id': variant['product_id'],
            })

        stock = stock_data.get(sku)
        if stock is None:
            skipped_no_stock += 1
            continue

        price_gbp = stock.get('selling_price_gbp')
        quantity = stock.get('quantity', 0)
        listed_qty = stock.get('listed_qty', quantity)
        current_available = variant.get('available', 0)

        action = ''
        if listed_qty != current_available:
            action = f"{current_available}\u2192{listed_qty}"
            updates.append({
                'inventory_item_id': variant['inventory_item_id'],
                'quantity': listed_qty,
                'sku': sku,
                'old_available': current_available,
                'new_available': listed_qty,
            })
        else:
            action = 'ok'
            unchanged += 1

        price_str = f"\u00a3{price_gbp:>5.2f}" if price_gbp else "     -"
        print(f"{sku:<35} {price_str:>7} {quantity:>5} {listed_qty:>6} {current_available:>5} {action:<10}")

    print(f"{'\u2500'*80}")
    print(f"{len(variants)} variants checked: {len(updates)} to update, {unchanged} unchanged")
    if skipped_no_sku:
        print(f"  {skipped_no_sku} skipped (no SKU)")
    if skipped_no_stock:
        print(f"  {skipped_no_stock} skipped (no stock record)")

    if policy_fixes:
        print(f"  {len(policy_fixes)} variants have 'Continue selling when out of stock' enabled")

    if not updates and not policy_fixes:
        print("\nNo updates needed.")
        return

    if args.dry_run:
        if updates:
            print(f"\n[DRY RUN] Would update {len(updates)} variant quantities.")
        if policy_fixes:
            print(f"[DRY RUN] Would set {len(policy_fixes)} variants to stop selling at 0.")
        return

    # Batch push via inventorySetQuantities
    if updates:
        batch_items = [
            {'inventory_item_id': u['inventory_item_id'], 'quantity': u['quantity']}
            for u in updates
        ]
        print(f"\nPushing {len(updates)} quantity updates to Shopify (batches of 100)...")
        results = client.set_inventory_quantities(location_id, batch_items)

        total_success = sum(r['count'] for r in results if r['success'])
        total_failed = sum(r['count'] for r in results if not r['success'])
        for r in results:
            if not r['success']:
                print(f"  Batch {r['batch']} FAILED: {'; '.join(r['errors'])}")

        print(f"Quantities: {total_success} updated, {total_failed} failed.")

    # Fix inventory policy: CONTINUE → DENY
    if policy_fixes:
        print(f"\nSetting {len(policy_fixes)} variants to stop selling at 0...")
        policy_result = client.set_inventory_policy(policy_fixes, policy='DENY')
        print(f"Policy: {policy_result['updated']} updated, {policy_result['errors']} errors.")
        for err in policy_result['error_details'][:5]:
            print(f"  ERROR: {err}")


if __name__ == '__main__':
    main()
