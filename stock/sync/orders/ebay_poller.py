"""eBay Order Poller Lambda.

Runs on EventBridge schedule (every 5 minutes). Fetches completed eBay orders
since last poll, inserts sale events into RDS, and reduces Shopify inventory
for each sold SKU.

Flow:
    1. Get last poll time from pipeline_runs
    2. GetOrders(CreateTimeFrom=last_poll, OrderStatus=Completed)
    3. For each line item: insert sales_event, reduce Shopify qty
    4. Record pipeline_run

Environment Variables:
    PROXY_ENDPOINT, DB_USER, DB_PASSWORD, DATABASE_NAME
    SHOPIFY_STORE, SHOPIFY_CLIENT_ID, SHOPIFY_CLIENT_SECRET, SHOPIFY_API_VERSION
    (eBay credentials via AWS Secrets Manager)
"""

import json
import logging
import os
import sys
from datetime import datetime, timedelta, timezone

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Add parent paths for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', '..'))

from stock.sync.ebay.client import EbayClient
from stock.sync.shopify.client import ShopifyClient
from stock.sync.orders.cross_sync import (
    get_db_connection,
    insert_sale_event,
    mark_cross_synced,
    lookup_platform_listing,
    lookup_and_lock_platform_listing,
    check_listing_staleness,
    reduce_shopify_quantity,
    reduce_stock_inventory,
    restore_stock_inventory,
    update_platform_available,
    record_pipeline_run,
    get_last_poll_time,
)

STAGE_NAME = 'ebay_order_poller'
DEFAULT_LOOKBACK_MINUTES = 10


def lambda_handler(event, context):
    """Poll eBay for new orders and cross-sync to Shopify."""
    conn = None
    try:
        conn = get_db_connection()
        ebay_client = EbayClient()
        shopify_client = ShopifyClient()

        # 1. Determine poll window
        last_poll = get_last_poll_time(conn, STAGE_NAME)
        now = datetime.now(timezone.utc)

        if last_poll:
            create_time_from = last_poll
        else:
            # First run: look back DEFAULT_LOOKBACK_MINUTES
            lookback = now - timedelta(minutes=DEFAULT_LOOKBACK_MINUTES)
            create_time_from = lookback.strftime('%Y-%m-%dT%H:%M:%S.000Z')

        create_time_to = now.strftime('%Y-%m-%dT%H:%M:%S.000Z')
        logger.info(f"Polling eBay orders from {create_time_from} to {create_time_to}")

        # 2. Fetch orders
        orders = ebay_client.get_orders(create_time_from, create_time_to)
        logger.info(f"Fetched {len(orders)} orders with line items")

        # 3. Process each line item
        events_inserted = 0
        events_skipped = 0
        cross_synced = 0
        cross_failed = 0

        for order in orders:
            order_id = order['order_id']
            for item in order['line_items']:
                sku = item['sku']
                quantity = item['quantity']
                price_gbp = item.get('price_gbp')

                # Insert sale event (idempotent)
                inserted = insert_sale_event(
                    conn,
                    platform='ebay',
                    order_id=order_id,
                    sku=sku,
                    quantity=quantity,
                    event_type='sale',
                    unit_price_gbp=price_gbp,
                    raw_payload={'item_id': item.get('item_id')},
                )

                if not inserted:
                    events_skipped += 1
                    continue

                events_inserted += 1

                # Reduce central stock inventory
                try:
                    reduce_stock_inventory(conn, sku, quantity)
                    conn.commit()
                except Exception as e:
                    conn.rollback()
                    logger.error(f"stock_inventory update failed for {sku}: {e}")

                # Cross-sync: reduce Shopify inventory (with row lock)
                try:
                    listing = lookup_and_lock_platform_listing(conn, sku, 'shopify')
                    if listing is None:
                        conn.rollback()  # release any advisory lock
                        logger.warning(f"No Shopify listing for {sku} — skipping cross-sync")
                        mark_cross_synced(conn, 'ebay', order_id, sku, error='no_shopify_listing')
                        continue

                    check_listing_staleness(listing)

                    result = reduce_shopify_quantity(
                        shopify_client,
                        inventory_item_id=listing['platform_id'],
                        location_id=listing['secondary_id'],
                        qty_to_reduce=quantity,
                        current_available=listing['current_available'],
                    )

                    if result['success']:
                        cross_synced += 1
                        mark_cross_synced(conn, 'ebay', order_id, sku)
                        update_platform_available(conn, sku, 'shopify', result['new_quantity'])
                    else:
                        cross_failed += 1
                        logger.error(f"Cross-sync failed for {sku}: {result['error']}")
                        mark_cross_synced(conn, 'ebay', order_id, sku, error=result['error'])
                except Exception as e:
                    conn.rollback()  # release row lock on error
                    cross_failed += 1
                    logger.error(f"Cross-sync error for {sku}: {e}")
                    try:
                        mark_cross_synced(conn, 'ebay', order_id, sku, error=str(e))
                    except Exception:
                        pass

        # 4. Record pipeline run
        detail = (f"orders={len(orders)}, inserted={events_inserted}, "
                  f"skipped={events_skipped}, synced={cross_synced}, failed={cross_failed}")
        status = 'success' if cross_failed == 0 else 'partial'
        record_pipeline_run(conn, STAGE_NAME, status, rows_affected=events_inserted, detail=detail)

        logger.info(f"Done: {detail}")
        return {
            'statusCode': 200,
            'body': json.dumps({
                'orders_fetched': len(orders),
                'events_inserted': events_inserted,
                'events_skipped': events_skipped,
                'cross_synced': cross_synced,
                'cross_failed': cross_failed,
            }),
        }

    except Exception as e:
        logger.error(f"eBay order poller failed: {e}", exc_info=True)
        if conn:
            record_pipeline_run(conn, STAGE_NAME, 'failure', detail=str(e)[:500])
        return {
            'statusCode': 500,
            'body': json.dumps({'error': str(e)[:500]}),
        }
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass
