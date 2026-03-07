"""
Pipeline Orchestrator Lambda

Invokes the pricing pipeline Lambdas in order:
    scraper → fx-updater → calculator → optcg-scraper → [shopify + ebay async]

The first four stages run synchronously (each validates the previous).
The OPTCG scraper runs after the JP calculator (independent pipeline —
uses its own Amdoren USD→GBP FX call, not the fx-updater).
Push Lambdas are fired asynchronously (Event type) because they take
5-10 min each and sync invocation through VPC endpoints is unreliable
at that duration. Health-check runs on its own EventBridge schedule.

Triggered daily by EventBridge at 06:00 UTC.

Event parameters:
    - dry_run (bool): log stages without invoking (default: false)
    - start_from (str): skip stages before this one (e.g. "calculator")

Environment Variables:
    - PROXY_ENDPOINT, DB_USER, DB_PASSWORD, DATABASE_NAME, TABLE_NAME: RDS connection
    - SNS_TOPIC_ARN: alert topic for failures
"""

import os
import re
import json
import time
from datetime import datetime

import boto3
from botocore.config import Config

from metrics import record_pipeline_run

# boto3 default read timeout is 60s — extended for slower Lambdas
LAMBDA_CLIENT_CONFIG = Config(read_timeout=300, retries={'max_attempts': 0})

# Stages invoked synchronously (RequestResponse) — each must succeed before the next
SYNC_STAGES = [
    {"name": "scraper", "function_name": "cardrush_scraper"},
    {"name": "fx-updater", "function_name": "get_GBP-JPY"},
    {"name": "calculator", "function_name": "price_calculator"},
    {"name": "optcg-scraper", "function_name": "optcg-scraper"},
]

# Stages invoked asynchronously (Event) — fire-and-forget after sync stages succeed
ASYNC_STAGES = [
    {"name": "shopify-push", "function_name": "shopify-price-push"},
    {"name": "ebay-push", "function_name": "ebay-price-push"},
]

# All stage names in order (for start_from validation)
ALL_STAGE_NAMES = [s['name'] for s in SYNC_STAGES] + ['push']

_lambda_client = None
_sns_client = None


def _get_lambda_client():
    global _lambda_client
    if _lambda_client is None:
        _lambda_client = boto3.client('lambda', config=LAMBDA_CLIENT_CONFIG)
    return _lambda_client


def _get_sns_client():
    global _sns_client
    if _sns_client is None:
        _sns_client = boto3.client('sns')
    return _sns_client


def _safe_table_name(name):
    """Validate table name to prevent SQL injection."""
    if not re.match(r'^[a-zA-Z_][a-zA-Z0-9_]*$', name):
        raise ValueError(f"Invalid table name: {name}")
    return name


def get_db_connection():
    """Connect to database through RDS Proxy."""
    import psycopg2
    return psycopg2.connect(
        host=os.environ['PROXY_ENDPOINT'],
        database=os.environ.get('DATABASE_NAME', 'op_cardrush_link'),
        user=os.environ['DB_USER'],
        password=os.environ['DB_PASSWORD'],
        port=int(os.environ.get('DB_PORT', 5432)),
        connect_timeout=10
    )


def invoke_lambda(function_name, payload=None):
    """
    Invoke a Lambda synchronously and return parsed response.

    Raises RuntimeError if the invocation fails or returns non-200.
    """
    client = _get_lambda_client()

    invoke_args = {
        'FunctionName': function_name,
        'InvocationType': 'RequestResponse',
    }
    if payload is not None:
        invoke_args['Payload'] = json.dumps(payload)

    response = client.invoke(**invoke_args)

    # Check for Lambda-level errors (function crashed)
    if 'FunctionError' in response:
        error_payload = json.loads(response['Payload'].read())
        raise RuntimeError(
            f"Lambda {function_name} returned FunctionError: {error_payload}"
        )

    # Parse response payload
    response_payload = json.loads(response['Payload'].read())

    # Check for application-level errors (non-200 statusCode)
    status_code = response_payload.get('statusCode', 200)
    if status_code != 200:
        raise RuntimeError(
            f"Lambda {function_name} returned status {status_code}: "
            f"{response_payload.get('body', 'no body')}"
        )

    return response_payload


def invoke_async(function_name, payload=None):
    """
    Invoke a Lambda asynchronously (fire-and-forget).

    Returns the StatusCode from the invoke response (202 = accepted).
    Raises RuntimeError if the async dispatch itself fails.
    """
    client = _get_lambda_client()

    invoke_args = {
        'FunctionName': function_name,
        'InvocationType': 'Event',
    }
    if payload is not None:
        invoke_args['Payload'] = json.dumps(payload)

    response = client.invoke(**invoke_args)
    status = response.get('StatusCode', 0)

    if status != 202:
        raise RuntimeError(
            f"Async invoke of {function_name} returned status {status} (expected 202)"
        )

    return status


def send_sns_alert(subject, message):
    """Send an SNS alert. Logs and continues on failure."""
    topic_arn = os.environ.get('SNS_TOPIC_ARN')
    if not topic_arn:
        print(f"[orchestrator] No SNS_TOPIC_ARN set, skipping alert: {subject}")
        return

    try:
        _get_sns_client().publish(
            TopicArn=topic_arn,
            Subject=subject[:100],  # SNS subject max 100 chars
            Message=message,
        )
        print(f"[orchestrator] SNS alert sent: {subject}")
    except Exception as e:
        print(f"[orchestrator] Failed to send SNS alert: {e}")


def lambda_handler(event, context):
    """
    Run the pricing pipeline: sync stages then async push.

    Event params:
        dry_run (bool): log without invoking any stages
        start_from (str): skip stages before this name
        push_dry_run (bool): run sync stages normally but invoke push
            lambdas with dry_run=true (preview what would be pushed)
    """
    dry_run = event.get('dry_run', False)
    start_from = event.get('start_from')
    push_dry_run = event.get('push_dry_run', False)

    print("=" * 60)
    print("Pipeline Orchestrator")
    print(f"Started: {datetime.now(timezone.utc).isoformat()}")
    print(f"Dry run: {dry_run}")
    if push_dry_run:
        print(f"Push dry run: {push_dry_run}")
    if start_from:
        print(f"Starting from: {start_from}")
    print("=" * 60)

    # Validate start_from
    if start_from and start_from not in ALL_STAGE_NAMES:
        return {
            'statusCode': 400,
            'body': json.dumps({
                'error': f"Unknown stage: {start_from}",
                'valid_stages': ALL_STAGE_NAMES,
            })
        }

    # Determine which sync stages to run
    sync_to_run = SYNC_STAGES
    skip_push = False
    if start_from:
        if start_from == 'push':
            sync_to_run = []
        else:
            found = False
            filtered = []
            for stage in SYNC_STAGES:
                if stage['name'] == start_from:
                    found = True
                if found:
                    filtered.append(stage)
            sync_to_run = filtered

    results = {}
    failed_stage = None
    error_detail = None
    start_time = time.time()

    # --- Phase 1: Sync stages (scraper → fx → calculator) ---
    for stage in sync_to_run:
        stage_name = stage['name']
        function_name = stage['function_name']
        print(f"\n--- Stage: {stage_name} ({function_name}) ---")

        if dry_run:
            print(f"  [DRY RUN] Would invoke: {function_name}")
            results[stage_name] = 'dry_run'
            continue

        try:
            response = invoke_lambda(function_name)
            results[stage_name] = response
            status = response.get('statusCode', '?')
            print(f"  OK: status={status}")
        except Exception as e:
            failed_stage = stage_name
            error_detail = f"{type(e).__name__}: {e}" if not isinstance(e, RuntimeError) else str(e)
            print(f"  FAIL: {error_detail}")
            break

    # --- Phase 2: Async push (fire-and-forget) ---
    if not failed_stage:
        sub_names = [s['name'] for s in ASYNC_STAGES]
        print(f"\n--- Stage: push (async: {sub_names}) ---")

        if dry_run:
            print(f"  [DRY RUN] Would async invoke: {sub_names}")
            results['push'] = {s['name']: 'dry_run' for s in ASYNC_STAGES}
        else:
            async_results = {}
            push_payload = {'dry_run': True} if push_dry_run else None
            for stage in ASYNC_STAGES:
                try:
                    status = invoke_async(stage['function_name'], payload=push_payload)
                    async_results[stage['name']] = f"dispatched (status={status})"
                    print(f"  {stage['name']}: dispatched (status={status})")
                except Exception as e:
                    async_results[stage['name']] = f"dispatch failed: {e}"
                    print(f"  {stage['name']}: dispatch FAILED: {e}")

            results['push'] = async_results
            # Push dispatch failures are logged but don't abort the pipeline
            # (the health-check will catch if push didn't actually complete)

    elapsed = round(time.time() - start_time, 1)

    # Record to pipeline_runs
    if not dry_run:
        connection = None
        try:
            connection = get_db_connection()
            if failed_stage:
                record_pipeline_run(
                    connection, 'orchestrator', 'failed', 0,
                    f"Failed at {failed_stage}: {error_detail}"[:500]
                )
            else:
                stages_run = len(sync_to_run) + 1  # sync stages + push dispatch
                record_pipeline_run(
                    connection, 'orchestrator', 'success', stages_run,
                    f"Completed {stages_run} stages in {elapsed}s (push dispatched async)"
                )
        except Exception as e:
            print(f"[orchestrator] Failed to record pipeline run: {e}")
        finally:
            if connection:
                try:
                    connection.close()
                except Exception:
                    pass

    # Alert on failure
    if failed_stage and not dry_run:
        send_sns_alert(
            f"Pipeline FAILED at {failed_stage}",
            f"Pipeline orchestrator failed at stage '{failed_stage}'.\n\n"
            f"Error: {error_detail}\n\n"
            f"Elapsed: {elapsed}s\n"
            f"Completed stages: {list(results.keys())}\n"
            f"Timestamp: {datetime.now(timezone.utc).isoformat()}"
        )

    # Summary
    print(f"\n{'=' * 60}")
    if failed_stage:
        print(f"FAILED at stage: {failed_stage}")
        print(f"Error: {error_detail}")
    else:
        print(f"All stages completed successfully")
    print(f"Elapsed: {elapsed}s")
    print(f"Stages run: {list(results.keys())}")
    print("=" * 60)

    if failed_stage:
        return {
            'statusCode': 500,
            'body': json.dumps({
                'status': 'failed',
                'failed_stage': failed_stage,
                'error': error_detail,
                'completed_stages': list(results.keys()),
                'elapsed_seconds': elapsed,
                'timestamp': datetime.now(timezone.utc).isoformat(),
            })
        }

    return {
        'statusCode': 200,
        'body': json.dumps({
            'status': 'success',
            'stages_completed': list(results.keys()),
            'elapsed_seconds': elapsed,
            'dry_run': dry_run,
            'timestamp': datetime.now(timezone.utc).isoformat(),
        })
    }
