#!/usr/bin/env python3
"""Quick live test for eBay metadata sync.

Run: .venv/bin/python stock/sync/ebay/test_live.py

Tests:
1. Auth — GeteBayOfficialTime
2. Fetch — first 5 listings via GetMyeBaySelling
3. Dry run — title normalization preview on those 5 listings
"""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', '..'))

from stock.sync.ebay.client import EbayClient
from stock.sync.ebay.normalizer import normalize_title
from stock.sync.ebay.item_specifics import parse_sku, build_item_specifics


def main():
    print("=" * 70)
    print("eBay Metadata Sync — Live Test")
    print("=" * 70)

    # 1. Auth test
    print("\n[1] Testing auth (GeteBayOfficialTime)...")
    try:
        client = EbayClient()
        import xml.etree.ElementTree as ET
        root = ET.Element('GeteBayOfficialTimeRequest')
        root.set('xmlns', 'urn:ebay:apis:eBLBaseComponents')
        c = ET.SubElement(root, 'RequesterCredentials')
        t = ET.SubElement(c, 'eBayAuthToken')
        t.text = client._auth_token()
        xml_body = ET.tostring(root, encoding='unicode', xml_declaration=True)
        resp_root = client.call('GeteBayOfficialTime', xml_body)
        ns = {'e': 'urn:ebay:apis:eBLBaseComponents'}
        ack = resp_root.findtext('e:Ack', namespaces=ns)
        ts = resp_root.findtext('e:Timestamp', namespaces=ns)
        print(f"  Ack: {ack}, eBay time: {ts}")
        if ack != 'Success':
            print("  FAILED — token may be invalid")
            return
        print("  AUTH OK")
    except Exception as e:
        print(f"  FAILED: {e}")
        return

    # 2. Fetch listings
    print("\n[2] Fetching active listings...")
    try:
        listings = client.get_active_listings()
        print(f"  Fetched {len(listings)} listings")
    except Exception as e:
        print(f"  FAILED: {e}")
        return

    with_sku = [l for l in listings if l.get('sku')]
    print(f"  {len(with_sku)} have SKUs")

    # 3. Preview title changes on first 5
    sample = with_sku[:5]
    print(f"\n[3] Title normalization preview (first {len(sample)} listings):")
    print("-" * 70)

    for l in sample:
        old = l['title']
        new = normalize_title(old, l['sku'])
        changed = " << CHANGED" if new != old else ""
        print(f"  SKU:  {l['sku']}")
        print(f"  Old:  {old}")
        print(f"  New:  {new}{changed}")

        # Item specifics
        changes = build_item_specifics(l['sku'], l.get('item_specifics', {}))
        if changes:
            print(f"  Specifics to update: {changes}")
        print()

    print("=" * 70)
    print("Live test complete. Run with --dry-run to see all changes:")
    print("  .venv/bin/python -m stock.sync.ebay --dry-run --title-only")
    print("=" * 70)


if __name__ == '__main__':
    main()
