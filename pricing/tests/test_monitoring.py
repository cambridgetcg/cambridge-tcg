"""
Tests for the pipeline monitoring system.

Covers:
    - Health-check Lambda: all 9 checks (staleness x 5, zero-row, anomalies, missing, FX)
    - metrics.py: put_metric, record_pipeline_run
    - Edge cases: no pipeline_runs rows, NULL values, table not existing

Run: pytest pricing/tests/test_monitoring.py -v
"""

import os
import sys
import json
from unittest.mock import MagicMock, patch, call
from datetime import datetime, timedelta, timezone

import pytest

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------
BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LAMBDA_DIR = os.path.join(BASE, "lambdas")
PRICING_DIR = BASE  # pricing/ root — contains monitoring/

# Ensure pricing/ is on sys.path so `monitoring.metrics` is importable
if PRICING_DIR not in sys.path:
    sys.path.insert(0, PRICING_DIR)

# Mock psycopg2 if not installed locally (Lambda-only dependency)
if 'psycopg2' not in sys.modules:
    sys.modules['psycopg2'] = MagicMock()
    sys.modules['psycopg2.extras'] = MagicMock()


def load_lambda(subdir):
    """Import lambda_function.py from a specific Lambda directory."""
    path = os.path.join(LAMBDA_DIR, subdir)
    if path in sys.path:
        sys.path.remove(path)
    sys.path.insert(0, path)

    for mod_name in list(sys.modules.keys()):
        if mod_name == 'lambda_function':
            del sys.modules[mod_name]

    import lambda_function
    return lambda_function


def load_metrics():
    """Import metrics.py from the monitoring directory."""
    path = os.path.join(LAMBDA_DIR, "health-check")
    if path in sys.path:
        sys.path.remove(path)
    sys.path.insert(0, path)

    if "metrics" in sys.modules:
        del sys.modules["metrics"]

    import metrics
    return metrics


# ---------------------------------------------------------------------------
# 1. Test: record_pipeline_run
# ---------------------------------------------------------------------------

class TestRecordPipelineRun:
    """Test the record_pipeline_run helper in metrics.py."""

    def test_inserts_row_and_commits(self):
        """A successful call inserts one row and commits."""
        metrics = load_metrics()

        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        metrics.record_pipeline_run(mock_conn, 'scraper', 'success', 42)

        mock_cursor.execute.assert_called_once()
        args = mock_cursor.execute.call_args
        assert 'INSERT INTO pipeline_runs' in args[0][0]
        assert args[0][1] == ('scraper', 'success', 42, None)
        mock_conn.commit.assert_called_once()

    def test_with_detail_message(self):
        """Detail message is passed through to the INSERT."""
        metrics = load_metrics()

        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        metrics.record_pipeline_run(mock_conn, 'fx-updater', 'failure', 0, 'API timeout')

        args = mock_cursor.execute.call_args
        assert args[0][1] == ('fx-updater', 'failure', 0, 'API timeout')

    def test_never_raises_on_error(self):
        """If the INSERT fails, it logs but does not raise."""
        metrics = load_metrics()

        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_cursor.execute.side_effect = Exception("table does not exist")
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        # Should not raise
        metrics.record_pipeline_run(mock_conn, 'scraper', 'success', 10)

        # Should have tried to rollback
        mock_conn.rollback.assert_called_once()


# ---------------------------------------------------------------------------
# 2. Test: put_metric
# ---------------------------------------------------------------------------

class TestPutMetric:
    """Test the put_metric CloudWatch helper."""

    def test_emits_metric_to_cloudwatch(self):
        """put_metric calls CloudWatch put_metric_data with correct namespace."""
        metrics = load_metrics()

        mock_cw = MagicMock()
        metrics._cw_client = mock_cw

        metrics.put_metric('ScraperStaleness', 3600, 'Seconds')

        mock_cw.put_metric_data.assert_called_once()
        call_kwargs = mock_cw.put_metric_data.call_args[1]
        assert call_kwargs['Namespace'] == 'CambridgeTCG/Pipeline'
        assert call_kwargs['MetricData'][0]['MetricName'] == 'ScraperStaleness'
        assert call_kwargs['MetricData'][0]['Value'] == 3600
        assert call_kwargs['MetricData'][0]['Unit'] == 'Seconds'

        # Reset
        metrics._cw_client = None

    def test_never_raises_on_error(self):
        """If CloudWatch call fails, it logs but does not raise."""
        metrics = load_metrics()

        mock_cw = MagicMock()
        mock_cw.put_metric_data.side_effect = Exception("AccessDenied")
        metrics._cw_client = mock_cw

        # Should not raise
        metrics.put_metric('TestMetric', 1)

        # Reset
        metrics._cw_client = None


# ---------------------------------------------------------------------------
# 3. Test: Health-check Lambda — staleness checks
# ---------------------------------------------------------------------------

class TestHealthCheckStaleness:
    """Test the health-check Lambda's staleness detection."""

    def test_stage_stale_when_old(self):
        """A stage is stale when last_run > threshold_hours ago."""
        hc = load_lambda("health-check")

        mock_cursor = MagicMock()
        last_run = datetime.now(timezone.utc) - timedelta(hours=30)
        mock_cursor.fetchone.return_value = {'last_run': last_run}

        is_stale, age_seconds, detail = hc.check_staleness(mock_cursor, 'scraper', 26)

        assert is_stale is True
        assert age_seconds > 26 * 3600
        assert '30.0h ago' in detail

    def test_stage_fresh_when_recent(self):
        """A stage is fresh when last_run < threshold_hours ago."""
        hc = load_lambda("health-check")

        mock_cursor = MagicMock()
        last_run = datetime.now(timezone.utc) - timedelta(hours=2)
        mock_cursor.fetchone.return_value = {'last_run': last_run}

        is_stale, age_seconds, detail = hc.check_staleness(mock_cursor, 'scraper', 26)

        assert is_stale is False
        assert age_seconds < 26 * 3600

    def test_stage_stale_when_no_runs(self):
        """A stage with no recorded runs is always stale."""
        hc = load_lambda("health-check")

        mock_cursor = MagicMock()
        mock_cursor.fetchone.return_value = {'last_run': None}

        is_stale, age_seconds, detail = hc.check_staleness(mock_cursor, 'scraper', 26)

        assert is_stale is True
        assert age_seconds is None
        assert 'No runs recorded' in detail


# ---------------------------------------------------------------------------
# 4. Test: Health-check Lambda — zero-row check
# ---------------------------------------------------------------------------

class TestHealthCheckZeroRows:
    """Test the zero-row scraper update check."""

    def test_detects_zero_row_update(self):
        hc = load_lambda("health-check")

        mock_cursor = MagicMock()
        mock_cursor.fetchone.return_value = {
            'rows_affected': 0,
            'completed_at': datetime.now(timezone.utc)
        }

        is_zero, detail = hc.check_zero_rows(mock_cursor)

        assert is_zero is True
        assert 'affected 0 rows' in detail

    def test_passes_with_nonzero_rows(self):
        hc = load_lambda("health-check")

        mock_cursor = MagicMock()
        mock_cursor.fetchone.return_value = {
            'rows_affected': 42,
            'completed_at': datetime.now(timezone.utc)
        }

        is_zero, detail = hc.check_zero_rows(mock_cursor)

        assert is_zero is False
        assert '42 rows' in detail

    def test_no_scraper_runs_recorded(self):
        hc = load_lambda("health-check")

        mock_cursor = MagicMock()
        mock_cursor.fetchone.return_value = None

        is_zero, detail = hc.check_zero_rows(mock_cursor)

        assert is_zero is False
        assert 'No scraper runs' in detail


# ---------------------------------------------------------------------------
# 5. Test: Health-check Lambda — price anomalies
# ---------------------------------------------------------------------------

class TestHealthCheckPriceAnomalies:
    """Test price anomaly detection."""

    def test_detects_anomalous_prices(self):
        hc = load_lambda("health-check")

        mock_cursor = MagicMock()
        mock_cursor.fetchone.return_value = {'count': 3}

        count, detail = hc.check_price_anomalies(mock_cursor, 'cardrush_link')

        assert count == 3
        assert '3 products' in detail

    def test_passes_when_all_normal(self):
        hc = load_lambda("health-check")

        mock_cursor = MagicMock()
        mock_cursor.fetchone.return_value = {'count': 0}

        count, detail = hc.check_price_anomalies(mock_cursor, 'cardrush_link')

        assert count == 0
        assert 'within expected range' in detail


# ---------------------------------------------------------------------------
# 6. Test: Health-check Lambda — missing prices
# ---------------------------------------------------------------------------

class TestHealthCheckMissingPrices:
    """Test missing price detection."""

    def test_detects_missing_selling_prices(self):
        hc = load_lambda("health-check")

        mock_cursor = MagicMock()
        mock_cursor.fetchone.return_value = {'count': 5}

        count, detail = hc.check_missing_prices(mock_cursor, 'cardrush_link')

        assert count == 5
        assert '5 products' in detail

    def test_passes_when_all_priced(self):
        hc = load_lambda("health-check")

        mock_cursor = MagicMock()
        mock_cursor.fetchone.return_value = {'count': 0}

        count, detail = hc.check_missing_prices(mock_cursor, 'cardrush_link')

        assert count == 0


# ---------------------------------------------------------------------------
# 7. Test: Health-check Lambda — FX rate sanity
# ---------------------------------------------------------------------------

class TestHealthCheckFxRate:
    """Test FX rate sanity check."""

    def test_detects_anomalous_rate(self):
        hc = load_lambda("health-check")

        mock_cursor = MagicMock()
        mock_cursor.fetchall.return_value = [{'gbp_to_jpy': 50.0}]

        is_anomalous, rates, detail = hc.check_fx_rate_sanity(mock_cursor, 'cardrush_link')

        assert is_anomalous is True
        assert 50.0 in rates

    def test_passes_with_normal_rate(self):
        hc = load_lambda("health-check")

        mock_cursor = MagicMock()
        mock_cursor.fetchall.return_value = [{'gbp_to_jpy': 190.0}]

        is_anomalous, rates, detail = hc.check_fx_rate_sanity(mock_cursor, 'cardrush_link')

        assert is_anomalous is False
        assert 190.0 in rates

    def test_stale_when_no_rates(self):
        hc = load_lambda("health-check")

        mock_cursor = MagicMock()
        mock_cursor.fetchall.return_value = []

        is_anomalous, rates, detail = hc.check_fx_rate_sanity(mock_cursor, 'cardrush_link')

        assert is_anomalous is True
        assert 'No FX rates' in detail


# ---------------------------------------------------------------------------
# 8. Test: Health-check Lambda handler — full integration with mocked DB
# ---------------------------------------------------------------------------

class TestHealthCheckHandler:
    """Test the full lambda_handler with mocked database."""

    def test_all_checks_pass(self):
        """When everything is healthy, all checks pass."""
        hc = load_lambda("health-check")

        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        recent = datetime.now(timezone.utc) - timedelta(hours=1)

        call_count = [0]
        def mock_fetchone():
            call_count[0] += 1
            # Calls 1-5: staleness checks (one per stage)
            if call_count[0] <= 5:
                return {'last_run': recent}
            # Call 6: zero-row check
            elif call_count[0] == 6:
                return {'rows_affected': 42, 'completed_at': recent}
            # Calls 7-8: price anomalies + missing prices
            else:
                return {'count': 0}

        mock_cursor.fetchone.side_effect = mock_fetchone
        # FX rate sanity: normal rate
        mock_cursor.fetchall.return_value = [{'gbp_to_jpy': 190.0}]

        env = {
            'PROXY_ENDPOINT': 'mock',
            'DB_USER': 'mock',
            'DB_PASSWORD': 'mock',
            'TABLE_NAME': 'cardrush_link',
        }

        with patch.dict(os.environ, env), \
             patch.object(hc, 'get_db_connection', return_value=mock_conn), \
             patch.object(hc, 'put_metrics_batch'):
            result = hc.lambda_handler({}, {})

        body = json.loads(result['body'])
        assert result['statusCode'] == 200
        assert body['checks_failed'] == 0
        assert body['checks_passed'] == body['total_checks']

    def test_stale_scraper_detected(self):
        """When scraper is stale, it appears in failures."""
        hc = load_lambda("health-check")

        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        recent = datetime.now(timezone.utc) - timedelta(hours=1)
        stale = datetime.now(timezone.utc) - timedelta(hours=30)

        call_count = [0]
        def mock_fetchone():
            call_count[0] += 1
            # Call 1: scraper staleness - stale
            if call_count[0] == 1:
                return {'last_run': stale}
            # Calls 2-5: other stages - fresh
            elif call_count[0] <= 5:
                return {'last_run': recent}
            # Call 6: zero-row check
            elif call_count[0] == 6:
                return {'rows_affected': 42, 'completed_at': recent}
            else:
                return {'count': 0}

        mock_cursor.fetchone.side_effect = mock_fetchone
        mock_cursor.fetchall.return_value = [{'gbp_to_jpy': 190.0}]

        env = {
            'PROXY_ENDPOINT': 'mock',
            'DB_USER': 'mock',
            'DB_PASSWORD': 'mock',
            'TABLE_NAME': 'cardrush_link',
        }

        with patch.dict(os.environ, env), \
             patch.object(hc, 'get_db_connection', return_value=mock_conn), \
             patch.object(hc, 'put_metrics_batch'):
            result = hc.lambda_handler({}, {})

        body = json.loads(result['body'])
        assert body['checks_failed'] == 1
        assert any(f['check'] == 'scraper_staleness' for f in body['failures'])


# ---------------------------------------------------------------------------
# 9. Test: monitoring.metrics is importable from pricing/ root
# ---------------------------------------------------------------------------

class TestMonitoringImportable:
    """Verify that monitoring.metrics is importable when pricing/ is on sys.path."""

    def test_import_record_pipeline_run(self):
        """monitoring.metrics.record_pipeline_run is callable."""
        from monitoring.metrics import record_pipeline_run
        assert callable(record_pipeline_run)

    def test_import_put_metric(self):
        """monitoring.metrics.put_metric is callable."""
        from monitoring.metrics import put_metric
        assert callable(put_metric)

    def test_record_pipeline_run_present_in_all_lambdas(self):
        """All 5 existing Lambda files contain record_pipeline_run calls."""
        lambda_files = {
            'scraper-cardrush': 'scraper',
            'cardrush-fx-updater': 'fx-updater',
            'price-calculator': 'calculator',
            'api-shopify': 'shopify',
            'api-ebay': 'ebay',
        }

        for subdir, stage in lambda_files.items():
            source_path = os.path.join(LAMBDA_DIR, subdir, 'lambda_function.py')
            resolved = os.path.realpath(source_path)
            with open(resolved, 'r') as f:
                source = f.read()
            assert 'record_pipeline_run' in source, (
                f"{subdir} does not contain record_pipeline_run"
            )
            assert f"'{stage}'" in source, (
                f"{subdir} does not record stage '{stage}'"
            )
