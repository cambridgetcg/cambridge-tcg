"""Cross-platform stock sync helpers.

Shared by shopify_webhook.py and ebay_poller.py Lambdas.
Handles: RDS sales_events insert, platform_listings lookup,
and cross-platform quantity reduction (eBay ↔ Shopify).

Environment Variables:
    PROXY_ENDPOINT, DB_USER, DB_PASSWORD, DATABASE_NAME, TABLE_NAME
    SHOPIFY_STORE, SHOPIFY_CLIENT_ID, SHOPIFY_CLIENT_SECRET, SHOPIFY_API_VERSION
    (eBay credentials via AWS Secrets Manager)
"""

import json
import logging
import os
import re
import sys
from datetime import datetime, timedelta, timezone

import psycopg2

logger = logging.getLogger(__name__)


def _safe_table_name(name):
    """Validate table name to prevent SQL injection."""
    if not re.match(r'^[a-zA-Z_][a-zA-Z0-9_]*$', name):
        raise ValueError(f"Invalid table name: {name}")
    return name


def get_db_connection():
    """Create a psycopg2 connection to RDS via Proxy."""
    return psycopg2.connect(
        host=os.environ['PROXY_ENDPOINT'],
        user=os.environ['DB_USER'],
        password=os.environ['DB_PASSWORD'],
        port=int(os.environ.get('DB_PORT', '5432')),
        dbname=os.environ.get('DATABASE_NAME', 'op_cardrush_link'),
        connect_timeout=10,
    )


def insert_sale_event(conn, platform, order_id, sku, quantity, event_type='sale',
                      unit_price_gbp=None, raw_payload=None):
    """Insert a sale event into sales_events table. Idempotent via unique constraint.

    Returns True if inserted, False if duplicate (already processed).
    """
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO sales_events "
                "(platform, order_id, sku, quantity, event_type, unit_price_gbp, raw_payload) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (platform, order_id, sku, event_type) DO NOTHING "
                "RETURNING id",
                (platform, str(order_id), sku, quantity, event_type,
                 unit_price_gbp, json.dumps(raw_payload) if raw_payload else None),
            )
            inserted = cur.fetchone() is not None
            conn.commit()
            return inserted
    except Exception as e:
        logger.error(f"Failed to insert sale event: {e}")
        conn.rollback()
        raise


def mark_cross_synced(conn, platform, order_id, sku, event_type='sale', error=None):
    """Mark a sales_event as cross-synced (or record cross-sync error)."""
    try:
        with conn.cursor() as cur:
            if error:
                cur.execute(
                    "UPDATE sales_events SET cross_sync_error = %s "
                    "WHERE platform = %s AND order_id = %s AND sku = %s AND event_type = %s",
                    (str(error), platform, str(order_id), sku, event_type),
                )
            else:
                cur.execute(
                    "UPDATE sales_events SET cross_synced = TRUE, cross_synced_at = NOW() "
                    "WHERE platform = %s AND order_id = %s AND sku = %s AND event_type = %s",
                    (platform, str(order_id), sku, event_type),
                )
            conn.commit()
    except Exception as e:
        logger.error(f"Failed to mark cross_synced: {e}")
        conn.rollback()
        raise


def lookup_platform_listing(conn, sku, platform):
    """Look up a SKU's listing on a given platform.

    Returns {platform_id, secondary_id, current_available} or None.
    """
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT platform_id, secondary_id, current_available "
                "FROM platform_listings WHERE sku = %s AND platform = %s",
                (sku, platform),
            )
            row = cur.fetchone()
            if row:
                return {
                    'platform_id': row[0],
                    'secondary_id': row[1],
                    'current_available': row[2] or 0,
                }
    except Exception as e:
        logger.error(f"Failed to lookup platform listing for {sku}/{platform}: {e}")
        raise
    return None


def lookup_and_lock_platform_listing(conn, sku, platform):
    """Look up and row-lock a SKU's listing. Caller must commit/rollback to release.

    Uses SELECT ... FOR UPDATE to prevent race conditions in concurrent
    cross-sync operations (e.g. simultaneous Shopify webhook + eBay poller).

    Returns {platform_id, secondary_id, current_available, last_refreshed} or None.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT platform_id, secondary_id, current_available, last_refreshed "
            "FROM platform_listings WHERE sku = %s AND platform = %s FOR UPDATE",
            (sku, platform),
        )
        row = cur.fetchone()
        if row:
            return {
                'platform_id': row[0],
                'secondary_id': row[1],
                'current_available': row[2] or 0,
                'last_refreshed': row[3],
            }
    return None


def reduce_ebay_quantity(ebay_client, item_id, qty_to_reduce, current_available):
    """Reduce eBay listing quantity via ReviseFixedPriceItem.

    Args:
        ebay_client: EbayClient instance
        item_id: eBay ItemID
        qty_to_reduce: Positive int to subtract from available
        current_available: Current available quantity on eBay

    Returns:
        {success: bool, new_quantity: int, error: str|None}
    """
    new_qty = max(0, current_available - qty_to_reduce)
    try:
        result = ebay_client.revise_item(item_id, quantity=new_qty)
        if result['ack'] in ('Success', 'Warning'):
            return {'success': True, 'new_quantity': new_qty, 'error': None}
        else:
            error = '; '.join(result.get('errors', []))
            return {'success': False, 'new_quantity': current_available, 'error': error}
    except Exception as e:
        return {'success': False, 'new_quantity': current_available, 'error': str(e)}


def reduce_shopify_quantity(shopify_client, inventory_item_id, location_id,
                            qty_to_reduce, current_available):
    """Reduce Shopify inventory via inventorySetQuantities.

    Args:
        shopify_client: ShopifyClient instance
        inventory_item_id: Shopify InventoryItem GID
        location_id: Shopify Location GID
        qty_to_reduce: Positive int to subtract from available
        current_available: Current available quantity on Shopify

    Returns:
        {success: bool, new_quantity: int, error: str|None}
    """
    new_qty = max(0, current_available - qty_to_reduce)
    try:
        results = shopify_client.set_inventory_quantities(
            location_id,
            [{'inventory_item_id': inventory_item_id, 'quantity': new_qty}],
        )
        if results and results[0]['success']:
            return {'success': True, 'new_quantity': new_qty, 'error': None}
        else:
            errors = results[0].get('errors', []) if results else ['No result']
            return {'success': False, 'new_quantity': current_available, 'error': '; '.join(errors)}
    except Exception as e:
        return {'success': False, 'new_quantity': current_available, 'error': str(e)}


def update_platform_available(conn, sku, platform, new_available):
    """Update the current_available count in platform_listings cache."""
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE platform_listings SET current_available = %s, last_refreshed = NOW() "
                "WHERE sku = %s AND platform = %s",
                (new_available, sku, platform),
            )
            conn.commit()
    except Exception as e:
        logger.error(f"Failed to update platform_available for {sku}/{platform}: {e}")
        try:
            conn.rollback()
        except Exception:
            pass
        raise


def check_listing_staleness(listing, max_age_hours=24):
    """Check if a platform_listings entry is stale.

    Args:
        listing: dict with 'last_refreshed' key (datetime or None)
        max_age_hours: threshold in hours (default 24)

    Returns:
        True if stale (last_refreshed is older than max_age_hours or None), False otherwise.
    """
    last_refreshed = listing.get('last_refreshed')
    if last_refreshed is None:
        logger.warning("Listing has no last_refreshed timestamp — treating as stale")
        return True

    # Ensure timezone-aware comparison
    if last_refreshed.tzinfo is None:
        last_refreshed = last_refreshed.replace(tzinfo=timezone.utc)

    age = datetime.now(timezone.utc) - last_refreshed
    if age > timedelta(hours=max_age_hours):
        logger.warning(
            f"Listing cache is {age.total_seconds() / 3600:.1f}h old "
            f"(threshold: {max_age_hours}h) — data may be outdated"
        )
        return True
    return False


def reduce_stock_inventory(conn, sku, quantity_sold):
    """Reduce stock_inventory quantity. Row-locks for concurrency safety.

    Returns {old_qty, new_qty, clamped} or None if SKU not found.
    Does NOT commit — caller manages the transaction.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT quantity FROM stock_inventory WHERE sku = %s FOR UPDATE",
            (sku,),
        )
        row = cur.fetchone()
        if row is None:
            logger.warning(f"SKU {sku} not in stock_inventory — skipping reduction")
            return None

        old_qty = row[0]
        new_qty = max(0, old_qty - quantity_sold)
        clamped = (old_qty - quantity_sold) < 0

        cur.execute(
            "UPDATE stock_inventory SET quantity = %s, last_updated = NOW() WHERE sku = %s",
            (new_qty, sku),
        )
        logger.info(f"stock_inventory {sku}: {old_qty} → {new_qty} (sold {quantity_sold})")
        return {'old_qty': old_qty, 'new_qty': new_qty, 'clamped': clamped}


def restore_stock_inventory(conn, sku, quantity_restored):
    """Restore stock_inventory quantity (cancellations).

    Returns {old_qty, new_qty} or None if SKU not found.
    Does NOT commit — caller manages the transaction.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT quantity FROM stock_inventory WHERE sku = %s FOR UPDATE",
            (sku,),
        )
        row = cur.fetchone()
        if row is None:
            logger.warning(f"SKU {sku} not in stock_inventory — skipping restore")
            return None

        old_qty = row[0]
        new_qty = old_qty + quantity_restored

        cur.execute(
            "UPDATE stock_inventory SET quantity = %s, last_updated = NOW() WHERE sku = %s",
            (new_qty, sku),
        )
        logger.info(f"stock_inventory {sku}: {old_qty} → {new_qty} (restored {quantity_restored})")
        return {'old_qty': old_qty, 'new_qty': new_qty}


def record_pipeline_run(conn, stage, status, rows_affected=0, detail=None):
    """Insert a row into pipeline_runs. Never raises."""
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO pipeline_runs (stage, status, rows_affected, detail) "
                "VALUES (%s, %s, %s, %s)",
                (stage, status, rows_affected, detail),
            )
            conn.commit()
    except Exception as e:
        logger.error(f"Failed to record pipeline run for {stage}: {e}")
        try:
            conn.rollback()
        except Exception:
            pass


def get_last_poll_time(conn, stage):
    """Get the most recent completed_at for a pipeline stage.

    Returns ISO datetime string or None if no previous run.
    """
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT completed_at FROM pipeline_runs "
                "WHERE stage = %s AND status = 'success' "
                "ORDER BY completed_at DESC LIMIT 1",
                (stage,),
            )
            row = cur.fetchone()
            if row and row[0]:
                return row[0].isoformat() + 'Z'
    except Exception as e:
        logger.error(f"Failed to get last poll time for {stage}: {e}")
    return None
