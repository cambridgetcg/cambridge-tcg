"""
CardRush FX Rate Updater Lambda

Fetches live GBP/JPY exchange rate from Amdoren API,
writes gbp_to_jpy to RDS cardrush_link table.

Architecture:
    - Python 3.12, arm64, VPC-connected (writes to RDS via Proxy)
    - Amdoren FX API for live exchange rates

Environment Variables:
    - PROXY_ENDPOINT: RDS Proxy endpoint
    - DB_USER: Database username
    - DB_PASSWORD: Database password
    - DATABASE_NAME: Database name (default: op_cardrush_link)
    - TABLE_NAME: Table name (default: cardrush_link)
    - AMDOREN_API_KEY: Amdoren API key
"""

import os
import re
import json
import requests
import psycopg2
from datetime import datetime
from monitoring.metrics import record_pipeline_run


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
        connect_timeout=10
    )


def fetch_gbp_to_jpy(api_key):
    """
    Fetch live GBP to JPY exchange rate from Amdoren API.

    Returns:
        float: Exchange rate (e.g., 190.24)

    Raises:
        Exception: On Amdoren API error or rate outside sanity range (50-500)
    """
    url = "https://www.amdoren.com/api/currency.php"
    params = {
        "api_key": api_key,
        "from": "GBP",
        "to": "JPY",
        "amount": 1,
    }

    response = requests.get(url, params=params)
    response.raise_for_status()

    data = response.json()

    if data.get("error") != 0:
        raise Exception(f"Amdoren API error: {data.get('error_message', 'unknown')}")

    rate = data["amount"]

    if rate < 50 or rate > 500:
        raise Exception(
            f"GBP/JPY rate {rate} outside sanity range (50-500). "
            f"Refusing to update."
        )

    return rate


def lambda_handler(event, context):
    """Fetch live GBP/JPY rate and write to RDS."""
    print("=" * 60)
    print("CardRush FX Rate Updater")
    print(f"Started: {datetime.now()}")
    print("=" * 60)

    connection = None

    try:
        table_name = _safe_table_name(os.environ.get('TABLE_NAME', 'cardrush_link'))
        api_key = os.environ.get('AMDOREN_API_KEY')
        rate_source = 'api'

        print("Fetching GBP/JPY rate from Amdoren...")
        try:
            rate = fetch_gbp_to_jpy(api_key)
        except Exception as api_err:
            print(f"Amdoren API failed: {api_err}")
            print("Falling back to last known rate from database...")
            rate_source = 'db_fallback'
            fallback_conn = get_db_connection()
            try:
                with fallback_conn.cursor() as cur:
                    cur.execute(
                        f"SELECT gbp_to_jpy FROM {table_name} "
                        "WHERE gbp_to_jpy IS NOT NULL LIMIT 1"
                    )
                    row = cur.fetchone()
                    if row and row[0]:
                        rate = float(row[0])
                        print(f"Using fallback rate from DB: {rate}")
                    else:
                        raise Exception(
                            f"Amdoren API failed ({api_err}) and no previous rate in DB"
                        )
            finally:
                fallback_conn.close()
        print(f"GBP/JPY rate: {rate} (source: {rate_source})")

        print("Connecting to database...")
        connection = get_db_connection()

        with connection.cursor() as cursor:
            cursor.execute(
                f"UPDATE {table_name} SET gbp_to_jpy = %s",
                (rate,)
            )
            updated = cursor.rowcount
            connection.commit()

        print(f"Updated gbp_to_jpy = {rate} for {updated} rows")
        record_pipeline_run(connection, 'fx-updater', 'success', updated,
                            f"rate={rate}, source={rate_source}")

        return {
            'statusCode': 200,
            'body': json.dumps({
                'success': True,
                'rate': rate,
                'rate_source': rate_source,
                'rows_updated': updated,
                'timestamp': datetime.now().isoformat()
            })
        }

    except Exception as e:
        print(f"Error: {e}")
        import traceback
        traceback.print_exc()

        if connection:
            connection.rollback()
            record_pipeline_run(connection, 'fx-updater', 'failure', 0, str(e))

        return {
            'statusCode': 500,
            'body': json.dumps({'success': False, 'error': str(e)})
        }

    finally:
        if connection:
            connection.close()
            print("Database connection closed")
