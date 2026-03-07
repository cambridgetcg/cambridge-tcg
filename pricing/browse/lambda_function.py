"""
eBay Browse API — Competitor Price Monitor Lambda

Reads our SKUs, cost_gbp, and eBay selling prices from RDS, searches the
Browse API for competitor listings, classifies price ratios against our
Japanese acquisition cost (cost_gbp), stores results, and sends SNS alerts
for acquisition targets.

Classification baseline: cost_gbp (what we pay to source from Japan).
  ratio = competitor_price / cost_gbp
  ACQUISITION:  ratio < 0.50  — below half our cost, strong buy signal
  UNDERPRICED:  ratio < 0.70  — below our cost, worth watching
  COMPETITIVE:  ratio ≤ 1.00  — at or near our cost
  ABOVE:        ratio > 1.00  — above our cost

Architecture:
    - Python 3.12, arm64, VPC-connected (reads from RDS via Proxy)
    - Application-level OAuth (client_credentials — no user token)
    - Sequential Browse API calls (~400 × 0.5s ≈ 3.5 min)
    - Results stored in browse_price_monitor table
    - Acquisition alerts via existing SNS topic

Environment Variables:
    - PROXY_ENDPOINT: RDS Proxy endpoint
    - DB_USER: Database username
    - DB_PASSWORD: Database password
    - DATABASE_NAME: Database name (default: op_cardrush_link)
    - TABLE_NAME: Table name (default: cardrush_link)
    - EBAY_SECRET_NAME: Secrets Manager key (default: ebay-trading-api-credentials)
    - EBAY_OUR_SELLER_ID: Our eBay seller username (to exclude from results)
    - SNS_TOPIC_ARN: SNS topic for acquisition alerts (optional)
    - ACQUISITION_THRESHOLD: Ratio below which to alert (default: 0.50)
    - UNDERPRICED_THRESHOLD: Ratio for underpriced classification (default: 0.70)
"""

import os
import json
import re
import traceback
from datetime import datetime

import psycopg2
from psycopg2.extras import RealDictCursor
import boto3

from browse_client import BrowseClient
from monitoring.metrics import record_pipeline_run


def _safe_table_name(name):
    """Validate table name to prevent SQL injection."""
    if not re.match(r'^[a-zA-Z_][a-zA-Z0-9_]*$', name):
        raise ValueError(f"Invalid table name: {name}")
    return name


def get_db_connection():
    """Connect to database through RDS Proxy."""
    return psycopg2.connect(
        host=os.environ['PROXY_ENDPOINT'],
        database=os.environ.get('DATABASE_NAME', 'op_cardrush_link'),
        user=os.environ['DB_USER'],
        password=os.environ['DB_PASSWORD'],
        port=int(os.environ.get('DB_PORT', 5432)),
        cursor_factory=RealDictCursor,
        connect_timeout=10
    )


def parse_sku(sku):
    """
    Parse a SKU into its components for Browse API search.

    SKU formats:
        OP-OP01-062-JP  → game=OP, set_code=OP01, card_number=062, language=Japanese
        PKMN-SV6-045-JP → game=PKMN, set_code=SV6, card_number=045, language=Japanese
        OP-EB01-035-JP  → game=OP, set_code=EB01, card_number=035, language=Japanese

    Returns dict with keys: game, set_code, card_number, language
    Returns None if SKU cannot be parsed.
    """
    if not sku:
        return None

    parts = sku.split('-')
    if len(parts) < 4:
        return None

    game = parts[0]
    set_code = parts[1]
    card_number = parts[2]
    lang_code = parts[3]

    if game not in ('OP', 'PKMN'):
        return None

    language = 'Japanese' if lang_code == 'JP' else lang_code

    return {
        'game': game,
        'set_code': set_code,
        'card_number': card_number,
        'language': language,
    }


def classify_price_ratio(ratio):
    """
    Classify a competitor's price ratio against our acquisition cost.

    ratio = competitor_price / cost_gbp
    """
    acq_threshold = float(os.environ.get('ACQUISITION_THRESHOLD', '0.50'))
    under_threshold = float(os.environ.get('UNDERPRICED_THRESHOLD', '0.70'))

    if ratio < acq_threshold:
        return 'ACQUISITION'
    elif ratio < under_threshold:
        return 'UNDERPRICED'
    elif ratio <= 1.0:
        return 'COMPETITIVE'
    else:
        return 'ABOVE'


def store_results(connection, sku, cost_gbp, selling_price, competitors, classification_fn):
    """
    Store top-5 cheapest competitor results in browse_price_monitor.

    Ratio is computed against cost_gbp (our acquisition cost), not selling price.
    Returns the classification of the cheapest competitor (or None if no results).
    """
    if not competitors:
        return None

    top_5 = sorted(competitors, key=lambda c: c['price'])[:5]
    cheapest_class = None

    with connection.cursor() as cursor:
        for rank, comp in enumerate(top_5, start=1):
            ratio = comp['price'] / cost_gbp if cost_gbp > 0 else None
            classification = classification_fn(ratio) if ratio is not None else 'ABOVE'

            if rank == 1:
                cheapest_class = classification

            cursor.execute(
                """INSERT INTO browse_price_monitor
                   (sku, cost_gbp, selling_price, competitor_price,
                    competitor_seller, competitor_item_id, competitor_url,
                    competitor_title, price_ratio, classification, rank)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                (sku, cost_gbp, selling_price, comp['price'],
                 comp.get('seller', ''),
                 comp.get('item_id', ''), comp.get('url', ''),
                 comp.get('title', '')[:256] if comp.get('title') else '',
                 ratio, classification, rank)
            )
    connection.commit()
    return cheapest_class


def send_acquisition_alert(sns_client, topic_arn, acquisitions):
    """Send SNS email alert for acquisition targets."""
    if not acquisitions or not topic_arn:
        return

    lines = [
        "eBay Acquisition Opportunities Found",
        "=" * 40,
        f"Date: {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        f"Targets: {len(acquisitions)}",
        "",
    ]

    for acq in acquisitions:
        lines.append(f"SKU: {acq['sku']}")
        lines.append(f"  Our cost:    £{acq['cost_gbp']:.2f}")
        lines.append(f"  We sell at:  £{acq['selling_price']:.2f}")
        lines.append(f"  They sell:   £{acq['competitor_price']:.2f}")
        lines.append(f"  vs cost:     {acq['ratio']:.0%}")
        lines.append(f"  Seller: {acq['seller']}")
        lines.append(f"  URL: {acq['url']}")
        lines.append("")

    message = '\n'.join(lines)

    try:
        sns_client.publish(
            TopicArn=topic_arn,
            Subject=f"eBay Acquisitions: {len(acquisitions)} targets found",
            Message=message,
        )
        print(f"Sent acquisition alert for {len(acquisitions)} targets")
    except Exception as e:
        print(f"Failed to send SNS alert: {e}")


def lambda_handler(event, context):
    """
    Main handler: scan eBay for competitor prices.

    Event parameters (optional):
    - dry_run: If true, search but don't store results
    - skus: List of specific SKUs to scan (default: all with eBay prices)
    - max_skus: Maximum number of SKUs to process (for testing)
    """
    print("=" * 60)
    print("eBay Browse Monitor Lambda")
    print(f"Started: {datetime.now()}")
    print("=" * 60)

    connection = None

    try:
        table_name = _safe_table_name(os.environ.get('TABLE_NAME', 'cardrush_link'))
        dry_run = event.get('dry_run', False)
        sku_filter = event.get('skus', [])
        max_skus = event.get('max_skus', 0)
        topic_arn = os.environ.get('SNS_TOPIC_ARN', '')

        print(f"Table: {table_name}")
        print(f"Dry run: {dry_run}")
        print(f"SKU filter: {len(sku_filter)} SKUs" if sku_filter else "SKU filter: all")
        print(f"Max SKUs: {max_skus or 'unlimited'}")

        # Connect to RDS
        print("\nConnecting to database...")
        connection = get_db_connection()
        print("Connected to database")

        # Read SKUs + cost_gbp + selling prices
        with connection.cursor() as cursor:
            query = f"""
                SELECT sku, cost_gbp, ebay_business_selling_price
                FROM {table_name}
                WHERE ebay_business_selling_price IS NOT NULL
                  AND ebay_business_selling_price > 0
                  AND cost_gbp IS NOT NULL
                  AND cost_gbp > 0
            """
            if sku_filter:
                placeholders = ','.join(['%s'] * len(sku_filter))
                query += f" AND sku IN ({placeholders})"
                cursor.execute(query + " ORDER BY sku", sku_filter)
            else:
                cursor.execute(query + " ORDER BY sku")

            rows = cursor.fetchall()

        if max_skus:
            rows = rows[:max_skus]

        print(f"Found {len(rows)} SKUs to scan")

        if not rows:
            record_pipeline_run(connection, 'browse-monitor', 'success', 0, 'No SKUs to scan')
            return {
                'statusCode': 200,
                'body': json.dumps({'success': True, 'scanned': 0, 'message': 'No SKUs to scan'})
            }

        # Initialize Browse API client
        client = BrowseClient()
        acquisitions = []
        scanned = 0
        skipped = 0
        errors = 0
        classifications = {'ACQUISITION': 0, 'UNDERPRICED': 0, 'COMPETITIVE': 0, 'ABOVE': 0}

        for row in rows:
            sku = row['sku']
            cost_gbp = float(row['cost_gbp'])
            selling_price = float(row['ebay_business_selling_price'])

            parsed = parse_sku(sku)
            if not parsed:
                print(f"  Skipping {sku}: unparseable SKU")
                skipped += 1
                continue

            try:
                competitors = client.search_competitors(
                    game=parsed['game'],
                    set_code=parsed['set_code'],
                    card_number=parsed['card_number'],
                    language=parsed['language'],
                    our_price=selling_price,
                )
            except Exception as e:
                print(f"  Error searching {sku}: {e}")
                errors += 1
                continue

            scanned += 1

            if not competitors:
                print(f"  {sku}: no competitors found")
                continue

            cheapest = competitors[0]
            ratio = cheapest['price'] / cost_gbp if cost_gbp > 0 else None

            if not dry_run:
                classification = store_results(
                    connection, sku, cost_gbp, selling_price,
                    competitors, classify_price_ratio
                )
            else:
                classification = classify_price_ratio(ratio) if ratio else 'ABOVE'

            if classification:
                classifications[classification] = classifications.get(classification, 0) + 1

            if classification == 'ACQUISITION':
                acquisitions.append({
                    'sku': sku,
                    'cost_gbp': cost_gbp,
                    'selling_price': selling_price,
                    'competitor_price': cheapest['price'],
                    'ratio': ratio,
                    'seller': cheapest.get('seller', ''),
                    'url': cheapest.get('url', ''),
                })

            # Progress log every 50 SKUs
            if scanned % 50 == 0:
                print(f"  Progress: {scanned}/{len(rows)} scanned")

        # Send acquisition alerts
        if acquisitions and not dry_run:
            sns_client = boto3.client('sns')
            send_acquisition_alert(sns_client, topic_arn, acquisitions)

        # Summary
        print(f"\n{'=' * 60}")
        print("COMPLETED")
        print(f"Scanned: {scanned}, Skipped: {skipped}, Errors: {errors}")
        print(f"Classifications: {json.dumps(classifications)}")
        print(f"Acquisitions: {len(acquisitions)}")
        print(f"{'=' * 60}")

        status = 'success' if errors == 0 else 'partial'
        record_pipeline_run(connection, 'browse-monitor', status, scanned)

        return {
            'statusCode': 200,
            'body': json.dumps({
                'success': True,
                'dry_run': dry_run,
                'scanned': scanned,
                'skipped': skipped,
                'errors': errors,
                'classifications': classifications,
                'acquisitions': len(acquisitions),
                'timestamp': datetime.now().isoformat(),
            })
        }

    except Exception as e:
        print(f"Error: {e}")
        traceback.print_exc()

        if connection:
            record_pipeline_run(connection, 'browse-monitor', 'failure', 0, str(e))

        return {
            'statusCode': 500,
            'body': json.dumps({
                'success': False,
                'error': str(e),
            })
        }

    finally:
        if connection:
            connection.close()
            print("Database connection closed")
