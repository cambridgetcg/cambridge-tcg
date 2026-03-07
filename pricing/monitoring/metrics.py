"""
Shared monitoring helpers for the pricing pipeline.

- put_metric(): emit CloudWatch custom metrics to CambridgeTCG/Pipeline
- record_pipeline_run(): insert a row into pipeline_runs table
"""

import boto3
from datetime import datetime, timezone

NAMESPACE = 'CambridgeTCG/Pipeline'

_cw_client = None


def _get_cw_client():
    global _cw_client
    if _cw_client is None:
        _cw_client = boto3.client('cloudwatch')
    return _cw_client


def put_metric(name, value, unit='Count'):
    """Emit a CloudWatch custom metric to CambridgeTCG/Pipeline namespace."""
    try:
        _get_cw_client().put_metric_data(
            Namespace=NAMESPACE,
            MetricData=[{
                'MetricName': name,
                'Value': value,
                'Unit': unit,
                'Timestamp': datetime.now(timezone.utc),
            }]
        )
    except Exception as e:
        print(f"[monitoring] Failed to put metric {name}: {e}")


def put_metrics_batch(metrics):
    """
    Emit multiple CloudWatch metrics in a single API call.

    metrics: list of (name, value, unit) tuples
    CloudWatch allows up to 1000 metric data points per call.
    """
    if not metrics:
        return
    now = datetime.now(timezone.utc)
    try:
        _get_cw_client().put_metric_data(
            Namespace=NAMESPACE,
            MetricData=[{
                'MetricName': name,
                'Value': value,
                'Unit': unit,
                'Timestamp': now,
            } for name, value, unit in metrics]
        )
    except Exception as e:
        print(f"[monitoring] Failed to put {len(metrics)} metrics: {e}")


def record_pipeline_run(connection, stage, status, rows_affected=0, detail=None):
    """
    Insert a row into pipeline_runs to track Lambda execution.

    Never raises — logs and continues if the insert fails (e.g. table
    doesn't exist yet). Never blocks the calling Lambda.
    """
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO pipeline_runs (stage, status, rows_affected, detail) "
                "VALUES (%s, %s, %s, %s)",
                (stage, status, rows_affected, detail)
            )
            connection.commit()
    except Exception as e:
        print(f"[monitoring] Failed to record pipeline run for {stage}: {e}")
        try:
            connection.rollback()
        except Exception:
            pass
