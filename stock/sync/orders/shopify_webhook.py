"""Shopify Order Webhook Lambda.

Receives orders/create and orders/cancelled webhooks via API Gateway.
Validates HMAC-SHA256 signature, inserts sale events into RDS,
and reduces eBay inventory for each sold SKU.

Flow:
    1. Validate HMAC-SHA256 (X-Shopify-Hmac-Sha256 + SHOPIFY_WEBHOOK_SECRET)
    2. Parse topic (orders/create vs orders/cancelled)
    3. Extract line items with SKU + quantity
    4. For each SKU: insert sales_event, reduce eBay qty
    5. Return 200 (must respond within 5s for Shopify)

Environment Variables:
    PROXY_ENDPOINT, DB_USER, DB_PASSWORD, DATABASE_NAME
    SHOPIFY_WEBHOOK_SECRET: Webhook signing secret from Shopify
    (eBay credentials via AWS Secrets Manager)
"""

import base64
import hashlib
import hmac
import json
import logging
import os
import sys

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Add parent paths for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', '..'))

from stock.sync.ebay.client import EbayClient
from stock.sync.orders.cross_sync import (
    get_db_connection,
    insert_sale_event,
    mark_cross_synced,
    lookup_platform_listing,
    lookup_and_lock_platform_listing,
    check_listing_staleness,
    reduce_ebay_quantity,
    reduce_stock_inventory,
    restore_stock_inventory,
    update_platform_available,
    record_pipeline_run,
)

STAGE_NAME = 'shopify_order_webhook'


def _verify_hmac(body_bytes, hmac_header, secret):
    """Verify Shopify webhook HMAC-SHA256 signature."""
    digest = hmac.new(
        secret.encode('utf-8'),
        body_bytes,
        hashlib.sha256,
    ).digest()
    computed = base64.b64encode(digest).decode('utf-8')
    return hmac.compare_digest(computed, hmac_header)


def lambda_handler(event, context):
    """Handle Shopify order webhook (orders/create or orders/cancelled)."""
    conn = None
    try:
        # 1. Extract and validate HMAC
        headers = event.get('headers', {})
        # API Gateway v2 lowercases header names
        hmac_header = (headers.get('x-shopify-hmac-sha256')
                       or headers.get('X-Shopify-Hmac-Sha256', ''))
        topic = (headers.get('x-shopify-topic')
                 or headers.get('X-Shopify-Topic', ''))

        body_str = event.get('body', '')
        if event.get('isBase64Encoded'):
            body_bytes = base64.b64decode(body_str)
            body_str = body_bytes.decode('utf-8')
        else:
            body_bytes = body_str.encode('utf-8')

        secret = os.environ.get('SHOPIFY_WEBHOOK_SECRET', '')
        if not secret:
            logger.error("SHOPIFY_WEBHOOK_SECRET not configured")
            return {'statusCode': 500, 'body': 'Server configuration error'}

        if not _verify_hmac(body_bytes, hmac_header, secret):
            logger.warning("HMAC verification failed")
            return {'statusCode': 401, 'body': 'Unauthorized'}

        # 2. Parse order payload
        order = json.loads(body_str)
        order_id = str(order.get('id', ''))
        order_name = order.get('name', '')

        # Determine event type from topic
        if 'cancelled' in topic:
            event_type = 'cancellation'
            qty_sign = -1  # negative = cancellation restores stock
        else:
            event_type = 'sale'
            qty_sign = 1

        logger.info(f"Webhook: {topic} order={order_name} ({order_id})")

        # 3. Extract line items
        line_items = order.get('line_items', [])
        items_to_process = []
        for li in line_items:
            sku = (li.get('sku') or '').strip()
            quantity = li.get('quantity', 0)
            price_gbp = None
            try:
                price_gbp = float(li.get('price', 0))
            except (ValueError, TypeError):
                pass

            if sku and quantity > 0:
                items_to_process.append({
                    'sku': sku,
                    'quantity': quantity * qty_sign,
                    'price_gbp': price_gbp,
                })

        if not items_to_process:
            logger.info("No SKU line items to process")
            return {'statusCode': 200, 'body': 'OK (no items)'}

        # 4. Connect and process
        conn = get_db_connection()
        ebay_client = None  # lazy init

        events_inserted = 0
        cross_synced = 0
        cross_failed = 0

        for item in items_to_process:
            sku = item['sku']
            quantity = item['quantity']
            abs_quantity = abs(quantity)

            # Insert sale event (idempotent)
            inserted = insert_sale_event(
                conn,
                platform='shopify',
                order_id=order_id,
                sku=sku,
                quantity=quantity,
                event_type=event_type,
                unit_price_gbp=item.get('price_gbp'),
                raw_payload={'order_name': order_name},
            )

            if not inserted:
                continue  # duplicate, already processed

            events_inserted += 1

            # Reduce central stock inventory
            try:
                if event_type == 'sale':
                    reduce_stock_inventory(conn, sku, abs_quantity)
                else:
                    restore_stock_inventory(conn, sku, abs_quantity)
                conn.commit()
            except Exception as e:
                conn.rollback()
                logger.error(f"stock_inventory update failed for {sku}: {e}")

            # Cross-sync: adjust eBay inventory (with row lock)
            try:
                listing = lookup_and_lock_platform_listing(conn, sku, 'ebay')
                if listing is None:
                    conn.rollback()  # release any advisory lock
                    logger.info(f"No eBay listing for {sku} — skipping cross-sync")
                    mark_cross_synced(conn, 'shopify', order_id, sku, event_type,
                                      error='no_ebay_listing')
                    continue

                check_listing_staleness(listing)

                # Lazy-init eBay client (only when needed)
                if ebay_client is None:
                    ebay_client = EbayClient()

                if event_type == 'cancellation':
                    # Cancellation: increase eBay qty back
                    new_qty = listing['current_available'] + abs_quantity
                    result = ebay_client.revise_item(listing['platform_id'], quantity=new_qty)
                    if result['ack'] in ('Success', 'Warning'):
                        cross_synced += 1
                        mark_cross_synced(conn, 'shopify', order_id, sku, event_type)
                        update_platform_available(conn, sku, 'ebay', new_qty)
                    else:
                        error = '; '.join(result.get('errors', []))
                        cross_failed += 1
                        mark_cross_synced(conn, 'shopify', order_id, sku, event_type, error=error)
                else:
                    # Sale: reduce eBay qty
                    result = reduce_ebay_quantity(
                        ebay_client,
                        item_id=listing['platform_id'],
                        qty_to_reduce=abs_quantity,
                        current_available=listing['current_available'],
                    )

                    if result['success']:
                        cross_synced += 1
                        mark_cross_synced(conn, 'shopify', order_id, sku, event_type)
                        update_platform_available(conn, sku, 'ebay', result['new_quantity'])
                    else:
                        cross_failed += 1
                        logger.error(f"Cross-sync failed for {sku}: {result['error']}")
                        mark_cross_synced(conn, 'shopify', order_id, sku, event_type,
                                          error=result['error'])
            except Exception as e:
                conn.rollback()  # release row lock on error
                cross_failed += 1
                logger.error(f"Cross-sync error for {sku}: {e}")
                try:
                    mark_cross_synced(conn, 'shopify', order_id, sku, event_type, error=str(e))
                except Exception:
                    pass

        # 5. Record pipeline run
        status = 'success' if cross_failed == 0 else 'partial'
        detail = (f"topic={topic}, order={order_name}, items={len(items_to_process)}, "
                  f"inserted={events_inserted}, synced={cross_synced}, failed={cross_failed}")
        record_pipeline_run(conn, STAGE_NAME, status, rows_affected=events_inserted, detail=detail)

        logger.info(f"Done: {detail}")
        return {'statusCode': 200, 'body': 'OK'}

    except Exception as e:
        logger.error(f"Shopify webhook handler failed: {e}", exc_info=True)
        if conn:
            record_pipeline_run(conn, STAGE_NAME, 'failure', detail=str(e)[:500])
        # Return 200 to prevent Shopify retries on server errors
        # (we log the failure in RDS for manual review)
        return {'statusCode': 200, 'body': 'OK (internal error logged)'}
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass
