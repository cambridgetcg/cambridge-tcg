"""
Price Calculator Lambda

Step 1: Derives cost_gbp from price_yen / gbp_to_jpy.
Step 2: Derives landed_cost_gbp = cost_gbp * (1 + shipping_rate) + shipping_flat.
Step 2.5: Queries sales_events for per-SKU velocity → dynamic margin.
Step 3: Calculates channel selling prices from landed_cost_gbp via formula (per-SKU margin).
Step 4: Snapshots all prices into price_history table for trend tracking.

Cost model:
    cost_gbp = price_yen / gbp_to_jpy  (shelf price in GBP)
    landed_cost_gbp = cost_gbp * (1 + shipping_rate) + shipping_flat

Dynamic margin:
    velocity (units sold in window) → margin via linear interpolation
    0 sales → MARGIN_DEFAULT (unknown demand)
    VELOCITY_LOW → MARGIN_MIN (floor — need to compete)
    VELOCITY_HIGH → MARGIN_MAX (ceiling — demand is strong)
    To revert to flat margin: set MARGIN_MIN = MARGIN_MAX = MARGIN_DEFAULT

Formula: P = C * (1 + M) * (1 + V) / (1 - F * (1 + V))  where C = landed_cost_gbp
Final:   selling_price = ceil(P) + 0.80

Architecture:
    - Python 3.12, arm64, VPC-connected (reads/writes RDS via Proxy)
    - Self-contained: all cost data derived from RDS + env var rates
    - All prices end in .80

Environment Variables:
    - PROXY_ENDPOINT: RDS Proxy endpoint
    - DB_USER: Database username
    - DB_PASSWORD: Database password
    - DATABASE_NAME: Database name (default: op_cardrush_link)
    - TABLE_NAME: Table name (default: cardrush_link)
    - SHIPPING_RATE: Proportional shipping rate (default: 5, meaning 5%)
    - SHIPPING_FLAT_GBP: Flat shipping fee in GBP (default: 1.00)
    - MARGIN_MIN: Minimum margin percent for slow sellers (default: 12)
    - MARGIN_MAX: Maximum margin percent for fast sellers (default: 22)
    - MARGIN_DEFAULT: Margin for SKUs with no sales data (default: 18)
    - VELOCITY_WINDOW_DAYS: Days to look back for sales velocity (default: 30)
    - VELOCITY_LOW: Units sold threshold for minimum margin (default: 1)
    - VELOCITY_HIGH: Units sold threshold for maximum margin (default: 8)
    - VAT_RATE: VAT rate percent (default: 20)
    - EBAY_BUSINESS_FEE: eBay Business fee percent (default: 12)
    - CARDMARKET_FEE: Cardmarket fee percent (default: 8)
    - SHOPIFY_FEE: Shopify fee percent (default: 5)
"""

import os
import re
import json
import math
import psycopg2
from psycopg2.extras import RealDictCursor, execute_batch
from datetime import datetime
from monitoring.metrics import record_pipeline_run


def _safe_table_name(name):
    """Validate table name to prevent SQL injection."""
    if not re.match(r'^[a-zA-Z_][a-zA-Z0-9_]*$', name):
        raise ValueError(f"Invalid table name: {name}")
    return name


def velocity_to_margin(units_sold, low=1, high=8,
                       margin_min=0.12, margin_max=0.22, margin_default=0.18):
    """
    Map sales velocity to margin via linear interpolation.

    Returns:
        margin_default  if units_sold == 0  (no data — unknown demand)
        margin_min      if units_sold <= low (slow seller)
        margin_max      if units_sold >= high (fast seller)
        linear interp   if low < units_sold < high
    """
    if units_sold == 0:
        return margin_default
    if units_sold <= low:
        return margin_min
    if units_sold >= high:
        return margin_max
    # Linear interpolation between low and high
    t = (units_sold - low) / (high - low)
    return margin_min + t * (margin_max - margin_min)


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


def calculate_selling_price(cost_gbp, margin, fee, vat=0.20):
    """
    Calculate VAT-inclusive selling price for a given channel.

    P = C * (1 + M) * (1 + V) / (1 - F * (1 + V))
    selling_price = ceil(P) + 0.80

    Returns None for invalid inputs.
    """
    if cost_gbp is None or cost_gbp <= 0:
        return None

    denominator = 1 - fee * (1 + vat)
    if denominator <= 0:
        return None

    p = cost_gbp * (1 + margin) * (1 + vat) / denominator
    return math.ceil(p) + 0.80


def lambda_handler(event, context):
    """
    Step 1: Derive cost_gbp = price_yen / gbp_to_jpy
    Step 2: Derive landed_cost_gbp = cost_gbp * (1 + shipping_rate) + shipping_flat
    Step 3: Calculate 3 channel selling prices from landed_cost_gbp
    Step 4: Snapshot all prices into price_history table
    """
    print("=" * 60)
    print("Price Calculator")
    print(f"Started: {datetime.now()}")
    print("=" * 60)

    connection = None

    try:
        table_name = _safe_table_name(os.environ.get('TABLE_NAME', 'cardrush_link'))

        margin_min = float(os.environ.get('MARGIN_MIN', '12')) / 100
        margin_max = float(os.environ.get('MARGIN_MAX', '22')) / 100
        margin_default = float(os.environ.get('MARGIN_DEFAULT', '18')) / 100
        velocity_window = int(os.environ.get('VELOCITY_WINDOW_DAYS', '30'))
        velocity_low = int(os.environ.get('VELOCITY_LOW', '1'))
        velocity_high = int(os.environ.get('VELOCITY_HIGH', '8'))

        vat = float(os.environ.get('VAT_RATE', '20')) / 100
        ebay_biz_fee = float(os.environ.get('EBAY_BUSINESS_FEE', '12')) / 100
        cardmarket_fee = float(os.environ.get('CARDMARKET_FEE', '8')) / 100
        shopify_fee = float(os.environ.get('SHOPIFY_FEE', '5')) / 100

        shipping_rate = float(os.environ.get('SHIPPING_RATE', '5')) / 100
        shipping_flat = float(os.environ.get('SHIPPING_FLAT_GBP', '1.00'))

        print(f"Table: {table_name}")
        print(f"Margin: {margin_min*100:.0f}%–{margin_max*100:.0f}% "
              f"(default {margin_default*100:.0f}%), VAT: {vat*100:.0f}%")
        print(f"Velocity: window={velocity_window}d, "
              f"low={velocity_low}, high={velocity_high}")
        print(f"Fees: eBay Biz {ebay_biz_fee*100:.0f}%, "
              f"Cardmarket {cardmarket_fee*100:.0f}%, Shopify {shopify_fee*100:.0f}%")
        print(f"Shipping: {shipping_rate*100:.0f}% + £{shipping_flat:.2f}")

        print("\nConnecting to database...")
        connection = get_db_connection()
        print("Connected to database")

        with connection.cursor() as cursor:
            # Staleness guard: verify gbp_to_jpy exists before derivation
            cursor.execute(f"""
                SELECT COUNT(*) as count
                FROM {table_name}
                WHERE gbp_to_jpy IS NOT NULL AND gbp_to_jpy > 0
            """)
            fx_count = cursor.fetchone()['count']
            if fx_count == 0:
                raise Exception(
                    "No rows have a valid gbp_to_jpy rate. "
                    "Run cardrush-fx-updater before price-calculator."
                )
            print(f"FX rate present for {fx_count} rows")

            # Step 1: Derive cost_gbp from price_yen (tax-excluded shelf price)
            cursor.execute(f"""
                UPDATE {table_name}
                SET cost_gbp = price_yen / gbp_to_jpy
                WHERE price_yen IS NOT NULL AND price_yen > 0
                  AND gbp_to_jpy IS NOT NULL AND gbp_to_jpy > 0
            """)
            derivation_count = cursor.rowcount
            connection.commit()
            print(f"Derived cost_gbp for {derivation_count} rows")

            # Step 2: Derive landed_cost_gbp = cost_gbp * (1 + rate) + flat
            # Guard: skip EN cards (price_yen IS NULL) — their prices are set by OPTCG scraper
            cursor.execute(f"""
                UPDATE {table_name}
                SET shipping_fee_gbp = %s,
                    import_duty_rate = %s,
                    handling_fee_gbp = 0,
                    landed_cost_gbp = cost_gbp * (1 + %s) + %s
                WHERE cost_gbp IS NOT NULL AND cost_gbp > 0
                  AND price_yen IS NOT NULL
            """, (shipping_flat, shipping_rate, shipping_rate, shipping_flat))
            landed_count = cursor.rowcount
            connection.commit()
            print(f"Derived landed_cost_gbp for {landed_count} rows")

            # Step 2.5: Query sales velocity per SKU
            cursor.execute("""
                SELECT sku, SUM(quantity) as units_sold
                FROM sales_events
                WHERE event_type = 'sale'
                  AND created_at >= NOW() - INTERVAL '%s days'
                GROUP BY sku
            """, (velocity_window,))
            velocity_rows = cursor.fetchall()
            velocity_map = {r['sku']: int(r['units_sold']) for r in velocity_rows}
            print(f"Velocity data: {len(velocity_map)} SKUs with sales "
                  f"in last {velocity_window} days")

            # Step 3: Calculate selling prices from landed_cost_gbp
            # Guard: skip EN cards — their selling prices are set by OPTCG scraper
            cursor.execute(f"""
                SELECT sku, landed_cost_gbp
                FROM {table_name}
                WHERE landed_cost_gbp IS NOT NULL AND landed_cost_gbp > 0
                  AND price_yen IS NOT NULL
            """)
            rows = cursor.fetchall()

            updates = []
            margin_map = {}
            for row in rows:
                cost = float(row['landed_cost_gbp'])
                sku = row['sku']
                units = velocity_map.get(sku, 0)
                sku_margin = velocity_to_margin(
                    units, velocity_low, velocity_high,
                    margin_min, margin_max, margin_default
                )
                margin_map[sku] = sku_margin
                ebay_biz = calculate_selling_price(cost, sku_margin, ebay_biz_fee, vat)
                cardmarket_price = calculate_selling_price(cost, sku_margin, cardmarket_fee, vat)
                shopify_price = calculate_selling_price(cost, sku_margin, shopify_fee, vat)
                updates.append((ebay_biz, cardmarket_price, shopify_price, sku))

            # Margin distribution logging
            if margin_map:
                margins = list(margin_map.values())
                at_min = sum(1 for m in margins if m == margin_min)
                at_max = sum(1 for m in margins if m == margin_max)
                at_default = sum(1 for m in margins if m == margin_default)
                print(f"Margin distribution: min={min(margins)*100:.1f}%, "
                      f"max={max(margins)*100:.1f}%, avg={sum(margins)/len(margins)*100:.1f}%")
                print(f"  at floor ({margin_min*100:.0f}%): {at_min}, "
                      f"at ceiling ({margin_max*100:.0f}%): {at_max}, "
                      f"at default ({margin_default*100:.0f}%): {at_default}")

            print(f"Prices calculated for {len(updates)} SKUs")

            if updates:
                execute_batch(
                    cursor,
                    f"""UPDATE {table_name}
                        SET ebay_business_selling_price = %s,
                            cardmarket_selling_price = %s,
                            shopify_selling_price = %s
                        WHERE sku = %s""",
                    updates,
                    page_size=100
                )
                price_count = cursor.rowcount
                connection.commit()
                print(f"Updated selling prices for {price_count} rows")

            # Step 4: Record price history snapshot
            # Guard: skip EN cards — OPTCG scraper writes its own history
            cursor.execute(f"""
                SELECT sku, price_yen, cost_gbp, landed_cost_gbp, gbp_to_jpy,
                       shopify_selling_price, ebay_business_selling_price,
                       cardmarket_selling_price,
                       cardrush_stock, cardrush_stock_subgrade, price_yen_subgrade
                FROM {table_name}
                WHERE shopify_selling_price IS NOT NULL
                  AND price_yen IS NOT NULL
            """)
            history_rows = cursor.fetchall()

            if history_rows:
                execute_batch(
                    cursor,
                    """INSERT INTO price_history
                       (sku, price_yen, cost_gbp, landed_cost_gbp, gbp_to_jpy,
                        shopify_selling_price, ebay_selling_price,
                        cardmarket_selling_price, applied_margin,
                        cardrush_stock, cardrush_stock_subgrade, price_yen_subgrade)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                    [(r['sku'], r['price_yen'], r['cost_gbp'], r['landed_cost_gbp'],
                      r['gbp_to_jpy'], r['shopify_selling_price'],
                      r['ebay_business_selling_price'], r['cardmarket_selling_price'],
                      margin_map.get(r['sku']),
                      r['cardrush_stock'], r['cardrush_stock_subgrade'],
                      r['price_yen_subgrade'])
                     for r in history_rows],
                    page_size=100
                )
                connection.commit()
                print(f"Recorded price history for {len(history_rows)} SKUs")

            # Report rows still missing cost_gbp
            cursor.execute(f"""
                SELECT COUNT(*) as count
                FROM {table_name}
                WHERE cost_gbp IS NULL OR cost_gbp <= 0
            """)
            missing = cursor.fetchone()['count']
            if missing > 0:
                print(f"WARNING: {missing} rows still missing cost_gbp")

        record_pipeline_run(connection, 'calculator', 'success', len(updates))

        return {
            'statusCode': 200,
            'body': json.dumps({
                'success': True,
                'cost_gbp_derived': derivation_count,
                'landed_cost_derived': landed_count,
                'prices_calculated': len(updates),
                'timestamp': datetime.now().isoformat()
            })
        }

    except Exception as e:
        print(f"Error: {e}")
        import traceback
        traceback.print_exc()

        if connection:
            connection.rollback()
            record_pipeline_run(connection, 'calculator', 'failure', 0, str(e))

        return {
            'statusCode': 500,
            'body': json.dumps({'success': False, 'error': str(e)})
        }

    finally:
        if connection:
            connection.close()
            print("Database connection closed")
