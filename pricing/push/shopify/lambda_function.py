"""
Shopify Price Push Lambda (RDS-backed)

Reads SKU + shopify_selling_price from RDS (cardrush_link table),
looks up Shopify variant ID via GraphQL, updates price via REST API.

Migrated from S3 xlsx source to RDS for pipeline consolidation.
GraphQL lookup + REST price update flow is unchanged.

Architecture:
    - Python 3.12, arm64, VPC-connected (reads from RDS via Proxy)
    - GraphQL: SKU -> variant_id lookup
    - REST: variant price update
    - RateLimiter: 2 calls/sec (Shopify rate limit)
    - ThreadPoolExecutor: 5 concurrent workers

Environment Variables:
    - SHOPIFY_STORE: e.g. "yourstore.myshopify.com"
    - SHOPIFY_API_PASSWORD: Admin API access token
    - SHOPIFY_API_VERSION: e.g. "2023-04"
    - PROXY_ENDPOINT: RDS Proxy endpoint
    - DB_USER: Database username
    - DB_PASSWORD: Database password
    - DATABASE_NAME: Database name (default: op_cardrush_link)
    - TABLE_NAME: Table name (default: cardrush_link)
"""

import os
import json
import requests
import psycopg2
from psycopg2.extras import RealDictCursor
import concurrent.futures
import threading
import time
from datetime import datetime
import re
from monitoring.metrics import record_pipeline_run


def _safe_table_name(name):
    """Validate table name to prevent SQL injection."""
    if not re.match(r'^[a-zA-Z_][a-zA-Z0-9_]*$', name):
        raise ValueError(f"Invalid table name: {name}")
    return name


class RateLimiter:
    """Thread-safe rate limiter. Max calls per period (seconds)."""

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


def process_row(row_index, sku, price, shop_url, access_token, api_version, rate_limiter, session):
    debug_local = []
    # --- Step 1: Lookup variant by SKU using Shopify's GraphQL API ---
    graphql_url = f"https://{shop_url}/admin/api/{api_version}/graphql.json"
    graphql_query = f'''{{
      productVariants(first: 1, query: "sku:{sku}") {{
        edges {{
          node {{
            id
            sku
          }}
        }}
      }}
    }}'''
    headers_graphql = {
        "Content-Type": "application/json",
        "X-Shopify-Access-Token": access_token,
    }
    rate_limiter.wait()
    try:
        graphql_response = session.post(graphql_url, headers=headers_graphql, json={"query": graphql_query})
    except Exception as e:
        return {"result": {"sku": sku, "error": f"GraphQL exception: {e}"}, "debug": [f"Row {row_index}: Exception during GraphQL: {e}"]}

    if graphql_response.status_code != 200:
        error_details = f"GraphQL query failed with status {graphql_response.status_code}: {graphql_response.text}"
        return {"result": {"sku": sku, "error": error_details}, "debug": [f"Row {row_index}: {error_details}"]}

    graphql_data = graphql_response.json()
    variant_edges = graphql_data.get("data", {}).get("productVariants", {}).get("edges", [])
    if not variant_edges:
        return {"result": {"sku": sku, "error": "No variant found."}, "debug": [f"Row {row_index}: No variant found for SKU {sku}."]}

    variant_id_graphql = variant_edges[0]["node"]["id"]
    variant_numeric_id = variant_id_graphql.split("/")[-1] if variant_id_graphql.startswith("gid://") else variant_id_graphql
    debug_local.append(f"Row {row_index}: Found variant ID {variant_numeric_id} for SKU {sku}.")

    # --- Enforce rate limit before update call ---
    rate_limiter.wait()

    # --- Step 2: Update the variant price using Shopify's REST API ---
    update_url = f"https://{shop_url}/admin/api/{api_version}/variants/{variant_numeric_id}.json"
    payload = {"variant": {"id": int(variant_numeric_id), "price": str(price)}}
    update_headers = {
        "Content-Type": "application/json",
        "X-Shopify-Access-Token": access_token,
    }
    try:
        update_response = session.put(update_url, headers=update_headers, json=payload)
    except Exception as e:
        return {"result": {"sku": sku, "error": f"Update exception: {e}"}, "debug": debug_local + [f"Row {row_index}: Exception during update: {e}"]}

    if update_response.status_code not in [200, 201]:
        error_details = f"Update failed. Status: {update_response.status_code}, Response: {update_response.text}"
        return {"result": {"sku": sku, "error": error_details}, "debug": debug_local + [f"Row {row_index}: {error_details}"]}

    debug_local.append(f"Row {row_index}: Variant price updated successfully for SKU {sku}.")
    return {"result": {"sku": sku, "message": "Price updated", "variant": update_response.json()}, "debug": debug_local}


def lambda_handler(event, context):
    global_debug = []
    results = []

    dry_run = event.get('dry_run', False)

    # Shopify settings
    shop_url = os.environ.get("SHOPIFY_STORE")
    access_token = os.environ.get("SHOPIFY_API_PASSWORD")
    api_version = os.environ.get("SHOPIFY_API_VERSION", "2024-10")

    # Database settings
    table_name = _safe_table_name(os.environ.get("TABLE_NAME", "cardrush_link"))

    print("=" * 60)
    print("Shopify Price Push (RDS-backed)")
    print(f"Started: {datetime.now()}")
    print(f"Dry run: {dry_run}")
    print("=" * 60)

    global_debug.append(f"Store: {shop_url}, API version: {api_version}")
    global_debug.append(f"Reading prices from RDS table: {table_name}")

    # Rate limiter (2 updates per second — Shopify limit)
    rate_limiter = RateLimiter(max_calls=2, period=1)

    # Reuse HTTP connections
    session = requests.Session()

    connection = None

    try:
        # Connect to RDS and fetch SKU + price pairs
        print("Connecting to database...")
        connection = get_db_connection()
        print("Connected to database")

        with connection.cursor() as cursor:
            cursor.execute(f"""
                SELECT sku, shopify_selling_price
                FROM {table_name}
                WHERE shopify_selling_price IS NOT NULL
                  AND shopify_selling_price > 0
                ORDER BY sku
            """)
            rows = cursor.fetchall()

        print(f"Found {len(rows)} items with Shopify prices")
        global_debug.append(f"Found {len(rows)} items with Shopify prices in RDS")

        if dry_run:
            items = [
                {"sku": row['sku'], "price": float(row['shopify_selling_price'])}
                for row in rows
            ]
            print(f"[DRY RUN] Would push {len(items)} items to Shopify")
            return {
                "statusCode": 200,
                "body": json.dumps({
                    "dry_run": True,
                    "summary": {"total": len(items)},
                    "items": items,
                    "timestamp": datetime.now().isoformat()
                })
            }

        if not rows:
            return {
                "statusCode": 200,
                "body": json.dumps({
                    "results": [],
                    "debug": global_debug + ["No items to update"],
                    "summary": {"total": 0, "success": 0, "errors": 0}
                })
            }

        # Build task list from RDS data
        tasks = [(idx + 1, row['sku'], float(row['shopify_selling_price'])) for idx, row in enumerate(rows)]

        # Process all items concurrently
        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
            futures = [
                executor.submit(
                    process_row, row_index, sku, price,
                    shop_url, access_token, api_version, rate_limiter, session
                )
                for row_index, sku, price in tasks
            ]
            for future in concurrent.futures.as_completed(futures):
                try:
                    result = future.result()
                    results.append(result["result"])
                    global_debug.extend(result["debug"])
                except Exception as e:
                    global_debug.append(f"Exception processing a row: {e}")

        success_count = sum(1 for r in results if "message" in r)
        error_count = sum(1 for r in results if "error" in r)

        print(f"\nCompleted: {success_count} success, {error_count} errors out of {len(tasks)} items")
        global_debug.append("Completed processing all items.")

        record_pipeline_run(
            connection, 'shopify',
            'success' if error_count == 0 else 'partial',
            success_count
        )

        # Determine status code: 200=all ok, 207=partial, 500=total failure
        if error_count == 0:
            status_code = 200
        elif success_count > 0:
            status_code = 207
        else:
            status_code = 500

        return {
            "statusCode": status_code,
            "body": json.dumps({
                "results": results,
                "debug": global_debug,
                "summary": {
                    "total": len(tasks),
                    "success": success_count,
                    "errors": error_count
                }
            })
        }

    except Exception as e:
        error_msg = f"Error: {e}"
        print(f"Error: {e}")
        import traceback
        traceback.print_exc()

        if connection:
            record_pipeline_run(connection, 'shopify', 'failure', 0, str(e))

        return {
            "statusCode": 500,
            "body": json.dumps({
                "error": error_msg,
                "debug": global_debug
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
    os.environ['SHOPIFY_STORE'] = 'your-store.myshopify.com'
    os.environ['SHOPIFY_API_PASSWORD'] = 'your-token'

    result = lambda_handler({'dry_run': True}, {})
    print(json.dumps(json.loads(result['body']), indent=2))
