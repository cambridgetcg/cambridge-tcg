"""
Pipeline Health-Check Lambda

Runs on EventBridge schedule (every 30 min). Queries RDS to detect
staleness, zero-row updates, price anomalies, and FX rate drift.
Emits CloudWatch custom metrics to CambridgeTCG/Pipeline namespace.

Checks:
    1. Scraper staleness       — last run > 26h ago
    2. FX-updater staleness    — last run > 26h ago
    3. Calculator staleness    — last run > 26h ago
    4. Shopify push staleness  — last run > 26h ago
    5. eBay push staleness     — last run > 26h ago
    6. Zero-row scraper update — last scraper run affected 0 rows
    7. Price anomalies         — prices outside 1.80–500 range
    8. Missing prices          — price_yen present but no selling price
    9. FX rate sanity          — gbp_to_jpy outside 100–300

Environment Variables:
    - PROXY_ENDPOINT: RDS Proxy endpoint
    - DB_USER: Database username
    - DB_PASSWORD: Database password
    - DATABASE_NAME: Database name (default: op_cardrush_link)
    - TABLE_NAME: Table name (default: cardrush_link)
    - STALENESS_HOURS: Hours before a stage is considered stale (default: 26)
"""

import os
import re
import json
import psycopg2
from psycopg2.extras import RealDictCursor
from datetime import datetime, timedelta, timezone

from metrics import put_metrics_batch

# Staleness threshold in hours (26h = daily pipeline + 2h buffer)
DEFAULT_STALENESS_HOURS = 26

STAGES = ['scraper', 'fx-updater', 'calculator', 'shopify', 'ebay']


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


def check_staleness(cursor, stage, threshold_hours):
    """
    Check if a pipeline stage hasn't run within threshold_hours.

    Returns (is_stale: bool, seconds_since_last_run: float or None, detail: str)
    """
    cursor.execute(
        "SELECT MAX(completed_at) as last_run FROM pipeline_runs WHERE stage = %s",
        (stage,)
    )
    row = cursor.fetchone()
    last_run = row['last_run'] if row else None

    if last_run is None:
        return True, None, f"No runs recorded for {stage}"

    age = datetime.now(timezone.utc) - last_run
    age_seconds = age.total_seconds()
    threshold_seconds = threshold_hours * 3600

    if age_seconds > threshold_seconds:
        hours_ago = age_seconds / 3600
        return True, age_seconds, f"Last run {hours_ago:.1f}h ago (threshold: {threshold_hours}h)"

    return False, age_seconds, f"Last run {age_seconds / 3600:.1f}h ago"


def check_zero_rows(cursor):
    """
    Check if the most recent scraper run affected 0 rows.

    Returns (is_zero: bool, detail: str)
    """
    cursor.execute(
        "SELECT rows_affected, completed_at FROM pipeline_runs "
        "WHERE stage = 'scraper' ORDER BY completed_at DESC LIMIT 1"
    )
    row = cursor.fetchone()

    if row is None:
        return False, "No scraper runs recorded"

    if row['rows_affected'] == 0:
        return True, f"Last scraper run at {row['completed_at']} affected 0 rows"

    return False, f"Last scraper run affected {row['rows_affected']} rows"


def check_price_anomalies(cursor, table_name):
    """
    Check for prices outside the expected 1.80–500 range.

    Returns (count: int, detail: str)
    """
    cursor.execute(
        f"SELECT COUNT(*) as count FROM {table_name} "
        f"WHERE shopify_selling_price < 1.80 OR shopify_selling_price > 500"
    )
    count = cursor.fetchone()['count']

    if count > 0:
        return count, f"{count} products with price outside 1.80–500 range"

    return 0, "All prices within expected range"


def check_missing_prices(cursor, table_name):
    """
    Check for products with price_yen but no shopify_selling_price.

    Returns (count: int, detail: str)
    """
    cursor.execute(
        f"SELECT COUNT(*) as count FROM {table_name} "
        f"WHERE price_yen IS NOT NULL AND shopify_selling_price IS NULL"
    )
    count = cursor.fetchone()['count']

    if count > 0:
        return count, f"{count} products with price_yen but no selling price"

    return 0, "All priced products have selling prices"


def check_fx_rate_sanity(cursor, table_name):
    """
    Check that gbp_to_jpy is within 100–300 (tighter than Lambda's 50–500 guard).

    Returns (is_anomalous: bool, rates: list, detail: str)
    """
    cursor.execute(
        f"SELECT DISTINCT gbp_to_jpy FROM {table_name} "
        f"WHERE gbp_to_jpy IS NOT NULL"
    )
    rows = cursor.fetchall()
    rates = [float(r['gbp_to_jpy']) for r in rows]

    if not rates:
        return True, [], "No FX rates found in table"

    anomalous = [r for r in rates if r < 100 or r > 300]
    if anomalous:
        return True, anomalous, f"FX rate(s) outside 100–300: {anomalous}"

    return False, rates, f"FX rate(s) within range: {rates}"


def lambda_handler(event, context):
    """Run all health checks and emit CloudWatch metrics."""
    print("=" * 60)
    print("Pipeline Health Check")
    print(f"Started: {datetime.now(timezone.utc).isoformat()}")
    print("=" * 60)

    staleness_hours = int(os.environ.get('STALENESS_HOURS', DEFAULT_STALENESS_HOURS))
    table_name = _safe_table_name(os.environ.get('TABLE_NAME', 'cardrush_link'))

    connection = None
    failures = []
    checks_passed = 0
    total_checks = 0
    pending_metrics = []  # Collect all metrics, emit in one batch at the end

    try:
        connection = get_db_connection()

        with connection.cursor() as cursor:
            # --- Staleness checks (5 stages) ---
            metric_names = {
                'scraper': 'ScraperStaleness',
                'fx-updater': 'FxStaleness',
                'calculator': 'CalculatorStaleness',
                'shopify': 'ShopifyStaleness',
                'ebay': 'EbayStaleness',
            }

            for stage in STAGES:
                total_checks += 1
                is_stale, age_seconds, detail = check_staleness(cursor, stage, staleness_hours)

                metric_value = age_seconds if age_seconds is not None else staleness_hours * 3600 + 1
                pending_metrics.append((metric_names[stage], metric_value, 'Seconds'))

                if is_stale:
                    failures.append({'check': f'{stage}_staleness', 'detail': detail})
                    print(f"FAIL: {stage} staleness — {detail}")
                else:
                    checks_passed += 1
                    print(f"OK:   {stage} staleness — {detail}")

            # --- Zero-row update check ---
            total_checks += 1
            is_zero, detail = check_zero_rows(cursor)
            pending_metrics.append(('ZeroRowUpdate', 1 if is_zero else 0, 'Count'))

            if is_zero:
                failures.append({'check': 'zero_row_update', 'detail': detail})
                print(f"FAIL: zero row update — {detail}")
            else:
                checks_passed += 1
                print(f"OK:   zero row update — {detail}")

            # --- Price anomalies ---
            total_checks += 1
            anomaly_count, detail = check_price_anomalies(cursor, table_name)
            pending_metrics.append(('PriceAnomalies', anomaly_count, 'Count'))

            if anomaly_count > 0:
                failures.append({'check': 'price_anomalies', 'detail': detail})
                print(f"FAIL: price anomalies — {detail}")
            else:
                checks_passed += 1
                print(f"OK:   price anomalies — {detail}")

            # --- Missing prices ---
            total_checks += 1
            missing_count, detail = check_missing_prices(cursor, table_name)
            pending_metrics.append(('MissingPrices', missing_count, 'Count'))

            if missing_count > 0:
                failures.append({'check': 'missing_prices', 'detail': detail})
                print(f"FAIL: missing prices — {detail}")
            else:
                checks_passed += 1
                print(f"OK:   missing prices — {detail}")

            # --- FX rate sanity ---
            total_checks += 1
            is_anomalous, rates, detail = check_fx_rate_sanity(cursor, table_name)
            pending_metrics.append(('FxRateAnomaly', 1 if is_anomalous else 0, 'Count'))

            if is_anomalous:
                failures.append({'check': 'fx_rate_sanity', 'detail': detail})
                print(f"FAIL: FX rate sanity — {detail}")
            else:
                checks_passed += 1
                print(f"OK:   FX rate sanity — {detail}")

        # --- Emit all metrics in a single API call ---
        print(f"\nEmitting {len(pending_metrics)} metrics to CloudWatch...")
        put_metrics_batch(pending_metrics)

        # --- Summary ---
        checks_failed = len(failures)
        print(f"Summary: {checks_passed}/{total_checks} passed, {checks_failed} failed")

        if failures:
            print(f"Failures: {json.dumps(failures)}")

        return {
            'statusCode': 200,
            'body': json.dumps({
                'checks_passed': checks_passed,
                'checks_failed': checks_failed,
                'total_checks': total_checks,
                'failures': failures,
                'timestamp': datetime.now(timezone.utc).isoformat(),
            })
        }

    except Exception as e:
        print(f"Health check error: {e}")
        import traceback
        traceback.print_exc()

        return {
            'statusCode': 500,
            'body': json.dumps({
                'error': str(e),
                'timestamp': datetime.now(timezone.utc).isoformat(),
            })
        }

    finally:
        if connection:
            connection.close()
            print("Database connection closed")
