"""Price History API Lambda

Serves historical price data and trade-in buy list from RDS.

Routes:
    GET  /prices?sku=X[&days=N]           — Price history for a single SKU
    GET  /skus                             — List all tracked SKUs
    GET  /catalog                          — Full catalog with prices and stock
    GET  /indices?days=N                   — Market indices (S&P 500-style)
    GET  /buylist                          — Trade-in buy list (One Piece only)
    POST /tradein                          — Submit trade-in request
    GET  /tradein/{reference}?email=X      — Check trade-in status
    POST /tradein/{reference}/status       — Admin: update trade-in status

Environment Variables:
    PROXY_ENDPOINT, DB_USER, DB_PASSWORD, DATABASE_NAME, DB_PORT
    SNS_TOPIC_ARN (optional — for trade-in notifications)
    ADMIN_SECRET (required for admin endpoints)
    SES_ENABLED, SES_SANDBOX_MODE (email delivery)
"""

import os
import re
import json
import math
import hmac
import psycopg2
from psycopg2.extras import RealDictCursor
from datetime import datetime, timedelta, timezone
from email_templates import confirmation_email, received_email, payment_email
from email_sender import send_email

# Pricing parameters — same defaults as calculator Lambda
MARGIN = 0.22
VAT = 0.20
SHOPIFY_FEE = 0.05
SHIPPING_RATE = 0.05
SHIPPING_FLAT_GBP = 1.00


def get_db_connection():
    return psycopg2.connect(
        host=os.environ['PROXY_ENDPOINT'],
        user=os.environ['DB_USER'],
        password=os.environ['DB_PASSWORD'],
        port=int(os.environ.get('DB_PORT', '5432')),
        dbname=os.environ.get('DATABASE_NAME', 'op_cardrush_link'),
        cursor_factory=RealDictCursor,
        connect_timeout=10,
    )


def _json_response(status, body):
    return {
        'statusCode': status,
        'headers': {
            'Content-Type': 'application/json',
            'Access-Control-Allow-Origin': '*',
            'Access-Control-Allow-Headers': 'Content-Type, X-Admin-Secret',
            'Access-Control-Allow-Methods': 'GET, POST, OPTIONS',
        },
        'body': json.dumps(body, default=str),
    }


def _selling_price(price_yen, gbp_to_jpy):
    """Derive Shopify selling price from price_yen using current FX rate.

    Same formula as calculator Lambda:
        cost_gbp = price_yen / gbp_to_jpy
        landed   = cost_gbp * (1 + shipping_rate) + shipping_flat
        P        = landed * (1 + margin) * (1 + vat) / (1 - fee * (1 + vat))
        selling  = ceil(P) + 0.80
    """
    if not price_yen or not gbp_to_jpy or gbp_to_jpy <= 0:
        return None
    cost_gbp = float(price_yen) / float(gbp_to_jpy)
    landed = cost_gbp * (1 + SHIPPING_RATE) + SHIPPING_FLAT_GBP
    denominator = 1 - SHOPIFY_FEE * (1 + VAT)
    if denominator <= 0:
        return None
    p = landed * (1 + MARGIN) * (1 + VAT) / denominator
    return math.ceil(p) + 0.80


def _selling_price_usd(price_usd, usd_to_gbp):
    """Derive Shopify selling price from price_usd for EN cards.

    Same formula as JP but no import shipping (market price, not wholesale):
        cost_gbp = price_usd * usd_to_gbp
        P        = cost_gbp * (1 + margin) * (1 + vat) / (1 - fee * (1 + vat))
        selling  = ceil(P) + 0.80
    """
    if not price_usd or not usd_to_gbp or usd_to_gbp <= 0:
        return None
    cost_gbp = float(price_usd) * float(usd_to_gbp)
    denominator = 1 - SHOPIFY_FEE * (1 + VAT)
    if denominator <= 0:
        return None
    p = cost_gbp * (1 + MARGIN) * (1 + VAT) / denominator
    return math.ceil(p) + 0.80


def _get_prices(params):
    sku = params.get('sku')
    if not sku:
        return _json_response(400, {'error': 'sku parameter required'})

    if not re.match(r'^[A-Za-z0-9\-]+$', sku):
        return _json_response(400, {'error': 'Invalid SKU format'})

    days = params.get('days')

    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            # Get current FX rates from cardrush_link
            cur.execute("""
                SELECT gbp_to_jpy FROM cardrush_link
                WHERE gbp_to_jpy IS NOT NULL AND gbp_to_jpy > 0
                LIMIT 1
            """)
            fx_row = cur.fetchone()
            gbp_to_jpy = float(fx_row['gbp_to_jpy']) if fx_row else None

            cur.execute("""
                SELECT usd_to_gbp FROM cardrush_link
                WHERE usd_to_gbp IS NOT NULL AND usd_to_gbp > 0
                LIMIT 1
            """)
            usd_row = cur.fetchone()
            usd_to_gbp = float(usd_row['usd_to_gbp']) if usd_row else None

            if days:
                try:
                    days = int(days)
                except ValueError:
                    return _json_response(400, {'error': 'days must be an integer'})
                cur.execute("""
                    SELECT DISTINCT ON (DATE(recorded_at))
                        price_yen, price_usd,
                        cardrush_stock, cardrush_stock_subgrade,
                        price_yen_subgrade,
                        DATE(recorded_at) as date
                    FROM price_history
                    WHERE sku = %s
                      AND recorded_at >= CURRENT_DATE - INTERVAL '1 day' * %s
                    ORDER BY DATE(recorded_at), recorded_at DESC
                """, (sku, days))
            else:
                cur.execute("""
                    SELECT DISTINCT ON (DATE(recorded_at))
                        price_yen, price_usd,
                        cardrush_stock, cardrush_stock_subgrade,
                        price_yen_subgrade,
                        DATE(recorded_at) as date
                    FROM price_history
                    WHERE sku = %s
                    ORDER BY DATE(recorded_at), recorded_at DESC
                """, (sku,))

            rows = cur.fetchall()

        # Compute GBP selling price from whichever source price is present
        prices = []
        for row in rows:
            price_yen = row['price_yen']
            price_usd = row.get('price_usd')
            if price_yen:
                selling = _selling_price(price_yen, gbp_to_jpy)
            elif price_usd:
                selling = _selling_price_usd(price_usd, usd_to_gbp)
            else:
                selling = None
            prices.append({
                'date': row['date'],
                'price_yen': price_yen,
                'price_usd': float(price_usd) if price_usd else None,
                'price_yen_subgrade': row.get('price_yen_subgrade'),
                'cardrush_stock': row.get('cardrush_stock'),
                'cardrush_stock_subgrade': row.get('cardrush_stock_subgrade'),
                'selling_price_gbp': selling,
            })

        return _json_response(200, {
            'sku': sku,
            'count': len(prices),
            'gbp_to_jpy': gbp_to_jpy,
            'usd_to_gbp': usd_to_gbp,
            'prices': prices,
        })
    except Exception as e:
        return _json_response(500, {'error': str(e)})
    finally:
        if conn:
            conn.close()


def _get_skus():
    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            cur.execute("""
                SELECT DISTINCT sku FROM price_history ORDER BY sku
            """)
            skus = [row['sku'] for row in cur.fetchall()]

        return _json_response(200, {
            'count': len(skus),
            'skus': skus,
        })
    except Exception as e:
        return _json_response(500, {'error': str(e)})
    finally:
        if conn:
            conn.close()


def _parse_sku(sku):
    """Parse SKU into game, set_code, card_number, lang, variant components.

    Supports both 4-segment (OP-OP01-001-JP) and 5-segment (OP-OP01-001-EN-P1) SKUs.
    """
    if not sku:
        return {}
    m = re.match(r'^(OP|PKMN)-([A-Za-z0-9]+)-(\d{2,4})-([A-Z]{2})(?:-(P\d+))?$', sku)
    if not m:
        return {}
    prefix, set_code, card_number, lang = m.group(1), m.group(2), m.group(3), m.group(4)
    variant = m.group(5)  # None for base cards, "P1"/"P2" for parallels
    result = {'game': prefix, 'set_code': set_code, 'card_number': card_number, 'lang': lang}
    if variant:
        result['variant'] = variant
    return result


def _has_table(cur, table_name):
    """Check if a table exists in the database."""
    cur.execute("""
        SELECT EXISTS (
            SELECT 1 FROM information_schema.tables
            WHERE table_name = %s
        )
    """, (table_name,))
    return cur.fetchone()['exists']


def _has_column(cur, table_name, column_name):
    """Check if a column exists on a table."""
    cur.execute("""
        SELECT EXISTS (
            SELECT 1 FROM information_schema.columns
            WHERE table_name = %s AND column_name = %s
        )
    """, (table_name, column_name))
    return cur.fetchone()['exists']


def _get_catalog():
    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            cur.execute("""
                SELECT gbp_to_jpy FROM cardrush_link
                WHERE gbp_to_jpy IS NOT NULL AND gbp_to_jpy > 0 LIMIT 1
            """)
            fx_row = cur.fetchone()
            gbp_to_jpy = float(fx_row['gbp_to_jpy']) if fx_row else None

            has_catalog = (_has_table(cur, 'card_catalog')
                          and _has_column(cur, 'cardrush_link', 'card_image_id'))

            if has_catalog:
                cur.execute("""
                    SELECT cl.sku, cl.price_yen, cl.price_usd,
                           cl.shopify_selling_price,
                           cl.ebay_business_selling_price,
                           cl.cardmarket_selling_price,
                           cl.card_image_id,
                           COALESCE(si.quantity, 0) AS stock_qty,
                           cc.card_name, cc.rarity, cc.card_color, cc.card_type
                    FROM cardrush_link cl
                    LEFT JOIN stock_inventory si ON cl.sku = si.sku
                    LEFT JOIN card_catalog cc ON cl.card_image_id = cc.card_image_id
                    WHERE cl.shopify_selling_price IS NOT NULL
                    ORDER BY cl.sku
                """)
            else:
                cur.execute("""
                    SELECT cl.sku, cl.price_yen, cl.price_usd,
                           cl.shopify_selling_price,
                           cl.ebay_business_selling_price,
                           cl.cardmarket_selling_price,
                           NULL AS card_image_id,
                           COALESCE(si.quantity, 0) AS stock_qty,
                           NULL AS card_name, NULL AS rarity,
                           NULL AS card_color, NULL AS card_type
                    FROM cardrush_link cl
                    LEFT JOIN stock_inventory si ON cl.sku = si.sku
                    WHERE cl.shopify_selling_price IS NOT NULL
                    ORDER BY cl.sku
                """)
            rows = cur.fetchall()

        skus = []
        for row in rows:
            parsed = _parse_sku(row['sku'])
            skus.append({
                'sku': row['sku'],
                'game': parsed.get('game', ''),
                'set_code': parsed.get('set_code', ''),
                'card_number': parsed.get('card_number', ''),
                'lang': parsed.get('lang', 'JP'),
                'price_yen': row['price_yen'],
                'price_usd': float(row['price_usd']) if row.get('price_usd') else None,
                'shopify_price': float(row['shopify_selling_price']) if row['shopify_selling_price'] else None,
                'ebay_price': float(row['ebay_business_selling_price']) if row['ebay_business_selling_price'] else None,
                'cardmarket_price': float(row['cardmarket_selling_price']) if row['cardmarket_selling_price'] else None,
                'in_stock': row['stock_qty'] > 0,
                'card_name': row.get('card_name'),
                'rarity': row.get('rarity'),
                'card_color': row.get('card_color'),
                'card_type': row.get('card_type'),
                'card_image_id': row.get('card_image_id'),
                'variant': parsed.get('variant'),
            })

        return _json_response(200, {
            'count': len(skus),
            'gbp_to_jpy': gbp_to_jpy,
            'skus': skus,
        })
    except Exception as e:
        return _json_response(500, {'error': str(e)})
    finally:
        if conn:
            conn.close()


def _get_indices(params):
    days = params.get('days')
    game_filter = params.get('game')  # optional: 'OP' or 'PKMN'
    lang_filter = params.get('lang')  # optional: 'JP' or 'EN'
    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            days_clause = ""
            days_params = []
            if days:
                try:
                    days = int(days)
                except ValueError:
                    return _json_response(400, {'error': 'days must be an integer'})
                days_clause = "AND DATE(recorded_at) >= CURRENT_DATE - INTERVAL '1 day' * %s"
                days_params = [days]

            if game_filter and not re.match(r'^[A-Z]{2,4}$', game_filter):
                return _json_response(400, {'error': 'Invalid game parameter'})

            if lang_filter and lang_filter not in ('JP', 'EN'):
                return _json_response(400, {'error': 'lang must be JP or EN'})

            lang_clause = ""
            lang_params = []
            if lang_filter:
                lang_clause = "AND sku ~ %s"
                lang_params = ['-' + lang_filter + '(-|$)']

            # Get current FX rate as fallback for old rows missing gbp_to_jpy
            cur.execute("""
                SELECT gbp_to_jpy FROM cardrush_link
                WHERE gbp_to_jpy IS NOT NULL AND gbp_to_jpy > 0 LIMIT 1
            """)
            fx_row = cur.fetchone()
            current_fx = float(fx_row['gbp_to_jpy']) if fx_row else 208.46

            PRICE_EXPR = """COALESCE(
                        shopify_selling_price,
                        CEIL((price_yen / COALESCE(NULLIF(gbp_to_jpy, 0), %s) * 1.05 + 1.00)
                             * 1.22 * 1.20 / (1 - 0.05 * 1.20)) + 0.80
                    )"""

            # ── Materialize daily prices once (avoids triple table scan) ──
            cur.execute("""
                CREATE TEMP TABLE _idx_daily ON COMMIT DROP AS
                SELECT * FROM (
                    SELECT DISTINCT ON (DATE(recorded_at), sku)
                        DATE(recorded_at) AS dt, sku,
                        SPLIT_PART(sku, '-', 2) AS set_code,
                        CASE WHEN sku LIKE 'OP-%%' THEN 'OP' ELSE 'PKMN' END AS game,
                        CASE WHEN sku ~ '-EN(-|$)' THEN 'EN' ELSE 'JP' END AS lang,
                        """ + PRICE_EXPR + """ AS price
                    FROM price_history
                    WHERE (price_yen IS NOT NULL OR price_usd IS NOT NULL)
                    """ + days_clause + """
                    """ + lang_clause + """
                    ORDER BY DATE(recorded_at), sku, recorded_at DESC
                ) sub WHERE price IS NOT NULL
            """, [current_fx] + days_params + lang_params)

            # ── Query 1: Game-level index time series (split by language) ──
            cur.execute("""
                WITH base AS (SELECT MIN(dt) AS dt FROM _idx_daily),
                base_sums AS (
                    SELECT 'ALL' AS series, SUM(price) AS total FROM _idx_daily WHERE dt = (SELECT dt FROM base)
                    UNION ALL
                    SELECT game || '-' || lang, SUM(price) FROM _idx_daily WHERE dt = (SELECT dt FROM base) GROUP BY game, lang
                ),
                daily_sums AS (
                    SELECT dt, 'ALL' AS series, SUM(price) AS total, COUNT(*) AS cnt FROM _idx_daily GROUP BY dt
                    UNION ALL
                    SELECT dt, game || '-' || lang, SUM(price), COUNT(*) FROM _idx_daily GROUP BY dt, game, lang
                )
                SELECT ds.dt AS date, ds.series,
                       ROUND((ds.total / NULLIF(bs.total, 0)) * 100, 2) AS index_value,
                       ds.total AS total_value, ds.cnt AS sku_count
                FROM daily_sums ds
                JOIN base_sums bs ON ds.series = bs.series
                ORDER BY ds.dt, ds.series
            """)
            ts_rows = cur.fetchall()

            # ── Query 2: Set-level breakdown (current vs previous day) ──
            cur.execute("""
                WITH latest_day AS (SELECT MAX(dt) AS dt FROM _idx_daily),
                prev_day AS (SELECT MAX(dt) AS dt FROM _idx_daily WHERE dt < (SELECT dt FROM latest_day)),
                current_prices AS (
                    SELECT DISTINCT ON (sku) sku, set_code, game, lang, price
                    FROM _idx_daily WHERE dt = (SELECT dt FROM latest_day) ORDER BY sku
                ),
                prev_prices AS (
                    SELECT DISTINCT ON (sku) sku, price
                    FROM _idx_daily WHERE dt = (SELECT dt FROM prev_day) ORDER BY sku
                )
                SELECT c.set_code, c.game, c.lang, COUNT(*) AS card_count,
                       ROUND(AVG(c.price)::numeric, 2) AS avg_price,
                       ROUND(SUM(c.price)::numeric, 2) AS total_value,
                       ROUND(MIN(c.price)::numeric, 2) AS min_price,
                       ROUND(MAX(c.price)::numeric, 2) AS max_price,
                       ROUND(CASE WHEN COALESCE(SUM(p.price), 0) > 0
                           THEN ((SUM(c.price) - SUM(p.price)) / SUM(p.price)) * 100 ELSE 0 END::numeric, 2) AS pct_change
                FROM current_prices c LEFT JOIN prev_prices p ON c.sku = p.sku
                GROUP BY c.set_code, c.game, c.lang ORDER BY c.game, c.set_code, c.lang
            """)
            set_rows = cur.fetchall()

            # ── Query 3: Per-set index time series (only when game param given) ──
            set_ts_rows = []
            if game_filter:
                cur.execute("""
                    WITH base AS (SELECT MIN(dt) AS dt FROM _idx_daily),
                    set_base_sums AS (
                        SELECT set_code, game, lang, SUM(price) AS total
                        FROM _idx_daily WHERE dt = (SELECT dt FROM base) AND game = %s
                        GROUP BY set_code, game, lang
                    ),
                    set_daily_sums AS (
                        SELECT dt, set_code, game, lang, SUM(price) AS total, COUNT(*) AS cnt
                        FROM _idx_daily WHERE game = %s GROUP BY dt, set_code, game, lang
                    )
                    SELECT sds.dt AS date, sds.set_code, sds.game, sds.lang,
                           ROUND((sds.total / NULLIF(sbs.total, 0)) * 100, 2) AS index_value,
                           sds.total AS total_value, sds.cnt AS sku_count
                    FROM set_daily_sums sds
                    JOIN set_base_sums sbs ON sds.set_code = sbs.set_code AND sds.lang = sbs.lang
                    ORDER BY sds.dt, sds.set_code, sds.lang
                """, [game_filter, game_filter])
                set_ts_rows = cur.fetchall()

        # Build response
        if not ts_rows:
            return _json_response(200, {
                'base_date': None, 'latest_date': None,
                'series': {}, 'sets': [],
            })

        series_names = {
            'ALL': 'CTCG All Cards',
            'OP-JP': 'One Piece Japanese', 'OP-EN': 'One Piece English',
            'PKMN-JP': 'Pokemon Japanese', 'PKMN-EN': 'Pokemon English',
        }
        series = {}
        for row in ts_rows:
            s = row['series']
            if s not in series:
                series[s] = {
                    'name': series_names.get(s, s),
                    'current_index': None, 'change_1d': 0,
                    'total_value': 0, 'sku_count': 0, 'history': [],
                }
            series[s]['history'].append({
                'date': row['date'],
                'index': float(row['index_value']) if row['index_value'] else 100.0,
                'total': float(row['total_value']) if row['total_value'] else 0,
            })

        base_date = None
        latest_date = None
        for s in series.values():
            if s['history']:
                s['current_index'] = s['history'][-1]['index']
                s['total_value'] = s['history'][-1]['total']
                s['sku_count'] = 0
                if not base_date:
                    base_date = s['history'][0]['date']
                    latest_date = s['history'][-1]['date']
                if len(s['history']) >= 2:
                    prev_idx = s['history'][-2]['index']
                    curr_idx = s['history'][-1]['index']
                    if prev_idx > 0:
                        s['change_1d'] = round((curr_idx - prev_idx) / prev_idx * 100, 2)

        # Fill sku_count from last day's data
        for row in ts_rows:
            if row['date'] == latest_date and row['series'] in series:
                series[row['series']]['sku_count'] = row['sku_count']

        sets = []
        for row in set_rows:
            sets.append({
                'set_code': row['set_code'],
                'game': row['game'],
                'lang': row['lang'],
                'card_count': row['card_count'],
                'avg_price': float(row['avg_price']) if row['avg_price'] else 0,
                'total_value': float(row['total_value']) if row['total_value'] else 0,
                'min_price': float(row['min_price']) if row['min_price'] else 0,
                'max_price': float(row['max_price']) if row['max_price'] else 0,
                'pct_change': float(row['pct_change']) if row['pct_change'] else 0,
            })

        result = {
            'base_date': base_date,
            'latest_date': latest_date,
            'series': series,
            'sets': sets,
        }

        if set_ts_rows:
            set_series = {}
            for row in set_ts_rows:
                key = row['set_code'] + '-' + row['lang']
                if key not in set_series:
                    set_series[key] = {
                        'set_code': row['set_code'],
                        'lang': row['lang'],
                        'game': row['game'],
                        'history': [],
                    }
                set_series[key]['history'].append({
                    'date': row['date'],
                    'index': float(row['index_value']) if row['index_value'] else 100.0,
                    'total': float(row['total_value']) if row['total_value'] else 0,
                })
            result['set_series'] = set_series

        return _json_response(200, result)
    except Exception as e:
        return _json_response(500, {'error': str(e)})
    finally:
        if conn:
            conn.close()


def _get_buylist():
    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            # Read buylist config from stock_config
            cur.execute("""
                SELECT config_key, config_value FROM stock_config
                WHERE config_key IN ('buylist_cash_rate', 'buylist_credit_rate', 'buylist_want_tiers', 'buylist_default_want')
            """)
            config = {row['config_key']: row['config_value'] for row in cur.fetchall()}

            cash_rate = float(config.get('buylist_cash_rate', 0.55))
            credit_rate = float(config.get('buylist_credit_rate', 0.77))
            want_tiers = config.get('buylist_want_tiers', [{'max_stock': 0, 'want': 4}, {'max_stock': 2, 'want': 2}])
            default_want = int(config.get('buylist_default_want', 0))

            # JOIN cardrush_link + stock_inventory for OP-* SKUs
            cur.execute("""
                SELECT cl.sku, cl.landed_cost_gbp,
                       COALESCE(si.quantity, 0) AS stock_qty
                FROM cardrush_link cl
                LEFT JOIN stock_inventory si ON cl.sku = si.sku
                WHERE cl.sku LIKE 'OP-%%'
                  AND cl.landed_cost_gbp IS NOT NULL
                  AND cl.landed_cost_gbp > 0
                  AND cl.price_yen IS NOT NULL
                ORDER BY cl.sku
            """)
            rows = cur.fetchall()

        items = []
        for row in rows:
            landed = float(row['landed_cost_gbp'])
            cash_price = math.floor(landed * cash_rate * 10) / 10
            credit_price = math.floor(landed * credit_rate * 10) / 10
            if credit_price < 0.10:
                continue

            stock = int(row['stock_qty'])
            want_qty = default_want
            for tier in want_tiers:
                if stock <= tier['max_stock']:
                    want_qty = tier['want']
                    break

            parsed = _parse_sku(row['sku'])
            items.append({
                'sku': row['sku'],
                'set_code': parsed.get('set_code', ''),
                'card_number': parsed.get('card_number', ''),
                'cash_price': cash_price,
                'credit_price': credit_price,
                'cash_want': want_qty,
                'credit_want': -1,  # unlimited
            })

        cash_buying_count = sum(1 for it in items if it['cash_want'] > 0)

        return _json_response(200, {
            'count': len(items),
            'cash_buying_count': cash_buying_count,
            'cash_rate': cash_rate,
            'credit_rate': credit_rate,
            'items': items,
        })
    except Exception as e:
        return _json_response(500, {'error': str(e)})
    finally:
        if conn:
            conn.close()


def _post_tradein(body):
    """Handle trade-in submission. Validates cart, stores in RDS, notifies store."""
    # Validate required fields
    required = ['customer_name', 'customer_email', 'payment_method', 'delivery_method', 'items']
    for field in required:
        if not body.get(field):
            return _json_response(400, {'error': 'Missing required field: ' + field})

    email = body['customer_email'].strip()
    if not re.match(r'^[^@\s]+@[^@\s]+\.[^@\s]+$', email):
        return _json_response(400, {'error': 'Invalid email address'})

    if body['payment_method'] not in ('cash', 'credit'):
        return _json_response(400, {'error': 'payment_method must be cash or credit'})

    if body['delivery_method'] not in ('mail', 'instore'):
        return _json_response(400, {'error': 'delivery_method must be mail or instore'})

    items = body['items']
    if not isinstance(items, list) or len(items) == 0:
        return _json_response(400, {'error': 'Cart is empty'})

    if len(items) > 100:
        return _json_response(400, {'error': 'Maximum 100 items per submission'})

    # Honeypot check
    if body.get('website'):
        return _json_response(200, {'reference': 'TI-00000000-0000'})

    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            # Read buylist rates
            cur.execute("""
                SELECT config_key, config_value FROM stock_config
                WHERE config_key IN ('buylist_cash_rate', 'buylist_credit_rate')
            """)
            config = {row['config_key']: row['config_value'] for row in cur.fetchall()}
            cash_rate = float(config.get('buylist_cash_rate', 0.55))
            credit_rate = float(config.get('buylist_credit_rate', 0.77))

            # Validate SKUs and get current landed costs
            skus = list(set(item['sku'] for item in items))
            placeholders = ','.join(['%s'] * len(skus))
            cur.execute("""
                SELECT sku, landed_cost_gbp FROM cardrush_link
                WHERE sku IN ({})
                  AND landed_cost_gbp IS NOT NULL
                  AND landed_cost_gbp > 0
            """.format(placeholders), skus)
            prices = {row['sku']: float(row['landed_cost_gbp']) for row in cur.fetchall()}

            for item in items:
                if item['sku'] not in prices:
                    return _json_response(400, {'error': 'Card not on buylist: ' + item['sku']})
                qty = item.get('quantity', 0)
                if not isinstance(qty, int) or qty < 1 or qty > 99:
                    return _json_response(400, {'error': 'Invalid quantity for ' + item['sku']})

            # Calculate totals at current prices
            cash_total = 0.0
            credit_total = 0.0
            item_records = []
            for item in items:
                landed = prices[item['sku']]
                cash_price = math.floor(landed * cash_rate * 10) / 10
                credit_price = math.floor(landed * credit_rate * 10) / 10
                qty = int(item['quantity'])
                cash_total += cash_price * qty
                credit_total += credit_price * qty
                item_records.append((item['sku'], qty, cash_price, credit_price))

            if credit_total < 5.0:
                return _json_response(400, {
                    'error': 'Minimum trade-in value is \u00a35.00 credit. Current total: \u00a3%.2f' % credit_total
                })

            # Generate reference: TI-YYYYMMDD-XXXX
            today = datetime.now(timezone.utc).strftime('%Y%m%d')
            ref_suffix = os.urandom(2).hex().upper()
            reference = 'TI-%s-%s' % (today, ref_suffix)

            # Insert submission
            cur.execute("""
                INSERT INTO tradein_submissions
                (reference, status, customer_name, customer_email, customer_phone,
                 payment_method, delivery_method, is_over_18, notes,
                 quoted_cash_total, quoted_credit_total, quote_expires_at)
                VALUES (%s, 'submitted', %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW() + INTERVAL '7 days')
                RETURNING id, quote_expires_at
            """, (
                reference,
                body['customer_name'].strip(),
                email,
                body.get('customer_phone', '').strip(),
                body['payment_method'],
                body['delivery_method'],
                body.get('is_over_18', True),
                body.get('notes', '').strip(),
                round(cash_total, 2),
                round(credit_total, 2),
            ))
            result = cur.fetchone()
            submission_id = result['id']
            expires_at = result['quote_expires_at']

            # Insert items
            for sku, qty, cash_p, credit_p in item_records:
                cur.execute("""
                    INSERT INTO tradein_items
                    (submission_id, sku, quantity, quoted_cash_price, quoted_credit_price)
                    VALUES (%s, %s, %s, %s, %s)
                """, (submission_id, sku, qty, cash_p, credit_p))

            conn.commit()

        # Notify store (best-effort)
        _notify_tradein(reference, body['customer_name'].strip(), email,
                        body['payment_method'], body['delivery_method'],
                        len(items), cash_total, credit_total)

        # Send confirmation email to customer (best-effort)
        email_sent = False
        try:
            sub = {
                'reference': reference,
                'customer_name': body['customer_name'].strip(),
                'payment_method': body['payment_method'],
                'delivery_method': body['delivery_method'],
                'quoted_cash_total': round(cash_total, 2),
                'quoted_credit_total': round(credit_total, 2),
                'item_count': len(items),
                'expires_at': str(expires_at),
            }
            subj, email_body = confirmation_email(sub)
            email_sent = send_email(email, subj, email_body)
        except Exception as e:
            print('Confirmation email failed: %s' % str(e))

        return _json_response(200, {
            'reference': reference,
            'quoted_cash_total': round(cash_total, 2),
            'quoted_credit_total': round(credit_total, 2),
            'item_count': len(items),
            'payment_method': body['payment_method'],
            'delivery_method': body['delivery_method'],
            'expires_at': str(expires_at),
            'email_sent': email_sent,
        })
    except Exception as e:
        if conn:
            conn.rollback()
        return _json_response(500, {'error': str(e)})
    finally:
        if conn:
            conn.close()


def _notify_tradein(reference, name, email, payment, delivery, item_count, cash_total, credit_total):
    """Send SNS notification to store about new trade-in. Best-effort, never raises."""
    topic_arn = os.environ.get('SNS_TOPIC_ARN')
    if not topic_arn:
        return
    try:
        import boto3
        sns = boto3.client('sns')
        chosen = '\u00a3%.2f (%s)' % (credit_total if payment == 'credit' else cash_total, payment)
        subject = 'New Trade-In: %s \u2014 %d cards \u2014 %s' % (reference, item_count, chosen)
        message = (
            'New trade-in submission:\n\n'
            'Reference: %s\n'
            'Customer: %s (%s)\n'
            'Payment: %s\n'
            'Delivery: %s\n'
            'Items: %d cards\n'
            'Cash total: \u00a3%.2f\n'
            'Credit total: \u00a3%.2f\n'
        ) % (reference, name, email, payment, delivery, item_count, cash_total, credit_total)
        sns.publish(TopicArn=topic_arn, Subject=subject[:100], Message=message)
    except Exception as e:
        print('SNS notification failed: %s' % str(e))


def _get_tradein_status(reference, params):
    """Look up trade-in status. Email acts as auth token."""
    email = (params.get('email') or '').strip().lower()
    if not email:
        return _json_response(400, {'error': 'email parameter required'})

    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, reference, status, customer_name,
                       payment_method, delivery_method,
                       quoted_cash_total, quoted_credit_total,
                       quote_expires_at, created_at, updated_at,
                       tracking_number, payment_reference
                FROM tradein_submissions
                WHERE reference = %s AND LOWER(customer_email) = %s
            """, (reference, email))
            row = cur.fetchone()

            if not row:
                return _json_response(404, {'error': 'Trade-in not found'})

            cur.execute("""
                SELECT sku, quantity, quoted_cash_price, quoted_credit_price
                FROM tradein_items
                WHERE submission_id = %s
                ORDER BY sku
            """, (row['id'],))
            items = cur.fetchall()

        chosen_total = (float(row['quoted_credit_total'])
                        if row['payment_method'] == 'credit'
                        else float(row['quoted_cash_total']))

        return _json_response(200, {
            'reference': row['reference'],
            'status': row['status'],
            'customer_name': row['customer_name'],
            'payment_method': row['payment_method'],
            'delivery_method': row['delivery_method'],
            'quoted_cash_total': float(row['quoted_cash_total']),
            'quoted_credit_total': float(row['quoted_credit_total']),
            'chosen_total': chosen_total,
            'expires_at': row['quote_expires_at'],
            'submitted_at': row['created_at'],
            'updated_at': row['updated_at'],
            'tracking_number': row['tracking_number'],
            'payment_reference': row['payment_reference'],
            'items': [{
                'sku': item['sku'],
                'quantity': item['quantity'],
                'cash_price': float(item['quoted_cash_price']),
                'credit_price': float(item['quoted_credit_price']),
            } for item in items],
        })
    except Exception as e:
        return _json_response(500, {'error': str(e)})
    finally:
        if conn:
            conn.close()


def _update_tradein_status(reference, body):
    """Admin endpoint to update trade-in status. Sends email on change."""
    new_status = body.get('status')
    if new_status not in ('received', 'paid'):
        return _json_response(400, {'error': 'status must be received or paid'})

    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            set_clauses = ["status = %s", "updated_at = NOW()"]
            params = [new_status]

            tracking = body.get('tracking_number')
            if tracking:
                set_clauses.append("tracking_number = %s")
                params.append(str(tracking)[:100])

            payment_ref = body.get('payment_reference')
            if payment_ref:
                set_clauses.append("payment_reference = %s")
                params.append(str(payment_ref)[:100])

            params.append(reference)

            cur.execute("""
                UPDATE tradein_submissions
                SET %s
                WHERE reference = %%s
                RETURNING id, reference, status, customer_name, customer_email,
                          payment_method, delivery_method,
                          quoted_cash_total, quoted_credit_total,
                          quote_expires_at, tracking_number, payment_reference
            """ % ", ".join(set_clauses), params)

            row = cur.fetchone()
            if not row:
                conn.rollback()
                return _json_response(404, {'error': 'Trade-in not found'})

            conn.commit()

        # Send email to customer (best-effort)
        chosen_total = (float(row['quoted_credit_total'])
                        if row['payment_method'] == 'credit'
                        else float(row['quoted_cash_total']))
        sub = {
            'reference': row['reference'],
            'customer_name': row['customer_name'],
            'payment_method': row['payment_method'],
            'delivery_method': row['delivery_method'],
            'quoted_cash_total': float(row['quoted_cash_total']),
            'quoted_credit_total': float(row['quoted_credit_total']),
            'expires_at': str(row['quote_expires_at']),
            'payment_reference': row['payment_reference'],
        }
        if new_status == 'received':
            subj, email_body = received_email(sub)
        else:
            subj, email_body = payment_email(sub)
        email_sent = send_email(row['customer_email'], subj, email_body)

        return _json_response(200, {
            'reference': row['reference'],
            'status': row['status'],
            'email_sent': email_sent,
        })
    except Exception as e:
        if conn:
            conn.rollback()
        return _json_response(500, {'error': str(e)})
    finally:
        if conn:
            conn.close()


def lambda_handler(event, context):
    method = event.get('requestContext', {}).get('http', {}).get('method', 'GET')
    path = event.get('requestContext', {}).get('http', {}).get('path', '/')

    if method == 'OPTIONS':
        return _json_response(200, {})

    # Parse dynamic tradein routes
    ref_status_match = re.match(r'^/tradein/(TI-\d{8}-[A-F0-9]{4})/status$', path)
    ref_match = re.match(r'^/tradein/(TI-\d{8}-[A-F0-9]{4})$', path)

    # POST routes
    if method == 'POST':
        try:
            body = json.loads(event.get('body', '{}'))
        except (json.JSONDecodeError, TypeError):
            return _json_response(400, {'error': 'Invalid JSON body'})

        if path == '/tradein':
            return _post_tradein(body)

        if ref_status_match:
            headers = event.get('headers', {})
            admin_secret = os.environ.get('ADMIN_SECRET', '')
            provided = headers.get('x-admin-secret', '')
            if not admin_secret or not hmac.compare_digest(provided, admin_secret):
                return _json_response(403, {'error': 'Forbidden'})
            return _update_tradein_status(ref_status_match.group(1), body)

        return _json_response(405, {'error': 'Method not allowed'})

    if method != 'GET':
        return _json_response(405, {'error': 'Method not allowed'})

    params = event.get('queryStringParameters') or {}

    if ref_match:
        return _get_tradein_status(ref_match.group(1), params)
    elif path == '/catalog':
        return _get_catalog()
    elif path == '/indices':
        return _get_indices(params)
    elif path == '/skus':
        return _get_skus()
    elif path == '/buylist':
        return _get_buylist()
    else:
        return _get_prices(params)
