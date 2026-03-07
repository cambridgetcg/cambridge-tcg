"""Migrate stock_data.json into RDS stock_inventory + stock_config tables.

Reads the local JSON file and bulk-inserts into the new RDS tables.
Idempotent: uses ON CONFLICT DO UPDATE so it's safe to re-run.

Usage:
    python -m pricing.migrations.migrate_stock_to_rds [--dry-run]
    python -m pricing.migrations.migrate_stock_to_rds --json-path /path/to/stock_data.json
"""

import argparse
import json
import os
import sys

import psycopg2
from psycopg2.extras import execute_batch


DEFAULT_JSON_PATH = os.path.join(
    os.path.dirname(__file__), '..', '..', 'stock', 'count', 'stock_data.json'
)


def main():
    parser = argparse.ArgumentParser(description='Migrate stock_data.json to RDS')
    parser.add_argument('--dry-run', action='store_true', help='Preview without writing')
    parser.add_argument('--json-path', default=DEFAULT_JSON_PATH, help='Path to stock_data.json')
    args = parser.parse_args()

    # Load JSON
    json_path = os.path.abspath(args.json_path)
    if not os.path.exists(json_path):
        print(f"Error: {json_path} not found", file=sys.stderr)
        sys.exit(1)

    with open(json_path, 'r') as f:
        data = json.load(f)

    stock = data.get('stock', {})
    metadata = data.get('metadata', {})

    print(f"Loaded {len(stock)} SKUs from {json_path}")

    # Connect to RDS
    host = os.environ.get('PROXY_ENDPOINT')
    user = os.environ.get('DB_USER')
    password = os.environ.get('DB_PASSWORD')
    port = int(os.environ.get('DB_PORT', '5432'))
    database = os.environ.get('DATABASE_NAME', 'op_cardrush_link')

    missing = []
    if not host: missing.append('PROXY_ENDPOINT')
    if not user: missing.append('DB_USER')
    if not password: missing.append('DB_PASSWORD')
    if missing:
        print(f"Error: Missing env vars: {', '.join(missing)}", file=sys.stderr)
        sys.exit(1)

    print(f"Connecting to {host}:{port}/{database}...")
    conn = psycopg2.connect(
        host=host, user=user, password=password,
        port=port, dbname=database, connect_timeout=10,
    )

    try:
        # Migrate stock records
        rows = []
        for sku, entry in stock.items():
            rows.append((
                sku,
                entry.get('quantity', 0),
                entry.get('total_cost_yen', 0),
                entry.get('purchased_qty', 0),
                entry.get('last_updated'),
            ))

        if args.dry_run:
            print(f"\n[DRY RUN] Would upsert {len(rows)} stock_inventory rows")
        else:
            with conn.cursor() as cur:
                execute_batch(
                    cur,
                    "INSERT INTO stock_inventory (sku, quantity, total_cost_yen, purchased_qty, last_updated) "
                    "VALUES (%s, %s, %s, %s, COALESCE(%s::timestamp, NOW())) "
                    "ON CONFLICT (sku) DO UPDATE SET "
                    "quantity = EXCLUDED.quantity, "
                    "total_cost_yen = EXCLUDED.total_cost_yen, "
                    "purchased_qty = EXCLUDED.purchased_qty, "
                    "last_updated = EXCLUDED.last_updated",
                    rows,
                    page_size=100,
                )
                conn.commit()
            print(f"Upserted {len(rows)} stock_inventory rows")

        # Migrate listing tiers
        listing_tiers = metadata.get('listing_tiers')
        default_cap = metadata.get('listing_default_cap', 1)

        if listing_tiers is not None:
            if args.dry_run:
                print(f"[DRY RUN] Would write listing_tiers: {listing_tiers}")
                print(f"[DRY RUN] Would write listing_default_cap: {default_cap}")
            else:
                with conn.cursor() as cur:
                    cur.execute(
                        "INSERT INTO stock_config (config_key, config_value) "
                        "VALUES ('listing_tiers', %s::jsonb) "
                        "ON CONFLICT (config_key) DO UPDATE SET "
                        "config_value = EXCLUDED.config_value, updated_at = NOW()",
                        (json.dumps(listing_tiers),),
                    )
                    cur.execute(
                        "INSERT INTO stock_config (config_key, config_value) "
                        "VALUES ('listing_default_cap', %s::jsonb) "
                        "ON CONFLICT (config_key) DO UPDATE SET "
                        "config_value = EXCLUDED.config_value, updated_at = NOW()",
                        (json.dumps(default_cap),),
                    )
                    conn.commit()
                print(f"Written listing_tiers ({len(listing_tiers)} tiers) and listing_default_cap ({default_cap})")
        else:
            print("No listing tiers in metadata — skipped")

        # Summary
        if not args.dry_run:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM stock_inventory")
                count = cur.fetchone()[0]
                cur.execute("SELECT SUM(quantity) FROM stock_inventory")
                total_qty = cur.fetchone()[0] or 0
                cur.execute("SELECT COUNT(*) FROM stock_inventory WHERE total_cost_yen > 0")
                with_cost = cur.fetchone()[0]
                cur.execute("SELECT COUNT(*) FROM stock_config")
                config_count = cur.fetchone()[0]
            print(f"\nVerification:")
            print(f"  stock_inventory: {count} rows, {total_qty} total qty, {with_cost} with cost data")
            print(f"  stock_config: {config_count} rows")

    finally:
        conn.close()


if __name__ == '__main__':
    main()
