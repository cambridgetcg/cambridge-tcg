"""Core sync orchestration for eBay listing metadata.

Fetches listings from eBay, normalizes titles/descriptions/item specifics,
and pushes updates back. Shared between CLI and Lambda entry points.
"""

import concurrent.futures
import logging

from stock.sync.ebay.normalizer import normalize_title
from stock.sync.ebay.description import generate_description
from stock.sync.ebay.item_specifics import build_item_specifics

logger = logging.getLogger(__name__)

# Condition guide images (S3 source) — uploaded to eBay EPS at sync time
CONDITION_GUIDE_S3_URLS = [
    'https://jp-op-photos.s3.us-east-1.amazonaws.com/condition-guide/01-no-white-spots.jpeg',
    'https://jp-op-photos.s3.us-east-1.amazonaws.com/condition-guide/02-no-crease-dent.jpeg',
    'https://jp-op-photos.s3.us-east-1.amazonaws.com/condition-guide/03-no-foil-discolouring.jpeg',
    'https://jp-op-photos.s3.us-east-1.amazonaws.com/condition-guide/04-slight-edge-whitening.jpeg',
    'https://jp-op-photos.s3.us-east-1.amazonaws.com/condition-guide/05-slight-off-centering.jpeg',
]

# Cache for EPS URLs (populated once per Lambda invocation)
_eps_guide_urls = None


def sync_listings(client, dry_run=False, skus=None, sku_prefix=None,
                  title=True, description=True, specifics=True,
                  pictures=False, workers=5):
    """
    Fetch listings from eBay, normalize, push updates.

    Args:
        client: EbayClient instance
        dry_run: Preview changes without pushing
        skus: Optional set/list of SKUs to filter (None = all)
        sku_prefix: Optional SKU prefix to filter, e.g. "OP01"
        title: Whether to sync titles
        description: Whether to sync descriptions
        specifics: Whether to sync item specifics
        pictures: Whether to append condition guide images
        workers: Number of concurrent workers for ReviseFixedPriceItem

    Returns:
        {total, checked, updated, skipped, errors, changes: [{sku, item_id, field, old, new}]}
    """
    logger.info("Fetching active listings from eBay...")
    listings = client.get_active_listings()
    logger.info(f"Fetched {len(listings)} active listings")

    # Filter by SKU if specified
    if skus:
        sku_set = set(skus)
        listings = [l for l in listings if l['sku'] in sku_set]
        logger.info(f"Filtered to {len(listings)} listings matching requested SKUs")

    if sku_prefix:
        listings = [l for l in listings if l.get('sku', '').startswith(sku_prefix)]
        logger.info(f"Filtered to {len(listings)} listings matching prefix '{sku_prefix}'")

    # Skip listings without SKU
    listings_with_sku = [l for l in listings if l.get('sku')]
    skipped_no_sku = len(listings) - len(listings_with_sku)
    if skipped_no_sku:
        logger.warning(f"Skipped {skipped_no_sku} listings with no SKU")

    # Upload condition guide images to eBay EPS (once per invocation)
    if pictures:
        _ensure_eps_guide_urls(client)

    # For description or pictures sync, we need full item details via GetItem
    # (GetMyeBaySelling doesn't include Description or PictureDetails).
    if description or pictures:
        _enrich_details(client, listings_with_sku, workers,
                        need_description=description, need_pictures=pictures)

    # Compute changes for each listing
    changes = []
    for listing in listings_with_sku:
        item_changes = _compute_changes(listing, title, description, specifics, pictures)
        changes.extend(item_changes)

    items_to_update = {}  # item_id → {title, description, item_specifics}
    for change in changes:
        item_id = change['item_id']
        if item_id not in items_to_update:
            items_to_update[item_id] = {'item_id': item_id, 'sku': change['sku']}
        entry = items_to_update[item_id]

        if change['field'] == 'title':
            entry['title'] = change['new']
        elif change['field'] == 'description':
            entry['description'] = change['new']
        elif change['field'] == 'item_specifics':
            entry['item_specifics'] = change['new']
        elif change['field'] == 'pictures':
            entry['picture_urls'] = change['new']

    result = {
        'total': len(listings),
        'checked': len(listings_with_sku),
        'updated': 0,
        'skipped': len(listings_with_sku) - len(items_to_update),
        'errors': 0,
        'changes': changes,
        'error_details': [],
    }

    if dry_run:
        logger.info(f"[DRY RUN] {len(changes)} changes across {len(items_to_update)} listings")
        result['updated'] = len(items_to_update)
        return result

    if not items_to_update:
        logger.info("No changes needed — all listings are up to date")
        return result

    # Push updates concurrently
    logger.info(f"Pushing updates to {len(items_to_update)} listings...")
    update_results = _push_updates(client, list(items_to_update.values()), workers)

    for ur in update_results:
        if ur['ack'] in ('Success', 'Warning'):
            result['updated'] += 1
        else:
            result['errors'] += 1
            result['error_details'].append({
                'item_id': ur['item_id'],
                'errors': ur.get('errors', []),
            })

    logger.info(
        f"Done: {result['updated']} updated, {result['errors']} errors, "
        f"{result['skipped']} unchanged"
    )
    return result


def _ensure_eps_guide_urls(client):
    """Upload condition guide images to eBay EPS. Cached per invocation."""
    global _eps_guide_urls
    if _eps_guide_urls is not None:
        return

    logger.info(f"Uploading {len(CONDITION_GUIDE_S3_URLS)} condition guide images to eBay EPS...")
    _eps_guide_urls = []
    for s3_url in CONDITION_GUIDE_S3_URLS:
        try:
            eps_url = client.upload_picture(s3_url)
            _eps_guide_urls.append(eps_url)
            logger.info(f"  Uploaded: {s3_url.split('/')[-1]} -> {eps_url}")
        except Exception as e:
            logger.error(f"  Failed to upload {s3_url}: {e}")
            raise


def _enrich_details(client, listings, workers,
                    need_description=False, need_pictures=False):
    """Fetch full details (description, picture URLs) via GetItem."""
    needs_fetch = [
        l for l in listings
        if (need_description and not l.get('description'))
        or (need_pictures and 'picture_urls' not in l)
    ]
    if not needs_fetch:
        return

    logger.info(f"Fetching full details for {len(needs_fetch)} listings...")

    def fetch_one(listing):
        try:
            detail = client.get_item(listing['item_id'])
            if detail:
                if need_description and not listing.get('description'):
                    listing['description'] = detail.get('description', '')
                if not listing.get('item_specifics') and detail.get('item_specifics'):
                    listing['item_specifics'] = detail['item_specifics']
                if need_pictures:
                    listing['picture_urls'] = detail.get('picture_urls', [])
        except Exception as e:
            logger.warning(f"Failed to fetch details for {listing['item_id']}: {e}")

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
        list(executor.map(fetch_one, needs_fetch))


def _compute_changes(listing, do_title, do_description, do_specifics, do_pictures=False):
    """Compute changes needed for a single listing."""
    changes = []
    sku = listing['sku']
    item_id = listing['item_id']

    if do_title:
        current_title = listing.get('title', '')
        new_title = normalize_title(current_title, sku)
        if new_title != current_title:
            changes.append({
                'sku': sku,
                'item_id': item_id,
                'field': 'title',
                'old': current_title,
                'new': new_title,
            })

    if do_specifics:
        current_specifics = listing.get('item_specifics', {})
        new_specifics = build_item_specifics(sku, current_specifics)
        if new_specifics:
            changes.append({
                'sku': sku,
                'item_id': item_id,
                'field': 'item_specifics',
                'old': {k: current_specifics.get(k) for k in new_specifics},
                'new': new_specifics,
            })

    if do_description:
        current_desc = listing.get('description', '')
        # Use item specifics (merged: existing + new from SKU) for description
        merged_specifics = dict(listing.get('item_specifics', {}))
        merged_specifics.update(build_item_specifics(sku, listing.get('item_specifics', {})))
        new_desc = generate_description(sku, listing.get('title', ''), merged_specifics)
        if new_desc.strip() != current_desc.strip():
            changes.append({
                'sku': sku,
                'item_id': item_id,
                'field': 'description',
                'old': current_desc[:100] + '...' if len(current_desc) > 100 else current_desc,
                'new': new_desc,
            })

    if do_pictures and _eps_guide_urls:
        current_urls = listing.get('picture_urls', [])
        # Check if condition guide images are already present
        # (detect by checking if any current URL contains 'condition-guide' in the filename)
        has_guide = any('condition' in u.lower() for u in current_urls)
        if not has_guide:
            # Keep existing card photos, append EPS-hosted condition guide at the end
            new_urls = current_urls + _eps_guide_urls
            changes.append({
                'sku': sku,
                'item_id': item_id,
                'field': 'pictures',
                'old': f'{len(current_urls)} images',
                'new': new_urls,
            })

    return changes


def _push_updates(client, updates, workers):
    """Push ReviseFixedPriceItem updates concurrently."""
    results = []

    def push_one(update):
        try:
            return client.revise_item(
                item_id=update['item_id'],
                title=update.get('title'),
                description=update.get('description'),
                item_specifics=update.get('item_specifics'),
                picture_urls=update.get('picture_urls'),
            )
        except Exception as e:
            return {
                'ack': 'Failure',
                'item_id': update['item_id'],
                'errors': [str(e)],
            }

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
        results = list(executor.map(push_one, updates))

    return results
