"""Push listed stock quantities to eBay.

Gets stock data (with tier-capped listed_qty) from the admin Lambda,
fetches active eBay listings, and pushes listed_qty via ReviseFixedPriceItem.

eBay GTC listings: Quantity field = desired available (eBay adds QuantitySold internally).

Usage:
    python -m stock.count.push_ebay_stock [--dry-run]
"""

import argparse
import json
import os
import sys

# Add repo root to path for cross-package imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from stock.sync.ebay.client import EbayClient


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
    parser = argparse.ArgumentParser(description='Push listed stock quantities to eBay')
    parser.add_argument('--dry-run', action='store_true', help='Preview without updating')
    args = parser.parse_args()

    # Get stock data from admin Lambda (reads stock_inventory + computes listed_qty)
    print("Loading inventory from admin Lambda...")
    stock_data = _get_stock_from_lambda()
    print(f"Loaded {len(stock_data)} SKUs")

    # Fetch active eBay listings
    print("\nConnecting to eBay...")
    client = EbayClient()
    listings = client.get_active_listings()
    print(f"Fetched {len(listings)} active listings")

    # Match listings to stock and compute desired quantities
    updates = []
    skipped_no_sku = 0
    skipped_no_stock = 0
    unchanged = 0

    print(f"\n{'SKU':<35} {'Price':>7} {'Stock':>5} {'Listed':>6} {'Avail':>5} {'Action':<10}")
    print(f"{'─'*80}")

    for listing in sorted(listings, key=lambda l: l.get('sku', '')):
        sku = listing.get('sku', '').strip()
        item_id = listing['item_id']

        if not sku:
            skipped_no_sku += 1
            continue

        stock = stock_data.get(sku)
        if stock is None:
            skipped_no_stock += 1
            continue

        price_gbp = stock.get('selling_price_gbp')
        quantity = stock.get('quantity', 0)
        listed_qty = stock.get('listed_qty', quantity)

        quantity_sold = listing.get('quantity_sold', 0)
        current_total = listing.get('quantity', 0)
        current_available = current_total - quantity_sold

        action = ''
        if listed_qty != current_available:
            action = f"{current_available}→{listed_qty}"
            updates.append({
                'item_id': item_id,
                'quantity': listed_qty,  # eBay GTC: Quantity = desired available
                'sku': sku,
                'old_available': current_available,
                'new_available': listed_qty,
            })
        else:
            action = 'ok'
            unchanged += 1

        price_str = f"£{price_gbp:>5.2f}" if price_gbp else "     -"
        print(f"{sku:<35} {price_str:>7} {quantity:>5} {listed_qty:>6} {current_available:>5} {action:<10}")

    print(f"{'─'*80}")
    print(f"{len(listings)} listings checked: {len(updates)} to update, {unchanged} unchanged")
    if skipped_no_sku:
        print(f"  {skipped_no_sku} skipped (no SKU)")
    if skipped_no_stock:
        print(f"  {skipped_no_stock} skipped (no stock record)")

    if not updates:
        print("\nNo updates needed.")
        return

    if args.dry_run:
        print(f"\n[DRY RUN] Would update {len(updates)} listings.")
        return

    # Push updates via ReviseFixedPriceItem (1 per call, reliable for GTC qty)
    print(f"\nPushing {len(updates)} quantity updates to eBay...")
    success = 0
    failed = 0
    for u in updates:
        result = client.revise_item(u['item_id'], quantity=u['quantity'])
        if result['ack'] in ('Success', 'Warning'):
            success += 1
        else:
            failed += 1
            print(f"  FAILED {u['sku']}: {result['errors']}")

    print(f"Done. {success} updated, {failed} failed.")


if __name__ == '__main__':
    main()
