"""
Local CLI for the eBay Browse competitor price monitor.

Usage:
    python -m pricing.browse.cli --dry-run                # preview mode
    python -m pricing.browse.cli --sku OP-OP01-062-JP     # specific SKU
    python -m pricing.browse.cli --max-skus 10            # first N only
    python -m pricing.browse.cli --json                   # JSON output
"""

import argparse
import json
import os
import sys

from dotenv import load_dotenv


def main():
    parser = argparse.ArgumentParser(description='eBay Browse API competitor price monitor')
    parser.add_argument('--dry-run', action='store_true', help='Search but do not store results')
    parser.add_argument('--sku', type=str, help='Scan a specific SKU only')
    parser.add_argument('--max-skus', type=int, default=0, help='Limit number of SKUs to scan')
    parser.add_argument('--json', action='store_true', dest='json_output', help='Output as JSON')
    args = parser.parse_args()

    # Load .env for local credentials
    load_dotenv()

    # Add browse/ and pricing/ to sys.path so flat Lambda imports resolve
    browse_dir = os.path.dirname(os.path.abspath(__file__))
    pricing_dir = os.path.dirname(browse_dir)
    for p in [browse_dir, pricing_dir]:
        if p not in sys.path:
            sys.path.insert(0, p)

    # Build event matching Lambda interface
    event = {
        'dry_run': args.dry_run,
    }
    if args.sku:
        event['skus'] = [args.sku]
    if args.max_skus:
        event['max_skus'] = args.max_skus

    # Import after dotenv + path setup so env vars and flat imports are available
    from lambda_function import lambda_handler

    result = lambda_handler(event, {})

    if args.json_output:
        body = json.loads(result.get('body', '{}'))
        print(json.dumps(body, indent=2))
    else:
        body = json.loads(result.get('body', '{}'))
        status_code = result.get('statusCode', 0)
        print(f"\nStatus: {status_code}")
        if body.get('success'):
            print(f"Scanned: {body.get('scanned', 0)}")
            print(f"Skipped: {body.get('skipped', 0)}")
            print(f"Errors: {body.get('errors', 0)}")
            classifications = body.get('classifications', {})
            if classifications:
                print("Classifications:")
                for cls, count in classifications.items():
                    print(f"  {cls}: {count}")
            acq = body.get('acquisitions', 0)
            if acq:
                print(f"ACQUISITION TARGETS: {acq}")
        else:
            print(f"Error: {body.get('error', 'unknown')}")


if __name__ == '__main__':
    main()
