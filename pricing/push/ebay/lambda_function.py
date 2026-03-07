"""
eBay Price Push Lambda

Reads selling prices from RDS (cardrush_link table), pushes price updates
to eBay using the Trading API ReviseInventoryStatus call (batch of 4 items).

Architecture:
    - Python 3.12, arm64, VPC-connected (reads from RDS via Proxy)
    - OAuth 2.0 User Access Token (Secrets Manager, ~1.9yr refresh token)
    - ReviseInventoryStatus for batch price updates (4 items per call)
    - Falls back to ReviseFixedPriceItem for single-item updates on error
    - ThreadPoolExecutor for concurrent API calls (5 workers)
    - RateLimiter: 5000 calls / 15 sec (conservative under eBay's 6000 limit)

Environment Variables:
    - PROXY_ENDPOINT: RDS Proxy endpoint
    - DB_USER: Database username
    - DB_PASSWORD: Database password
    - DATABASE_NAME: Database name (default: op_cardrush_link)
    - TABLE_NAME: Table name (default: cardrush_link)
    - EBAY_SECRET_NAME: Secrets Manager key (default: ebay-trading-api-credentials)
    - EBAY_SITE_ID: eBay site ID (default: 3 for UK)
"""

import os
import json
import xml.etree.ElementTree as ET
import concurrent.futures
import threading
import time
import psycopg2
from psycopg2.extras import RealDictCursor
from datetime import datetime

import requests
from ebay_auth import get_credentials, get_access_token
import re
from monitoring.metrics import record_pipeline_run


def _safe_table_name(name):
    """Validate table name to prevent SQL injection."""
    if not re.match(r'^[a-zA-Z_][a-zA-Z0-9_]*$', name):
        raise ValueError(f"Invalid table name: {name}")
    return name


# eBay Trading API endpoints
TRADING_API_ENDPOINTS = {
    'PRODUCTION': 'https://api.ebay.com/ws/api.dll',
    'SANDBOX': 'https://api.sandbox.ebay.com/ws/api.dll',
}

# Items per ReviseInventoryStatus call (eBay max is 4)
BATCH_SIZE = 4


class RateLimiter:
    """Thread-safe rate limiter. Reuses pattern from api-shopify."""

    def __init__(self, max_calls, period):
        self.max_calls = max_calls
        self.period = period
        self.lock = threading.Lock()
        self.calls = []

    def wait(self):
        while True:
            with self.lock:
                now = time.time()
                while self.calls and now - self.calls[0] > self.period:
                    self.calls.pop(0)
                if len(self.calls) < self.max_calls:
                    self.calls.append(time.time())
                    return
                sleep_time = self.period - (now - self.calls[0])
            if sleep_time > 0:
                time.sleep(sleep_time)


def get_db_connection():
    """Connect to database through RDS Proxy"""
    return psycopg2.connect(
        host=os.environ['PROXY_ENDPOINT'],
        database=os.environ.get('DATABASE_NAME', 'op_cardrush_link'),
        user=os.environ['DB_USER'],
        password=os.environ['DB_PASSWORD'],
        port=int(os.environ.get('DB_PORT', 5432)),
        cursor_factory=RealDictCursor,
        connect_timeout=10
    )


def build_revise_inventory_status_xml(items, credentials):
    """
    Build XML for ReviseInventoryStatus call.

    items: list of dicts with keys: item_id, price
    Max 4 items per call.

    Returns XML string.
    """
    root = ET.Element('ReviseInventoryStatusRequest')
    root.set('xmlns', 'urn:ebay:apis:eBLBaseComponents')

    # RequesterCredentials
    creds_elem = ET.SubElement(root, 'RequesterCredentials')
    token_elem = ET.SubElement(creds_elem, 'eBayAuthToken')
    token_elem.text = get_access_token(credentials)

    for item in items:
        inv_status = ET.SubElement(root, 'InventoryStatus')
        item_id_elem = ET.SubElement(inv_status, 'ItemID')
        item_id_elem.text = str(item['item_id'])
        price_elem = ET.SubElement(inv_status, 'StartPrice')
        price_elem.text = f"{item['price']:.2f}"

    return ET.tostring(root, encoding='unicode', xml_declaration=True)


def build_revise_fixed_price_xml(item_id, price, credentials):
    """
    Build XML for ReviseFixedPriceItem (single-item fallback).

    Returns XML string.
    """
    root = ET.Element('ReviseFixedPriceItemRequest')
    root.set('xmlns', 'urn:ebay:apis:eBLBaseComponents')

    creds_elem = ET.SubElement(root, 'RequesterCredentials')
    token_elem = ET.SubElement(creds_elem, 'eBayAuthToken')
    token_elem.text = get_access_token(credentials)

    item_elem = ET.SubElement(root, 'Item')
    item_id_elem = ET.SubElement(item_elem, 'ItemID')
    item_id_elem.text = str(item_id)
    price_elem = ET.SubElement(item_elem, 'StartPrice')
    price_elem.text = f"{price:.2f}"

    return ET.tostring(root, encoding='unicode', xml_declaration=True)


def get_trading_api_headers(call_name, credentials, site_id='3'):
    """Build eBay Trading API HTTP headers."""
    return {
        'X-EBAY-API-SITEID': site_id,
        'X-EBAY-API-COMPATIBILITY-LEVEL': '1349',
        'X-EBAY-API-CALL-NAME': call_name,
        'X-EBAY-API-APP-NAME': credentials['app_id'],
        'X-EBAY-API-DEV-NAME': credentials['dev_id'],
        'X-EBAY-API-CERT-NAME': credentials['cert_id'],
        'Content-Type': 'text/xml',
    }


def parse_inventory_status_response(xml_text):
    """
    Parse ReviseInventoryStatus response.

    Returns list of dicts: [{item_id, ack, errors}]
    """
    ns = {'e': 'urn:ebay:apis:eBLBaseComponents'}
    root = ET.fromstring(xml_text)

    ack = root.findtext('e:Ack', default='Failure', namespaces=ns)
    results = []

    # Parse per-item results from InventoryStatus elements
    for inv_status in root.findall('e:InventoryStatus', ns):
        item_id = inv_status.findtext('e:ItemID', namespaces=ns)
        start_price = inv_status.findtext('e:StartPrice', namespaces=ns)
        results.append({
            'item_id': item_id,
            'new_price': start_price,
            'ack': ack,
        })

    # Parse errors
    errors = []
    for error in root.findall('e:Errors', ns):
        severity = error.findtext('e:SeverityCode', namespaces=ns)
        code = error.findtext('e:ErrorCode', namespaces=ns)
        msg = error.findtext('e:LongMessage', namespaces=ns) or error.findtext('e:ShortMessage', namespaces=ns)
        errors.append({'severity': severity, 'code': code, 'message': msg})

    return ack, results, errors


def process_batch(batch_items, credentials, endpoint, site_id, rate_limiter, session):
    """
    Process a batch of up to 4 items via ReviseInventoryStatus.

    batch_items: list of dicts with keys: sku, item_id, price

    Returns dict with results per item.
    """
    rate_limiter.wait()

    items_payload = [{'item_id': item['item_id'], 'price': item['price']} for item in batch_items]
    xml_body = build_revise_inventory_status_xml(items_payload, credentials)
    headers = get_trading_api_headers('ReviseInventoryStatus', credentials, site_id)

    try:
        response = session.post(endpoint, headers=headers, data=xml_body, timeout=30)
    except Exception as e:
        return [{
            'sku': item['sku'],
            'item_id': item['item_id'],
            'status': 'error',
            'error': f"HTTP exception: {e}"
        } for item in batch_items]

    if response.status_code != 200:
        return [{
            'sku': item['sku'],
            'item_id': item['item_id'],
            'status': 'error',
            'error': f"HTTP {response.status_code}: {response.text[:200]}"
        } for item in batch_items]

    ack, results, errors = parse_inventory_status_response(response.text)

    batch_results = []

    if ack == 'Success' or ack == 'Warning':
        for item in batch_items:
            matched = next((r for r in results if r['item_id'] == str(item['item_id'])), None)
            batch_results.append({
                'sku': item['sku'],
                'item_id': item['item_id'],
                'status': 'success',
                'new_price': matched['new_price'] if matched else str(item['price']),
            })
    else:
        # Batch failed — try individual fallback for each item
        error_msg = '; '.join(f"[{e['code']}] {e['message']}" for e in errors)
        print(f"  Batch failed ({ack}): {error_msg}")
        print(f"  Falling back to ReviseFixedPriceItem for {len(batch_items)} items")

        for item in batch_items:
            result = revise_single_item(
                item, credentials, endpoint, site_id, rate_limiter, session
            )
            batch_results.append(result)

    return batch_results


def revise_single_item(item, credentials, endpoint, site_id, rate_limiter, session):
    """Fallback: update a single item via ReviseFixedPriceItem."""
    rate_limiter.wait()

    xml_body = build_revise_fixed_price_xml(item['item_id'], item['price'], credentials)
    headers = get_trading_api_headers('ReviseFixedPriceItem', credentials, site_id)

    try:
        response = session.post(endpoint, headers=headers, data=xml_body, timeout=30)
    except Exception as e:
        return {
            'sku': item['sku'],
            'item_id': item['item_id'],
            'status': 'error',
            'error': f"HTTP exception: {e}"
        }

    if response.status_code != 200:
        return {
            'sku': item['sku'],
            'item_id': item['item_id'],
            'status': 'error',
            'error': f"HTTP {response.status_code}"
        }

    ns = {'e': 'urn:ebay:apis:eBLBaseComponents'}
    root = ET.fromstring(response.text)
    ack = root.findtext('e:Ack', default='Failure', namespaces=ns)

    if ack in ('Success', 'Warning'):
        return {
            'sku': item['sku'],
            'item_id': item['item_id'],
            'status': 'success',
            'new_price': f"{item['price']:.2f}",
        }
    else:
        errors = []
        for error in root.findall('e:Errors', ns):
            msg = error.findtext('e:LongMessage', namespaces=ns) or error.findtext('e:ShortMessage', namespaces=ns)
            errors.append(msg)
        return {
            'sku': item['sku'],
            'item_id': item['item_id'],
            'status': 'error',
            'error': '; '.join(errors)
        }


def push_channel(channel_name, price_column, item_number_column, connection,
                 table_name, credentials, endpoint, site_id, rate_limiter):
    """
    Push prices for a single eBay channel.

    Returns dict with summary.
    """
    print(f"\n{'=' * 40}")
    print(f"Channel: {channel_name}")
    print(f"{'=' * 40}")

    with connection.cursor() as cursor:
        cursor.execute(f"""
            SELECT sku, {price_column} as price, {item_number_column} as item_id
            FROM {table_name}
            WHERE {price_column} IS NOT NULL
              AND {item_number_column} IS NOT NULL
              AND {price_column} > 0
            ORDER BY sku
        """)
        rows = cursor.fetchall()

    print(f"Found {len(rows)} items to update")

    if not rows:
        return {
            'channel': channel_name,
            'total': 0,
            'success': 0,
            'errors': 0,
            'error_details': []
        }

    # Build item list
    items = [{
        'sku': row['sku'],
        'item_id': str(row['item_id']),
        'price': float(row['price'])
    } for row in rows]

    # Split into batches of 4
    batches = [items[i:i + BATCH_SIZE] for i in range(0, len(items), BATCH_SIZE)]
    print(f"Split into {len(batches)} batches of up to {BATCH_SIZE}")

    all_results = []
    session = requests.Session()

    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        futures = {
            executor.submit(
                process_batch, batch, credentials, endpoint, site_id, rate_limiter, session
            ): idx
            for idx, batch in enumerate(batches)
        }

        for future in concurrent.futures.as_completed(futures):
            batch_idx = futures[future]
            try:
                batch_results = future.result()
                all_results.extend(batch_results)
            except Exception as e:
                print(f"  Batch {batch_idx} exception: {e}")
                # Mark all items in this batch as errored
                batch = batches[batch_idx]
                for item in batch:
                    all_results.append({
                        'sku': item['sku'],
                        'item_id': item['item_id'],
                        'status': 'error',
                        'error': f"Executor exception: {e}"
                    })

    success_count = sum(1 for r in all_results if r['status'] == 'success')
    error_count = sum(1 for r in all_results if r['status'] == 'error')
    error_details = [r for r in all_results if r['status'] == 'error']

    print(f"Results: {success_count} success, {error_count} errors out of {len(items)} items")

    if error_details:
        print(f"Error details (first 10):")
        for err in error_details[:10]:
            print(f"  SKU {err['sku']} (Item {err['item_id']}): {err.get('error', 'unknown')}")

    return {
        'channel': channel_name,
        'total': len(items),
        'success': success_count,
        'errors': error_count,
        'error_details': error_details[:50]  # Cap error details in response
    }


def lambda_handler(event, context):
    """
    Main handler: push eBay prices from RDS to eBay Trading API.

    Event parameters (optional):
    - channels: List of channels to push. Default: ["ebay_business"]
    - dry_run: If true, query RDS but don't call eBay API
    - site_id: eBay site ID override (default: 3 for UK)
    """
    print("=" * 60)
    print("eBay Price Push Lambda")
    print(f"Started: {datetime.now()}")
    print("=" * 60)

    connection = None

    try:
        # Configuration
        table_name = _safe_table_name(os.environ.get('TABLE_NAME', 'cardrush_link'))
        site_id = event.get('site_id', os.environ.get('EBAY_SITE_ID', '3'))
        dry_run = event.get('dry_run', False)
        channels = event.get('channels', ['ebay_business'])

        # Channel config: maps channel name -> (price_column, item_number_column)
        channel_config = {
            'ebay_business': ('ebay_business_selling_price', 'ebay_item_number_business'),
        }

        print(f"Table: {table_name}")
        print(f"Site ID: {site_id}")
        print(f"Channels: {channels}")
        print(f"Dry run: {dry_run}")

        # Get eBay credentials and access token
        print("\nRetrieving eBay credentials...")
        credentials = get_credentials()
        environment = credentials.get('environment', 'PRODUCTION')
        endpoint = TRADING_API_ENDPOINTS[environment]
        print(f"Environment: {environment}")

        # Refresh access token
        access_token = get_access_token(credentials)
        print(f"Access token obtained ({len(access_token)} chars)")

        # Rate limiter: 5000 calls per 15 seconds (conservative under eBay's 6000 limit)
        rate_limiter = RateLimiter(max_calls=5000, period=15)

        # Connect to RDS
        print("\nConnecting to database...")
        connection = get_db_connection()
        print("Connected to database")

        if dry_run:
            # Just report what would be pushed
            results = {}
            with connection.cursor() as cursor:
                for channel in channels:
                    if channel not in channel_config:
                        continue
                    price_col, item_col = channel_config[channel]
                    cursor.execute(f"""
                        SELECT COUNT(*) as count
                        FROM {table_name}
                        WHERE {price_col} IS NOT NULL
                          AND {item_col} IS NOT NULL
                          AND {price_col} > 0
                    """)
                    count = cursor.fetchone()['count']
                    results[channel] = {'total': count, 'status': 'dry_run'}
                    print(f"{channel}: {count} items would be pushed")

            return {
                'statusCode': 200,
                'body': json.dumps({
                    'success': True,
                    'dry_run': True,
                    'channels': results,
                    'timestamp': datetime.now().isoformat()
                })
            }

        # Push prices for each channel
        channel_results = {}
        for channel in channels:
            if channel not in channel_config:
                print(f"Unknown channel: {channel}, skipping")
                continue

            price_col, item_col = channel_config[channel]
            result = push_channel(
                channel_name=channel,
                price_column=price_col,
                item_number_column=item_col,
                connection=connection,
                table_name=table_name,
                credentials=credentials,
                endpoint=endpoint,
                site_id=site_id,
                rate_limiter=rate_limiter
            )
            channel_results[channel] = result

        # Summary
        total_success = sum(r['success'] for r in channel_results.values())
        total_errors = sum(r['errors'] for r in channel_results.values())
        total_items = sum(r['total'] for r in channel_results.values())

        print(f"\n{'=' * 60}")
        print("COMPLETED")
        print(f"Total: {total_items} items, {total_success} success, {total_errors} errors")
        print(f"{'=' * 60}")

        record_pipeline_run(
            connection, 'ebay',
            'success' if total_errors == 0 else 'partial',
            total_success
        )

        # Determine status code: 200=all ok, 207=partial, 500=total failure
        if total_errors == 0:
            status_code = 200
        elif total_success > 0:
            status_code = 207
        else:
            status_code = 500

        return {
            'statusCode': status_code,
            'body': json.dumps({
                'success': total_errors == 0,
                'channels': channel_results,
                'summary': {
                    'total_items': total_items,
                    'total_success': total_success,
                    'total_errors': total_errors,
                },
                'timestamp': datetime.now().isoformat()
            }, default=str)
        }

    except Exception as e:
        print(f"Error: {e}")
        import traceback
        traceback.print_exc()

        if connection:
            record_pipeline_run(connection, 'ebay', 'failure', 0, str(e))

        return {
            'statusCode': 500,
            'body': json.dumps({
                'success': False,
                'error': str(e)
            })
        }

    finally:
        if connection:
            connection.close()
            print("Database connection closed")


if __name__ == "__main__":
    os.environ['PROXY_ENDPOINT'] = 'your-proxy-endpoint'
    os.environ['DB_USER'] = 'your-username'
    os.environ['DB_PASSWORD'] = 'your-password'
    os.environ['DATABASE_NAME'] = 'op_cardrush_link'
    os.environ['TABLE_NAME'] = 'cardrush_link'
    os.environ['EBAY_SECRET_NAME'] = 'ebay-trading-api-credentials'
    os.environ['EBAY_SITE_ID'] = '3'

    result = lambda_handler({'dry_run': True}, {})
    print(json.dumps(json.loads(result['body']), indent=2))
