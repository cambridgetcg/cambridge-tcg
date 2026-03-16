"""Lambda entry point for Shopify listing metadata sync.

Invoked via direct Lambda invocation or EventBridge schedule.
No VPC needed (Shopify is a public API).

Event parameters:
    - dry_run: bool (default: false)
    - skus: list[str] (default: all)
    - sku_prefix: str (default: none) — filter by SKU prefix, e.g. "OP01"
    - sync_title: bool (default: true)
    - sync_description: bool (default: true)
    - sync_tags: bool (default: true)

Environment Variables:
    - SHOPIFY_STORE: e.g. "yourstore.myshopify.com"
    - SHOPIFY_API_PASSWORD: Admin API access token
    - SHOPIFY_API_VERSION: e.g. "2024-01"
"""

import json
import logging
from datetime import datetime

from stock.sync.shopify.client import ShopifyClient
from stock.sync.shopify.sync import sync_listings
from stock.sync.shopify.add_preorder_variants import run as ensure_preorder_variants

logger = logging.getLogger()
logger.setLevel(logging.INFO)


def lambda_handler(event, context):
    """Sync Shopify listing metadata (titles, descriptions, tags)."""
    logger.info("Shopify Metadata Sync Lambda started")
    logger.info(f"Event: {json.dumps(event)}")

    dry_run = event.get('dry_run', False)
    skus = event.get('skus')
    sku_prefix = event.get('sku_prefix')
    do_title = event.get('sync_title', True)
    do_description = event.get('sync_description', True)
    do_tags = event.get('sync_tags', True)

    try:
        client = ShopifyClient()

        result = sync_listings(
            client=client,
            dry_run=dry_run,
            skus=skus,
            sku_prefix=sku_prefix,
            title=do_title,
            description=do_description,
            tags=do_tags,
        )

        summary = {
            'total': result['total'],
            'checked': result['checked'],
            'updated': result['updated'],
            'skipped': result['skipped'],
            'errors': result['errors'],
            'change_count': len(result['changes']),
            'error_details': result['error_details'][:10],
            'timestamp': datetime.now().isoformat(),
            'dry_run': dry_run,
        }

        logger.info(f"Completed: {summary['updated']} updated, {summary['errors']} errors")

        # Ensure all card products have Pre-Order variants (default step)
        ensure_preorder = event.get('ensure_preorder', True)
        if ensure_preorder:
            logger.info("Ensuring Pre-Order variants on all card products...")
            ensure_preorder_variants(dry_run=dry_run)
            logger.info("Pre-Order variant check complete")

        return {
            'statusCode': 200,
            'body': json.dumps(summary, default=str),
        }

    except Exception as e:
        logger.error(f"Lambda error: {e}", exc_info=True)
        return {
            'statusCode': 500,
            'body': json.dumps({
                'success': False,
                'error': str(e),
            }),
        }
