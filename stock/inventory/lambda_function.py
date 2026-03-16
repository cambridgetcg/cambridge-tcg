"""Stock Inventory Admin API Lambda

CRUD API for stock_inventory table (unified with eBay/Shopify stock pipeline).
Invoked via boto3 lambda.invoke() from the Streamlit admin UI.

Routes:
    GET  /inventory              — List all stock + selling prices
    GET  /inventory/{sku}        — Single SKU detail
    POST /inventory/update       — Set absolute qty/cost for one SKU
    POST /inventory/batch-adjust — Bulk qty delta
    POST /inventory/order        — Record purchase order (batch qty+cost update)
    POST /inventory/promo/next   — Get next available promo SKU version
    POST /inventory/merge        — One-time merge stock_manual → stock_inventory
    GET  /inventory/catalog      — All SKUs from cardrush_link (for order form)
    GET  /inventory/restock      — Restock recommendations ranked by priority score

Environment Variables:
    PROXY_ENDPOINT, DB_USER, DB_PASSWORD, DATABASE_NAME, DB_PORT
    MARGIN_MIN, MARGIN_MAX, MARGIN_DEFAULT, VELOCITY_WINDOW_DAYS,
    VELOCITY_LOW, VELOCITY_HIGH, SHOPIFY_FEE, VAT_RATE
"""

import json
import math
import os
import re
import psycopg2
from psycopg2.extras import RealDictCursor
from datetime import datetime, timezone

STOCK_TABLE = 'stock_inventory'


def _safe_table_name(name):
    if not re.match(r'^[a-zA-Z_][a-zA-Z0-9_]*$', name):
        raise ValueError(f"Invalid table name: {name}")
    return name


def get_db_connection():
    return psycopg2.connect(
        host=os.environ['PROXY_ENDPOINT'],
        user=os.environ['DB_USER'],
        password=os.environ['DB_PASSWORD'],
        port=int(os.environ.get('DB_PORT', '5432')),
        dbname=os.environ.get('DATABASE_NAME', 'op_cardrush_link'),
        connect_timeout=10,
    )


def _json_response(status, body):
    return {
        'statusCode': status,
        'headers': {
            'Content-Type': 'application/json',
            'Access-Control-Allow-Origin': '*',
            'Access-Control-Allow-Headers': 'Content-Type, x-api-key',
            'Access-Control-Allow-Methods': 'GET, POST, OPTIONS',
        },
        'body': json.dumps(body, default=str),
    }


def _check_auth(event):
    """Return error response if auth fails, None if OK.

    When invoked via boto3 lambda.invoke(), IAM auth is already enforced.
    API key check only applies when invoked via Function URL.
    """
    # Direct boto3 invoke — IAM auth is sufficient
    rc = event.get('requestContext', {})
    if 'http' not in rc or rc.get('http', {}).get('sourceIp') is None:
        return None

    # Function URL invoke — check API key
    api_key = os.environ.get('INVENTORY_API_KEY', '')
    if not api_key:
        return _json_response(500, {'error': 'INVENTORY_API_KEY not configured'})
    headers = event.get('headers', {}) or {}
    provided = headers.get('x-api-key', '')
    if provided != api_key:
        return _json_response(401, {'error': 'Invalid or missing x-api-key'})
    return None


def _parse_path(event):
    """Extract HTTP method and path from Function URL event."""
    rc = event.get('requestContext', {}).get('http', {})
    method = rc.get('method', 'GET').upper()
    path = rc.get('path', '/')
    return method, path


def _parse_body(event):
    """Parse JSON body from event."""
    body = event.get('body', '')
    if not body:
        return {}
    if event.get('isBase64Encoded'):
        import base64
        body = base64.b64decode(body).decode('utf-8')
    return json.loads(body)


def _get_query_params(event):
    return event.get('queryStringParameters') or {}


# --- Route handlers ---

def _get_listing_tiers(conn):
    """Load listing tier config from stock_config table."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT config_key, config_value FROM stock_config "
            "WHERE config_key IN ('listing_tiers', 'listing_default_cap')"
        )
        rows = {r[0]: r[1] for r in cur.fetchall()}
    tiers = rows.get('listing_tiers')
    if tiers is None:
        return None
    return {'tiers': tiers, 'default_cap': rows.get('listing_default_cap', 1)}


def _listing_cap(price_gbp, tier_config):
    """Get the listing tier cap for a given selling price."""
    if tier_config is None or price_gbp is None:
        return None
    for tier in tier_config['tiers']:
        if price_gbp < tier['under_gbp']:
            return tier['cap']
    return tier_config['default_cap']


def _listed_qty(qty, price_gbp, tier_config):
    """Compute listed qty: min(actual_qty, tier_cap) based on selling price."""
    cap = _listing_cap(price_gbp, tier_config)
    return min(qty, cap) if cap is not None else qty


def _velocity_to_margin(units_sold, low=1, high=8,
                        margin_min=0.12, margin_max=0.22, margin_default=0.18):
    """Map sales velocity to margin via linear interpolation (mirrors calculator)."""
    if units_sold == 0:
        return margin_default
    if units_sold <= low:
        return margin_min
    if units_sold >= high:
        return margin_max
    t = (units_sold - low) / (high - low)
    return margin_min + t * (margin_max - margin_min)


def _selling_price(cost, margin, fee, vat=0.20):
    """Calculate VAT-inclusive selling price (mirrors calculator formula)."""
    if cost is None or cost <= 0:
        return None
    denom = 1 - fee * (1 + vat)
    if denom <= 0:
        return None
    p = cost * (1 + margin) * (1 + vat) / denom
    return math.ceil(p) + 0.80


def handle_list(event, conn):
    """GET /inventory — list all stock with selling prices."""
    params = _get_query_params(event)
    link_table = _safe_table_name(os.environ.get('TABLE_NAME', 'cardrush_link'))

    query = f"""
        SELECT si.sku, si.quantity, si.total_cost_yen, si.purchased_qty,
               si.last_updated, cl.shopify_selling_price, cl.price_yen,
               cl.landed_cost_gbp, cl.cardrush_url, cl.cardrush_url_subgrade,
               cl.cardrush_stock, cl.cardrush_stock_subgrade,
               cl.price_yen_subgrade
        FROM {STOCK_TABLE} si
        LEFT JOIN {link_table} cl ON si.sku = cl.sku
    """
    conditions = []
    values = []

    set_prefix = params.get('set_prefix', '').strip()
    if set_prefix:
        conditions.append("si.sku LIKE %s")
        values.append(f"%-{set_prefix}-%")

    search = params.get('search', '').strip()
    if search:
        conditions.append("si.sku ILIKE %s")
        values.append(f"%{search}%")

    if conditions:
        query += " WHERE " + " AND ".join(conditions)
    query += " ORDER BY si.sku"

    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(query, values)
        rows = cur.fetchall()

    tier_config = _get_listing_tiers(conn)

    # Batch-query sales metrics from sales_events
    vel_window = int(os.environ.get('VELOCITY_WINDOW_DAYS', '30'))
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            "SELECT sku,"
            "       SUM(quantity) AS total_sold,"
            "       SUM(CASE WHEN created_at >= NOW() - INTERVAL '%s days'"
            "                THEN quantity ELSE 0 END) AS units_sold,"
            "       SUM(CASE WHEN created_at >= NOW() - INTERVAL '%s days'"
            "                THEN quantity * COALESCE(unit_price_gbp, 0) ELSE 0 END) AS revenue_30d,"
            "       MAX(created_at) AS last_sale_at"
            " FROM sales_events"
            " WHERE event_type = 'sale'"
            " GROUP BY sku",
            (vel_window, vel_window),
        )
        sales_map = {}
        for r in cur.fetchall():
            sales_map[r['sku']] = {
                'total_sold': int(r['total_sold']),
                'units_sold': int(r['units_sold']),
                'revenue_30d': float(r['revenue_30d'] or 0),
                'last_sale_at': r['last_sale_at'],
            }

    # Read margin/fee env vars
    margin_min = float(os.environ.get('MARGIN_MIN', '12')) / 100
    margin_max = float(os.environ.get('MARGIN_MAX', '22')) / 100
    margin_default = float(os.environ.get('MARGIN_DEFAULT', '18')) / 100
    vel_low = int(os.environ.get('VELOCITY_LOW', '1'))
    vel_high = int(os.environ.get('VELOCITY_HIGH', '8'))
    shopify_fee = float(os.environ.get('SHOPIFY_FEE', '5')) / 100
    vat_rate = float(os.environ.get('VAT_RATE', '20')) / 100

    items = []
    for row in rows:
        price = float(row['shopify_selling_price']) if row['shopify_selling_price'] else None
        price_yen = int(row['price_yen']) if row['price_yen'] else None
        cost = int(row['total_cost_yen']) if row['total_cost_yen'] else 0
        qty = int(row['quantity'])
        avg_cost = round(cost / qty) if qty > 0 and cost > 0 else 0
        listed = _listed_qty(qty, price, tier_config)

        sku = row['sku']
        sales = sales_map.get(sku, {})
        vel = sales.get('units_sold', 0)
        margin = _velocity_to_margin(vel, vel_low, vel_high,
                                     margin_min, margin_max, margin_default)

        landed = float(row['landed_cost_gbp']) if row['landed_cost_gbp'] else None
        if landed and landed > 0:
            p_low = _selling_price(landed, margin_min, shopify_fee, vat_rate)
            p_high = _selling_price(landed, margin_max, shopify_fee, vat_rate)
            price_range = f"{p_low:.2f}\u2013{p_high:.2f}" if p_low and p_high else None
        else:
            price_range = None

        items.append({
            'sku': sku,
            'quantity': qty,
            'listed_qty': listed,
            'total_cost_yen': cost,
            'purchased_qty': int(row['purchased_qty'] or 0),
            'avg_cost_yen': avg_cost,
            'selling_price_gbp': price,
            'price_yen': price_yen,
            'last_updated': row['last_updated'],
            'velocity': vel,
            'margin_pct': round(margin * 100, 1),
            'price_range_gbp': price_range,
            'total_sold': sales.get('total_sold', 0),
            'revenue_30d': round(sales.get('revenue_30d', 0), 2),
            'last_sale': sales.get('last_sale_at'),
            'cardrush_url': row['cardrush_url'],
            'cardrush_url_subgrade': row['cardrush_url_subgrade'],
            'cardrush_stock': int(row['cardrush_stock']) if row['cardrush_stock'] is not None else None,
            'cardrush_stock_subgrade': int(row['cardrush_stock_subgrade']) if row['cardrush_stock_subgrade'] is not None else None,
            'price_yen_subgrade': int(row['price_yen_subgrade']) if row['price_yen_subgrade'] is not None else None,
        })

    return _json_response(200, {'items': items, 'count': len(items)})


def handle_get_sku(sku, conn):
    """GET /inventory/{sku} — single SKU detail."""
    link_table = _safe_table_name(os.environ.get('TABLE_NAME', 'cardrush_link'))

    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            f"""SELECT si.sku, si.quantity, si.total_cost_yen, si.purchased_qty,
                       si.last_updated, cl.shopify_selling_price, cl.price_yen
                FROM {STOCK_TABLE} si
                LEFT JOIN {link_table} cl ON si.sku = cl.sku
                WHERE si.sku = %s""",
            (sku,),
        )
        row = cur.fetchone()

    if not row:
        return _json_response(404, {'error': f'SKU not found: {sku}'})

    price = float(row['shopify_selling_price']) if row['shopify_selling_price'] else None
    price_yen = int(row['price_yen']) if row['price_yen'] else None
    cost = int(row['total_cost_yen']) if row['total_cost_yen'] else 0
    qty = int(row['quantity'])
    return _json_response(200, {
        'sku': row['sku'],
        'quantity': qty,
        'total_cost_yen': cost,
        'purchased_qty': int(row['purchased_qty'] or 0),
        'avg_cost_yen': round(cost / qty) if qty > 0 and cost > 0 else 0,
        'selling_price_gbp': price,
        'price_yen': price_yen,
        'last_updated': row['last_updated'],
    })


def handle_update(event, conn):
    """POST /inventory/update — set absolute qty/cost for one SKU."""
    body = _parse_body(event)
    sku = body.get('sku', '').strip()
    if not sku:
        return _json_response(400, {'error': 'sku is required'})

    sets = []
    values = []
    if 'quantity' in body:
        qty = int(body['quantity'])
        if qty < 0:
            return _json_response(400, {'error': 'quantity must be >= 0'})
        sets.append("quantity = %s")
        values.append(qty)
    if 'total_cost_yen' in body:
        cost = int(body['total_cost_yen'])
        if cost < 0:
            return _json_response(400, {'error': 'total_cost_yen must be >= 0'})
        sets.append("total_cost_yen = %s")
        values.append(cost)

    if not sets:
        return _json_response(400, {'error': 'provide quantity and/or total_cost_yen'})

    sets.append("last_updated = %s")
    values.append(datetime.now(timezone.utc))
    values.append(sku)

    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            f"UPDATE {STOCK_TABLE} SET {', '.join(sets)} WHERE sku = %s RETURNING *",
            values,
        )
        row = cur.fetchone()
        if not row:
            conn.rollback()
            return _json_response(404, {'error': f'SKU not found: {sku}'})
        conn.commit()

    return _json_response(200, {
        'updated': {
            'sku': row['sku'],
            'quantity': int(row['quantity']),
            'total_cost_yen': int(row['total_cost_yen'] or 0),
            'purchased_qty': int(row['purchased_qty'] or 0),
            'last_updated': row['last_updated'],
        }
    })


def handle_batch_adjust(event, conn):
    """POST /inventory/batch-adjust — bulk qty delta."""
    body = _parse_body(event)
    adjustments = body.get('adjustments', [])
    if not adjustments:
        return _json_response(400, {'error': 'adjustments list is required'})

    results = []
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        for adj in adjustments:
            sku = adj.get('sku', '').strip()
            qty_delta = int(adj.get('qty_delta', 0))
            if not sku:
                continue

            cur.execute(
                f"UPDATE {STOCK_TABLE} "
                "SET quantity = GREATEST(0, quantity + %s), last_updated = %s "
                "WHERE sku = %s RETURNING sku, quantity, total_cost_yen",
                (qty_delta, datetime.now(timezone.utc), sku),
            )
            row = cur.fetchone()
            if row:
                results.append({
                    'sku': row['sku'],
                    'quantity': int(row['quantity']),
                    'delta': qty_delta,
                })
            else:
                results.append({'sku': sku, 'error': 'not found'})

        conn.commit()

    return _json_response(200, {'results': results, 'count': len(results)})


def handle_order(event, conn):
    """POST /inventory/order — record a purchase order (batch qty+cost update).

    Accepts a list of items, each with sku, quantity, unit_price_yen.
    For existing SKUs: increments quantity, total_cost_yen, purchased_qty.
    For unknown SKUs (e.g. promos): inserts a new row.
    All updates in a single transaction.
    """
    body = _parse_body(event)
    items = body.get('items', [])
    if not items:
        return _json_response(400, {'error': 'items list is required'})

    now = datetime.now(timezone.utc)
    results = []
    order_total_yen = 0

    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        for item in items:
            sku = item.get('sku', '').strip()
            qty = int(item.get('quantity', 0))
            unit_price = int(item.get('unit_price_yen', 0))
            if not sku or qty <= 0 or unit_price < 0:
                return _json_response(400, {
                    'error': f'Invalid item: sku={sku}, quantity={qty}, unit_price_yen={unit_price}'
                })

            line_cost = qty * unit_price

            # Try UPDATE existing SKU first
            cur.execute(
                f"UPDATE {STOCK_TABLE} "
                "SET quantity = quantity + %s, "
                "    total_cost_yen = total_cost_yen + %s, "
                "    purchased_qty = purchased_qty + %s, "
                "    last_updated = %s "
                "WHERE sku = %s "
                "RETURNING *",
                (qty, line_cost, qty, now, sku),
            )
            row = cur.fetchone()

            if row:
                results.append({
                    'sku': row['sku'],
                    'quantity': int(row['quantity']),
                    'total_cost_yen': int(row['total_cost_yen'] or 0),
                    'action': 'updated',
                })
            else:
                # INSERT new SKU (promo or otherwise)
                cur.execute(
                    f"INSERT INTO {STOCK_TABLE} (sku, quantity, total_cost_yen, purchased_qty, last_updated) "
                    "VALUES (%s, %s, %s, %s, %s) "
                    "RETURNING *",
                    (sku, qty, line_cost, qty, now),
                )
                row = cur.fetchone()
                results.append({
                    'sku': row['sku'],
                    'quantity': int(row['quantity']),
                    'total_cost_yen': int(row['total_cost_yen'] or 0),
                    'action': 'created',
                })

            order_total_yen += line_cost

        conn.commit()

    return _json_response(200, {
        'results': results,
        'total_items': len(results),
        'total_cost_yen': order_total_yen,
    })


def handle_promo_next(event, conn):
    """POST /inventory/promo/next — get next available promo SKU version.

    Given a card_number (e.g. "001"), finds existing OP-P-001-V*-JP SKUs
    and returns the next version.
    """
    body = _parse_body(event)
    card_number = body.get('card_number', '').strip()
    if not card_number:
        return _json_response(400, {'error': 'card_number is required'})

    # Zero-pad to 3 digits
    card_number = card_number.zfill(3)

    pattern = f'OP-P-{card_number}-V%-JP'
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT sku FROM {STOCK_TABLE} WHERE sku LIKE %s ORDER BY sku",
            (pattern,),
        )
        existing = [r[0] for r in cur.fetchall()]

    if existing:
        # Parse highest version
        max_ver = 0
        for sku in existing:
            m = re.search(r'-V(\d+)-', sku)
            if m:
                max_ver = max(max_ver, int(m.group(1)))
        next_ver = max_ver + 1
    else:
        next_ver = 1

    next_sku = f'OP-P-{card_number}-V{next_ver}-JP'
    return _json_response(200, {'next_sku': next_sku, 'existing': existing})


def handle_catalog(event, conn):
    """GET /inventory/catalog — all SKUs from cardrush_link for order form autocomplete."""
    link_table = _safe_table_name(os.environ.get('TABLE_NAME', 'cardrush_link'))

    with conn.cursor() as cur:
        cur.execute(f"SELECT sku FROM {link_table} ORDER BY sku")
        skus = [r[0] for r in cur.fetchall()]

    return _json_response(200, {'skus': skus, 'count': len(skus)})


def handle_restock(event, conn):
    """GET /inventory/restock — SKUs needing restock (qty < listing cap)."""
    params = _get_query_params(event)
    set_prefix = params.get('set_prefix', '').strip()
    link_table = _safe_table_name(os.environ.get('TABLE_NAME', 'cardrush_link'))

    sql = f"""
        SELECT si.sku, si.quantity,
               cl.shopify_selling_price, cl.price_yen, cl.cardrush_url,
               cl.cardrush_url_subgrade, cl.cardrush_stock, cl.cardrush_stock_subgrade,
               cl.price_yen_subgrade, cc.card_name, cc.rarity
        FROM {STOCK_TABLE} si
        JOIN {link_table} cl ON si.sku = cl.sku
        LEFT JOIN card_catalog cc ON cl.card_image_id = cc.card_image_id
        WHERE cl.shopify_selling_price IS NOT NULL
    """
    values = []
    if set_prefix:
        sql += " AND si.sku LIKE %s"
        values.append(f"%-{set_prefix}-%")
    sql += " ORDER BY si.sku"

    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(sql, values)
        rows = cur.fetchall()

    tier_config = _get_listing_tiers(conn)

    items = []
    for row in rows:
        qty = int(row['quantity'])
        price = float(row['shopify_selling_price'])
        cap = _listing_cap(price, tier_config)
        if cap is None or qty >= cap:
            continue

        items.append({
            'sku': row['sku'],
            'card_name': row['card_name'] or '?',
            'rarity': row['rarity'],
            'quantity': qty,
            'listing_cap': cap,
            'restock_qty': cap - qty,
            'price_yen': int(row['price_yen']) if row['price_yen'] else None,
            'selling_price_gbp': round(price, 2),
            'cardrush_url': row['cardrush_url'],
            'cardrush_url_subgrade': row['cardrush_url_subgrade'],
            'cardrush_stock': int(row['cardrush_stock']) if row['cardrush_stock'] is not None else None,
            'cardrush_stock_subgrade': int(row['cardrush_stock_subgrade']) if row['cardrush_stock_subgrade'] is not None else None,
            'price_yen_subgrade': int(row['price_yen_subgrade']) if row['price_yen_subgrade'] is not None else None,
        })

    items.sort(key=lambda x: x['restock_qty'], reverse=True)
    return _json_response(200, {'items': items, 'count': len(items)})


def handle_sales(event, conn):
    """GET /inventory/sales — list sales events with optional filters."""
    params = _get_query_params(event)

    conditions = ["event_type = 'sale'"]
    values = []

    sku = params.get('sku', '').strip()
    if sku:
        conditions.append("sku ILIKE %s")
        values.append(f"%{sku}%")

    platform = params.get('platform', '').strip().lower()
    if platform in ('shopify', 'ebay'):
        conditions.append("platform = %s")
        values.append(platform)

    days = params.get('days', '30').strip()
    try:
        days = int(days)
    except ValueError:
        days = 30
    conditions.append("created_at >= NOW() - INTERVAL '%s days'")
    values.append(days)

    where = " AND ".join(conditions)
    limit = min(int(params.get('limit', '500')), 2000)
    values.append(limit)

    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            f"SELECT platform, order_id, sku, quantity, unit_price_gbp, created_at "
            f"FROM sales_events WHERE {where} "
            f"ORDER BY created_at DESC LIMIT %s",
            values,
        )
        rows = cur.fetchall()

        # Summary stats (same WHERE, no LIMIT)
        cur.execute(
            f"SELECT COUNT(*) AS total_orders, "
            f"       COALESCE(SUM(quantity), 0) AS total_units, "
            f"       COALESCE(SUM(quantity * COALESCE(unit_price_gbp, 0)), 0) AS total_revenue "
            f"FROM sales_events WHERE {where}",
            values[:-1],  # exclude limit
        )
        summary = cur.fetchone()

    events = []
    for row in rows:
        events.append({
            'platform': row['platform'],
            'order_id': row['order_id'],
            'sku': row['sku'],
            'quantity': int(row['quantity']),
            'unit_price_gbp': float(row['unit_price_gbp']) if row['unit_price_gbp'] else None,
            'total_gbp': round(float(row['unit_price_gbp'] or 0) * int(row['quantity']), 2),
            'created_at': row['created_at'],
        })

    return _json_response(200, {
        'events': events,
        'count': len(events),
        'total_orders': int(summary['total_orders']),
        'total_units': int(summary['total_units']),
        'total_revenue': round(float(summary['total_revenue']), 2),
        'days': days,
    })


def handle_seed(event, conn):
    """POST /inventory/seed — seed missing SKUs into stock_inventory.

    Two modes:
      1. {"prefix": "OP-OP12-"} — find matching SKUs in cardrush_link, insert missing ones
      2. {"skus": ["OP-OP12-001-JP", ...]} — insert these specific SKUs if missing

    New rows get quantity=0, total_cost_yen=0, purchased_qty=0.
    """
    body = _parse_body(event)
    prefix = body.get('prefix', '').strip()
    skus_list = body.get('skus', [])

    if not prefix and not skus_list:
        return _json_response(400, {'error': 'prefix or skus list is required'})

    link_table = _safe_table_name(os.environ.get('TABLE_NAME', 'cardrush_link'))
    now = datetime.now(timezone.utc)

    with conn.cursor() as cur:
        if skus_list:
            # Direct SKU list mode — filter out any that already exist
            cur.execute(
                f"SELECT sku FROM {STOCK_TABLE} WHERE sku = ANY(%s)",
                (skus_list,),
            )
            existing = {r[0] for r in cur.fetchall()}
            missing = sorted(s for s in skus_list if s not in existing)
        else:
            # Prefix mode — find in cardrush_link
            cur.execute(
                f"SELECT cl.sku FROM {link_table} cl "
                f"LEFT JOIN {STOCK_TABLE} si ON cl.sku = si.sku "
                "WHERE cl.sku LIKE %s AND si.sku IS NULL "
                "ORDER BY cl.sku",
                (prefix + '%',),
            )
            missing = [r[0] for r in cur.fetchall()]

        if not missing:
            return _json_response(200, {'inserted': [], 'count': 0, 'message': 'No missing SKUs'})

        # Batch insert
        from psycopg2.extras import execute_batch
        execute_batch(
            cur,
            f"INSERT INTO {STOCK_TABLE} (sku, quantity, total_cost_yen, purchased_qty, last_updated) "
            "VALUES (%s, 0, 0, 0, %s)",
            [(sku, now) for sku in missing],
        )
        conn.commit()

    return _json_response(200, {'inserted': missing, 'count': len(missing)})


def _sku_to_card_number(sku):
    """Derive card_number from SKU: OP-OP12-001-JP → OP12-001, OP-OP12-SP-ST18-004-JP → OP12-SP-ST18-004."""
    # Strip prefix (OP-) and suffix (-JP)
    m = re.match(r'^[A-Z]+-(.+)-[A-Z]+$', sku)
    return m.group(1) if m else sku


def handle_catalog_seed(event, conn):
    """POST /inventory/catalog/seed — insert missing SKUs into cardrush_link.

    Accepts a list of items with sku + optional selling_price_gbp, ebay_item_number.
    Only inserts SKUs that don't already exist in cardrush_link.
    Derives card_number from SKU automatically.
    """
    body = _parse_body(event)
    items = body.get('items', [])
    if not items:
        return _json_response(400, {'error': 'items list is required'})

    link_table = _safe_table_name(os.environ.get('TABLE_NAME', 'cardrush_link'))

    with conn.cursor() as cur:
        # Find which SKUs already exist
        skus = [item['sku'] for item in items]
        cur.execute(
            f"SELECT sku FROM {link_table} WHERE sku = ANY(%s)",
            (skus,),
        )
        existing = {r[0] for r in cur.fetchall()}

        to_insert = [item for item in items if item['sku'] not in existing]
        if not to_insert:
            return _json_response(200, {'inserted': [], 'count': 0, 'message': 'All SKUs already exist'})

        from psycopg2.extras import execute_batch
        execute_batch(
            cur,
            f"INSERT INTO {link_table} "
            "(card_number, sku, shopify_selling_price, ebay_item_number_business) "
            "VALUES (%s, %s, %s, %s)",
            [
                (
                    _sku_to_card_number(item['sku']),
                    item['sku'],
                    item.get('selling_price_gbp'),
                    item.get('ebay_item_number'),
                )
                for item in to_insert
            ],
        )
        conn.commit()

    inserted_skus = [item['sku'] for item in to_insert]
    return _json_response(200, {'inserted': inserted_skus, 'count': len(inserted_skus)})


def handle_catalog_update(event, conn):
    """POST /inventory/catalog/update — batch update price_yen in cardrush_link.

    Accepts: {"items": [{"sku": "...", "price_yen": 1980}, ...]}
    Updates price_yen for existing rows in cardrush_link.
    """
    body = _parse_body(event)
    items = body.get('items', [])
    if not items:
        return _json_response(400, {'error': 'items list is required'})

    link_table = _safe_table_name(os.environ.get('TABLE_NAME', 'cardrush_link'))

    with conn.cursor() as cur:
        from psycopg2.extras import execute_batch
        execute_batch(
            cur,
            f"UPDATE {link_table} SET price_yen = %s WHERE sku = %s",
            [(item['price_yen'], item['sku']) for item in items],
        )
        updated = cur.rowcount
        conn.commit()

    return _json_response(200, {'updated': updated, 'total_sent': len(items)})


def handle_merge(event, conn):
    """POST /inventory/merge — one-time merge of stock_manual into stock_inventory.

    Merges data that diverged while admin UI wrote to stock_manual and
    webhooks/pollers reduced stock_inventory:
      1. Inserts SKUs only in stock_manual (new seeds, promos)
      2. For overlapping SKUs: adds order qty delta, takes latest cost
    """
    with conn.cursor() as cur:
        # 1. Insert SKUs only in stock_manual
        cur.execute("""
            INSERT INTO stock_inventory (sku, quantity, total_cost_yen, purchased_qty, last_updated)
            SELECT sm.sku, sm.quantity, sm.total_cost_yen, sm.purchased_qty, sm.last_updated
            FROM stock_manual sm
            WHERE NOT EXISTS (SELECT 1 FROM stock_inventory si WHERE si.sku = sm.sku)
        """)
        inserted = cur.rowcount

        # 2. For overlapping SKUs: apply order additions + keep sales reductions
        # purchased_qty delta = orders placed via admin since the table split
        cur.execute("""
            UPDATE stock_inventory si
            SET quantity = si.quantity + GREATEST(0, sm.purchased_qty - si.purchased_qty),
                total_cost_yen = sm.total_cost_yen,
                purchased_qty = sm.purchased_qty,
                last_updated = NOW()
            FROM stock_manual sm
            WHERE si.sku = sm.sku
              AND (sm.purchased_qty > si.purchased_qty OR sm.total_cost_yen > si.total_cost_yen)
        """)
        updated = cur.rowcount

        # Count unchanged (overlap with no differences)
        cur.execute("""
            SELECT COUNT(*) FROM stock_manual sm
            JOIN stock_inventory si ON si.sku = sm.sku
            WHERE NOT (sm.purchased_qty > si.purchased_qty OR sm.total_cost_yen > si.total_cost_yen)
        """)
        # After the insert, all stock_manual SKUs are now in stock_inventory,
        # so unchanged = overlap minus updated
        unchanged = cur.fetchone()[0]

        conn.commit()

    return _json_response(200, {
        'inserted': inserted,
        'updated': updated,
        'unchanged': unchanged,
        'message': f'Merged stock_manual → stock_inventory: {inserted} inserted, {updated} updated, {unchanged} unchanged',
    })


# --- Main handler ---

def lambda_handler(event, context):
    """Lambda entry point."""
    method, path = _parse_path(event)

    # CORS preflight
    if method == 'OPTIONS':
        return _json_response(200, {})

    # Auth check
    auth_error = _check_auth(event)
    if auth_error:
        return auth_error

    conn = None
    try:
        conn = get_db_connection()

        # Route: GET /inventory
        if method == 'GET' and path == '/inventory':
            return handle_list(event, conn)

        # Route: GET /inventory/catalog (must be before /{sku} catch-all)
        if method == 'GET' and path == '/inventory/catalog':
            return handle_catalog(event, conn)

        # Route: GET /inventory/restock (must be before /{sku} catch-all)
        if method == 'GET' and path == '/inventory/restock':
            return handle_restock(event, conn)

        # Route: GET /inventory/sales (must be before /{sku} catch-all)
        if method == 'GET' and path == '/inventory/sales':
            return handle_sales(event, conn)

        # Route: GET /inventory/{sku}
        if method == 'GET' and path.startswith('/inventory/'):
            sku = path[len('/inventory/'):]
            if sku:
                return handle_get_sku(sku, conn)

        # Route: POST /inventory/update
        if method == 'POST' and path == '/inventory/update':
            return handle_update(event, conn)

        # Route: POST /inventory/batch-adjust
        if method == 'POST' and path == '/inventory/batch-adjust':
            return handle_batch_adjust(event, conn)

        # Route: POST /inventory/order
        if method == 'POST' and path == '/inventory/order':
            return handle_order(event, conn)

        # Route: POST /inventory/promo/next
        if method == 'POST' and path == '/inventory/promo/next':
            return handle_promo_next(event, conn)

        # Route: POST /inventory/merge
        if method == 'POST' and path == '/inventory/merge':
            return handle_merge(event, conn)

        # Route: POST /inventory/seed
        if method == 'POST' and path == '/inventory/seed':
            return handle_seed(event, conn)

        # Route: POST /inventory/catalog/seed
        if method == 'POST' and path == '/inventory/catalog/seed':
            return handle_catalog_seed(event, conn)

        # Route: POST /inventory/catalog/update
        if method == 'POST' and path == '/inventory/catalog/update':
            return handle_catalog_update(event, conn)

        return _json_response(404, {'error': f'Not found: {method} {path}'})

    except Exception as e:
        if conn:
            try:
                conn.rollback()
            except Exception:
                pass
        return _json_response(500, {'error': str(e)})
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass
