"""Lambda entry point for eBay listing metadata sync.

Invoked via direct Lambda invocation or EventBridge schedule.

Event parameters:
    - dry_run: bool (default: false)
    - skus: list[str] (default: all)
    - sku_prefix: str (default: none) — filter by SKU prefix, e.g. "OP01"
    - sync_title: bool (default: true)
    - sync_description: bool (default: true)
    - sync_specifics: bool (default: true)
    - sync_pictures: bool (default: false) — append condition guide images
    - workers: int (default: 5)
"""

import json
import logging
from datetime import datetime

from stock.sync.ebay.client import EbayClient
from stock.sync.ebay.sync import sync_listings

logger = logging.getLogger()
logger.setLevel(logging.INFO)


def lambda_handler(event, context):
    """Sync eBay listing metadata (titles, descriptions, item specifics)."""
    logger.info("eBay Metadata Sync Lambda started")
    logger.info(f"Event: {json.dumps(event)}")

    dry_run = event.get('dry_run', False)
    skus = event.get('skus')
    sku_prefix = event.get('sku_prefix')
    do_title = event.get('sync_title', True)
    do_description = event.get('sync_description', True)
    do_specifics = event.get('sync_specifics', True)
    do_pictures = event.get('sync_pictures', False)
    workers = event.get('workers', 5)

    try:
        client = EbayClient()

        result = sync_listings(
            client=client,
            dry_run=dry_run,
            skus=skus,
            sku_prefix=sku_prefix,
            title=do_title,
            description=do_description,
            specifics=do_specifics,
            pictures=do_pictures,
            workers=workers,
        )

        # Truncate changes detail for Lambda response (avoid payload size issues)
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
