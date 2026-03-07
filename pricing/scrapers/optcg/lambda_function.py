"""
OPTCG Scraper Lambda

Fetches English One Piece card prices from the OPTCG API (optcgapi.com),
converts USD to GBP, calculates selling prices, and writes to RDS.

Now includes parallel/alt-art variants as separate SKUs (P1/P2 suffix)
and populates the card_catalog table with all 17 metadata fields.

Steps:
    1. Fetch all booster + starter deck cards from OPTCG API
    2. Fetch USD→GBP FX rate from Amdoren API
    3. Map each card (including parallels) to a unique SKU
    4. Calculate selling prices (same formula as JP, but no import shipping)
    5. UPSERT card_catalog (all metadata)
    6. UPSERT into cardrush_link (pricing + card_image_id FK)
    7. INSERT snapshot into price_history

Cost model (EN — no import shipping/duty):
    cost_gbp = market_price_usd * usd_to_gbp
    landed_cost_gbp = cost_gbp  (no shipping surcharge for market-priced EN cards)
    P = C * (1 + M) * (1 + V) / (1 - F * (1 + V))
    selling_price = ceil(P) + 0.80

Environment Variables:
    PROXY_ENDPOINT, DB_USER, DB_PASSWORD, DATABASE_NAME, DB_PORT
    AMDOREN_API_KEY
"""

import os
import re
import json
import math
import requests
import psycopg2
from psycopg2.extras import RealDictCursor, execute_batch
from datetime import datetime
from monitoring.metrics import record_pipeline_run

# Pricing parameters — same as calculator Lambda
MARGIN = 0.22
VAT = 0.20
SHOPIFY_FEE = 0.05
EBAY_BIZ_FEE = 0.12
CARDMARKET_FEE = 0.08

OPTCG_API_BASE = 'https://www.optcgapi.com/api'


def _safe_table_name(name):
    """Validate table name to prevent SQL injection."""
    if not re.match(r'^[a-zA-Z_][a-zA-Z0-9_]*$', name):
        raise ValueError(f"Invalid table name: {name}")
    return name


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


def calculate_selling_price(cost_gbp, fee):
    """
    Calculate VAT-inclusive selling price for a given channel.

    P = C * (1 + M) * (1 + V) / (1 - F * (1 + V))
    selling_price = ceil(P) + 0.80
    """
    if cost_gbp is None or cost_gbp <= 0:
        return None

    denominator = 1 - fee * (1 + VAT)
    if denominator <= 0:
        return None

    p = cost_gbp * (1 + MARGIN) * (1 + VAT) / denominator
    return math.ceil(p) + 0.80


def fetch_optcg_cards():
    """Fetch all cards from OPTCG API (booster sets + starter decks)."""
    session = requests.Session()
    all_cards = []

    for endpoint in ['/allSetCards/', '/allSTCards/']:
        url = OPTCG_API_BASE + endpoint
        print(f"Fetching {url}...")
        resp = session.get(url, timeout=60)
        resp.raise_for_status()
        cards = resp.json()
        print(f"  Got {len(cards)} cards from {endpoint}")
        all_cards.extend(cards)

    return all_cards


def fetch_usd_to_gbp(api_key):
    """Fetch USD→GBP rate from Amdoren API."""
    url = 'https://www.amdoren.com/api/currency.php'
    params = {'api_key': api_key, 'from': 'USD', 'to': 'GBP'}
    resp = requests.get(url, params=params, timeout=30)
    resp.raise_for_status()
    data = resp.json()

    if data.get('error', 0) != 0:
        raise Exception(f"Amdoren API error: {data.get('error_message', 'unknown')}")

    rate = float(data['amount'])
    if rate <= 0 or rate > 2:
        raise Exception(f"USD→GBP rate {rate} outside sanity range (0, 2)")

    print(f"USD→GBP rate: {rate}")
    return rate


def map_sku(card):
    """
    Map OPTCG card to our SKU format, including parallel variant suffix.

    Base card:     "OP01-001" → "OP-OP01-001-EN"
    Parallel:      "OP01-001_p1" → "OP-OP01-001-EN-P1"
    """
    card_set_id = card['card_set_id']       # e.g. "OP01-001"
    card_image_id = card['card_image_id']   # e.g. "OP01-001_p1"

    sku = f"OP-{card_set_id}-EN"

    # Detect parallel variant suffix
    if '_p' in card_image_id:
        suffix = card_image_id.split('_p')[-1]  # "1", "2", etc.
        sku += f"-P{suffix}"

    return sku


def lambda_handler(event, context):
    print("=" * 60)
    print("OPTCG Scraper (English One Piece — with parallels)")
    print(f"Started: {datetime.now()}")
    print("=" * 60)

    dry_run = event.get('dry_run', False)
    connection = None

    try:
        table_name = _safe_table_name(os.environ.get('TABLE_NAME', 'cardrush_link'))
        amdoren_key = os.environ.get('AMDOREN_API_KEY')
        if not amdoren_key:
            raise Exception("AMDOREN_API_KEY not set")

        # Step 1: Fetch cards from OPTCG API
        raw_cards = fetch_optcg_cards()
        print(f"Total raw cards: {len(raw_cards)}")

        parallel_count = sum(1 for c in raw_cards if '_p' in (c.get('card_image_id') or ''))
        print(f"Parallel variants: {parallel_count}")

        # Step 2: Fetch FX rate
        usd_to_gbp = fetch_usd_to_gbp(amdoren_key)

        # Step 3: Calculate prices for ALL cards (base + parallels)
        priced_cards = []
        catalog_rows = []
        skipped_no_price = 0

        for card in raw_cards:
            card_image_id = card.get('card_image_id') or ''
            card_set_id = card.get('card_set_id') or ''

            if not card_image_id or not card_set_id:
                continue

            # Always build catalog row (even without price)
            catalog_rows.append({
                'card_image_id': card_image_id,
                'card_set_id': card_set_id,
                'set_id': card.get('set_id'),
                'card_name': card.get('card_name'),
                'rarity': card.get('rarity'),
                'card_color': card.get('card_color'),
                'card_type': card.get('card_type'),
                'card_text': card.get('card_text'),
                'card_cost': card.get('card_cost'),
                'card_power': card.get('card_power'),
                'card_counter': card.get('card_counter'),
                'card_life': card.get('card_life'),
                'card_attribute': card.get('card_attribute'),
                'card_trigger': card.get('card_trigger'),
                'market_price': float(card['market_price']) if card.get('market_price') else None,
                'inventory_price': float(card['inventory_price']) if card.get('inventory_price') else None,
                'date_scraped': card.get('date_scraped'),
            })

            market_price = card.get('market_price')
            if not market_price or float(market_price) <= 0:
                skipped_no_price += 1
                continue

            price_usd = float(market_price)
            sku = map_sku(card)
            cost_gbp = price_usd * usd_to_gbp

            # EN cards: no import shipping (market price, not wholesale)
            landed_cost_gbp = cost_gbp

            shopify_price = calculate_selling_price(landed_cost_gbp, SHOPIFY_FEE)
            ebay_price = calculate_selling_price(landed_cost_gbp, EBAY_BIZ_FEE)
            cardmarket_price = calculate_selling_price(landed_cost_gbp, CARDMARKET_FEE)

            priced_cards.append({
                'sku': sku,
                'card_image_id': card_image_id,
                'price_usd': price_usd,
                'usd_to_gbp': usd_to_gbp,
                'cost_gbp': round(cost_gbp, 4),
                'landed_cost_gbp': round(landed_cost_gbp, 4),
                'shopify_price': shopify_price,
                'ebay_price': ebay_price,
                'cardmarket_price': cardmarket_price,
            })

        print(f"Catalog rows: {len(catalog_rows)}")
        print(f"Priced cards: {len(priced_cards)} (skipped {skipped_no_price} with no price)")

        if dry_run:
            sample = priced_cards[:3] if priced_cards else []
            return {
                'statusCode': 200,
                'body': json.dumps({
                    'dry_run': True,
                    'total_cards': len(priced_cards),
                    'catalog_rows': len(catalog_rows),
                    'parallel_count': parallel_count,
                    'skipped_no_price': skipped_no_price,
                    'usd_to_gbp': usd_to_gbp,
                    'sample': sample,
                }, default=str)
            }

        if not priced_cards:
            raise Exception("No cards with valid prices found")

        # Step 4: Write to RDS
        print("\nConnecting to database...")
        connection = get_db_connection()
        print("Connected to database")

        with connection.cursor() as cursor:
            # Step 5: UPSERT card_catalog
            catalog_sql = """
                INSERT INTO card_catalog
                    (card_image_id, card_set_id, set_id, card_name, rarity,
                     card_color, card_type, card_text, card_cost, card_power,
                     card_counter, card_life, card_attribute, card_trigger,
                     market_price, inventory_price, date_scraped, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
                ON CONFLICT (card_image_id) DO UPDATE SET
                    card_set_id = EXCLUDED.card_set_id,
                    set_id = EXCLUDED.set_id,
                    card_name = EXCLUDED.card_name,
                    rarity = EXCLUDED.rarity,
                    card_color = EXCLUDED.card_color,
                    card_type = EXCLUDED.card_type,
                    card_text = EXCLUDED.card_text,
                    card_cost = EXCLUDED.card_cost,
                    card_power = EXCLUDED.card_power,
                    card_counter = EXCLUDED.card_counter,
                    card_life = EXCLUDED.card_life,
                    card_attribute = EXCLUDED.card_attribute,
                    card_trigger = EXCLUDED.card_trigger,
                    market_price = EXCLUDED.market_price,
                    inventory_price = EXCLUDED.inventory_price,
                    date_scraped = EXCLUDED.date_scraped,
                    updated_at = NOW()
            """

            catalog_data = [
                (c['card_image_id'], c['card_set_id'], c['set_id'],
                 c['card_name'], c['rarity'], c['card_color'], c['card_type'],
                 c['card_text'], c['card_cost'], c['card_power'],
                 c['card_counter'], c['card_life'], c['card_attribute'],
                 c['card_trigger'], c['market_price'], c['inventory_price'],
                 c['date_scraped'])
                for c in catalog_rows
            ]

            execute_batch(cursor, catalog_sql, catalog_data, page_size=100)
            connection.commit()
            print(f"UPSERT card_catalog: {len(catalog_data)} rows")

            # Step 6: UPSERT into cardrush_link (pricing + card_image_id FK)
            # card_number is PK (NOT NULL) — use sku as card_number for EN cards
            # to avoid collisions with JP card_numbers (e.g. "OP01-031")
            upsert_sql = f"""
                INSERT INTO {table_name}
                    (card_number, sku, card_image_id, price_usd, usd_to_gbp,
                     cost_gbp, landed_cost_gbp, shopify_selling_price,
                     ebay_business_selling_price, cardmarket_selling_price)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (sku) DO UPDATE SET
                    card_image_id = EXCLUDED.card_image_id,
                    price_usd = EXCLUDED.price_usd,
                    usd_to_gbp = EXCLUDED.usd_to_gbp,
                    cost_gbp = EXCLUDED.cost_gbp,
                    landed_cost_gbp = EXCLUDED.landed_cost_gbp,
                    shopify_selling_price = EXCLUDED.shopify_selling_price,
                    ebay_business_selling_price = EXCLUDED.ebay_business_selling_price,
                    cardmarket_selling_price = EXCLUDED.cardmarket_selling_price
            """

            upsert_data = [
                (c['sku'], c['sku'], c['card_image_id'], c['price_usd'],
                 c['usd_to_gbp'], c['cost_gbp'], c['landed_cost_gbp'],
                 c['shopify_price'], c['ebay_price'], c['cardmarket_price'])
                for c in priced_cards
            ]

            execute_batch(cursor, upsert_sql, upsert_data, page_size=100)
            upsert_count = cursor.rowcount
            connection.commit()
            print(f"UPSERT cardrush_link: {upsert_count} rows")

            # Step 7: INSERT into price_history
            history_sql = """
                INSERT INTO price_history
                    (sku, price_usd, cost_gbp, landed_cost_gbp,
                     shopify_selling_price, ebay_selling_price,
                     cardmarket_selling_price)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
            """

            history_data = [
                (c['sku'], c['price_usd'], c['cost_gbp'], c['landed_cost_gbp'],
                 c['shopify_price'], c['ebay_price'], c['cardmarket_price'])
                for c in priced_cards
            ]

            execute_batch(cursor, history_sql, history_data, page_size=100)
            connection.commit()
            print(f"Price history: {len(history_data)} rows inserted")

        record_pipeline_run(connection, 'optcg-scraper', 'success', len(priced_cards))

        return {
            'statusCode': 200,
            'body': json.dumps({
                'success': True,
                'total_cards': len(priced_cards),
                'catalog_rows': len(catalog_rows),
                'parallel_count': parallel_count,
                'skipped_no_price': skipped_no_price,
                'usd_to_gbp': usd_to_gbp,
                'upsert_count': upsert_count,
                'timestamp': datetime.now().isoformat(),
            })
        }

    except Exception as e:
        print(f"Error: {e}")
        import traceback
        traceback.print_exc()

        if connection:
            connection.rollback()
            record_pipeline_run(connection, 'optcg-scraper', 'failure', 0, str(e))

        return {
            'statusCode': 500,
            'body': json.dumps({'success': False, 'error': str(e)})
        }

    finally:
        if connection:
            connection.close()
            print("Database connection closed")
