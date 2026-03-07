"""
Tests for the pipeline orchestrator Lambda.

Covers:
    - invoke_lambda: success, error status, function error, connection error
    - invoke_async: success (202), dispatch failure
    - lambda_handler: full success, stage failure aborts, dry_run, start_from
    - Pipeline run recording: success recorded, failure recorded

Run: pytest pricing/tests/test_orchestrator.py -v
"""

import os
import sys
import json
from unittest.mock import MagicMock, patch, call
from io import BytesIO

import pytest

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------
BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LAMBDA_DIR = os.path.join(BASE, "lambdas")

# Mock psycopg2 if not installed locally (Lambda-only dependency)
if 'psycopg2' not in sys.modules:
    sys.modules['psycopg2'] = MagicMock()
    sys.modules['psycopg2.extras'] = MagicMock()


def load_orchestrator():
    """Import lambda_function.py from the orchestrator Lambda directory."""
    path = os.path.join(LAMBDA_DIR, "pipeline-orchestrator")
    # metrics.py lives in monitoring/ — add it to path (bundled in zip at deploy)
    metrics_path = os.path.join(LAMBDA_DIR, "health-check")

    # Remove both from sys.path first
    for p in (path, metrics_path):
        while p in sys.path:
            sys.path.remove(p)

    # metrics_path first (lower priority), then orchestrator path (higher priority)
    sys.path.insert(0, metrics_path)
    sys.path.insert(0, path)

    # Clear cached modules to get fresh imports
    for mod_name in list(sys.modules.keys()):
        if mod_name in ('lambda_function', 'metrics'):
            del sys.modules[mod_name]

    import lambda_function
    # Reset cached clients between tests
    lambda_function._lambda_client = None
    lambda_function._sns_client = None
    return lambda_function


# ---------------------------------------------------------------------------
# 1. Test: invoke_lambda (sync)
# ---------------------------------------------------------------------------

class TestInvokeLambda:
    """Test the invoke_lambda helper."""

    def test_success(self):
        """Successful invocation returns parsed response."""
        mod = load_orchestrator()

        response_payload = {'statusCode': 200, 'body': '{"ok": true}'}
        mock_client = MagicMock()
        mock_client.invoke.return_value = {
            'StatusCode': 200,
            'Payload': BytesIO(json.dumps(response_payload).encode()),
        }
        mod._lambda_client = mock_client

        result = mod.invoke_lambda('test-function')
        assert result == response_payload
        mock_client.invoke.assert_called_once_with(
            FunctionName='test-function',
            InvocationType='RequestResponse',
        )

    def test_success_with_payload(self):
        """Invocation passes payload when provided."""
        mod = load_orchestrator()

        response_payload = {'statusCode': 200, 'body': '{}'}
        mock_client = MagicMock()
        mock_client.invoke.return_value = {
            'StatusCode': 200,
            'Payload': BytesIO(json.dumps(response_payload).encode()),
        }
        mod._lambda_client = mock_client

        result = mod.invoke_lambda('test-function', payload={'key': 'val'})
        assert result == response_payload
        mock_client.invoke.assert_called_once_with(
            FunctionName='test-function',
            InvocationType='RequestResponse',
            Payload='{"key": "val"}',
        )

    def test_error_status_code(self):
        """Non-200 statusCode raises RuntimeError."""
        mod = load_orchestrator()

        response_payload = {'statusCode': 500, 'body': 'internal error'}
        mock_client = MagicMock()
        mock_client.invoke.return_value = {
            'StatusCode': 200,
            'Payload': BytesIO(json.dumps(response_payload).encode()),
        }
        mod._lambda_client = mock_client

        with pytest.raises(RuntimeError, match="returned status 500"):
            mod.invoke_lambda('test-function')

    def test_function_error(self):
        """FunctionError in response raises RuntimeError."""
        mod = load_orchestrator()

        error_payload = {'errorMessage': 'something broke', 'errorType': 'RuntimeError'}
        mock_client = MagicMock()
        mock_client.invoke.return_value = {
            'StatusCode': 200,
            'FunctionError': 'Unhandled',
            'Payload': BytesIO(json.dumps(error_payload).encode()),
        }
        mod._lambda_client = mock_client

        with pytest.raises(RuntimeError, match="FunctionError"):
            mod.invoke_lambda('test-function')

    def test_connection_error(self):
        """boto3 client error propagates."""
        mod = load_orchestrator()

        mock_client = MagicMock()
        mock_client.invoke.side_effect = Exception("Connection refused")
        mod._lambda_client = mock_client

        with pytest.raises(Exception, match="Connection refused"):
            mod.invoke_lambda('test-function')


# ---------------------------------------------------------------------------
# 2. Test: invoke_async
# ---------------------------------------------------------------------------

class TestInvokeAsync:
    """Test the invoke_async helper."""

    def test_success(self):
        """Successful async dispatch returns 202."""
        mod = load_orchestrator()

        mock_client = MagicMock()
        mock_client.invoke.return_value = {'StatusCode': 202}
        mod._lambda_client = mock_client

        result = mod.invoke_async('test-function')
        assert result == 202
        mock_client.invoke.assert_called_once_with(
            FunctionName='test-function',
            InvocationType='Event',
        )

    def test_dispatch_failure(self):
        """Non-202 status raises RuntimeError."""
        mod = load_orchestrator()

        mock_client = MagicMock()
        mock_client.invoke.return_value = {'StatusCode': 400}
        mod._lambda_client = mock_client

        with pytest.raises(RuntimeError, match="expected 202"):
            mod.invoke_async('test-function')

    def test_with_payload(self):
        """Async invoke passes payload when provided."""
        mod = load_orchestrator()

        mock_client = MagicMock()
        mock_client.invoke.return_value = {'StatusCode': 202}
        mod._lambda_client = mock_client

        mod.invoke_async('test-function', payload={'key': 'val'})
        mock_client.invoke.assert_called_once_with(
            FunctionName='test-function',
            InvocationType='Event',
            Payload='{"key": "val"}',
        )


# ---------------------------------------------------------------------------
# 3. Test: lambda_handler
# ---------------------------------------------------------------------------

class TestLambdaHandler:
    """Test the main lambda_handler orchestration."""

    def _make_mock_client(self):
        """Create a mock Lambda client that returns 200 for sync, 202 for async."""
        mock_client = MagicMock()

        def mock_invoke(**kwargs):
            if kwargs.get('InvocationType') == 'Event':
                return {'StatusCode': 202}
            payload = {'statusCode': 200, 'body': json.dumps({'ok': True})}
            return {
                'StatusCode': 200,
                'Payload': BytesIO(json.dumps(payload).encode()),
            }

        mock_client.invoke.side_effect = mock_invoke
        return mock_client

    @patch.dict(os.environ, {
        'PROXY_ENDPOINT': 'test', 'DB_USER': 'test',
        'DB_PASSWORD': 'test', 'DATABASE_NAME': 'test',
        'TABLE_NAME': 'test', 'SNS_TOPIC_ARN': 'arn:aws:sns:us-east-1:123:test',
    })
    def test_full_pipeline_success(self):
        """All stages succeed → 200 with all stage names."""
        mod = load_orchestrator()
        mock_client = self._make_mock_client()
        mod._lambda_client = mock_client

        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        with patch.object(mod, 'get_db_connection', return_value=mock_conn):
            result = mod.lambda_handler({}, None)

        assert result['statusCode'] == 200
        body = json.loads(result['body'])
        assert body['status'] == 'success'
        assert 'scraper' in body['stages_completed']
        assert 'push' in body['stages_completed']

        # Check that sync stages used RequestResponse and push used Event
        invoke_calls = mock_client.invoke.call_args_list
        sync_calls = [c for c in invoke_calls if c[1].get('InvocationType') == 'RequestResponse']
        async_calls = [c for c in invoke_calls if c[1].get('InvocationType') == 'Event']
        assert len(sync_calls) == 4  # scraper, fx, calculator, optcg-scraper
        assert len(async_calls) == 2  # shopify, ebay

    @patch.dict(os.environ, {
        'PROXY_ENDPOINT': 'test', 'DB_USER': 'test',
        'DB_PASSWORD': 'test', 'DATABASE_NAME': 'test',
        'TABLE_NAME': 'test', 'SNS_TOPIC_ARN': 'arn:aws:sns:us-east-1:123:test',
    })
    def test_stage_failure_aborts(self):
        """Calculator failure aborts pipeline before push."""
        mod = load_orchestrator()

        mock_client = MagicMock()

        def mock_invoke(**kwargs):
            if kwargs.get('InvocationType') == 'Event':
                return {'StatusCode': 202}
            fn = kwargs['FunctionName']
            if fn == 'price_calculator':
                return {
                    'StatusCode': 200,
                    'FunctionError': 'Unhandled',
                    'Payload': BytesIO(json.dumps({'errorMessage': 'db error'}).encode()),
                }
            payload = {'statusCode': 200, 'body': '{}'}
            return {
                'StatusCode': 200,
                'Payload': BytesIO(json.dumps(payload).encode()),
            }

        mock_client.invoke.side_effect = mock_invoke
        mod._lambda_client = mock_client

        mock_sns = MagicMock()
        mod._sns_client = mock_sns

        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        with patch.object(mod, 'get_db_connection', return_value=mock_conn):
            result = mod.lambda_handler({}, None)

        assert result['statusCode'] == 500
        body = json.loads(result['body'])
        assert body['failed_stage'] == 'calculator'
        assert 'scraper' in body['completed_stages']
        assert 'push' not in body['completed_stages']

        # No async invocations should have been made
        async_calls = [c for c in mock_client.invoke.call_args_list
                       if c[1].get('InvocationType') == 'Event']
        assert len(async_calls) == 0

        # SNS alert should have been sent
        mock_sns.publish.assert_called_once()
        call_args = mock_sns.publish.call_args
        assert 'calculator' in call_args[1]['Subject']

    def test_dry_run(self):
        """Dry run logs stages without invoking any Lambdas."""
        mod = load_orchestrator()
        mock_client = MagicMock()
        mod._lambda_client = mock_client

        result = mod.lambda_handler({'dry_run': True}, None)

        assert result['statusCode'] == 200
        body = json.loads(result['body'])
        assert body['dry_run'] is True
        assert 'scraper' in body['stages_completed']
        assert 'push' in body['stages_completed']

        # No Lambda invocations should have been made
        mock_client.invoke.assert_not_called()

    @patch.dict(os.environ, {
        'PROXY_ENDPOINT': 'test', 'DB_USER': 'test',
        'DB_PASSWORD': 'test', 'DATABASE_NAME': 'test',
        'TABLE_NAME': 'test',
    })
    def test_start_from_skips_stages(self):
        """start_from='calculator' skips scraper and fx-updater."""
        mod = load_orchestrator()
        mock_client = self._make_mock_client()
        mod._lambda_client = mock_client

        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        with patch.object(mod, 'get_db_connection', return_value=mock_conn):
            result = mod.lambda_handler({'start_from': 'calculator'}, None)

        assert result['statusCode'] == 200
        body = json.loads(result['body'])
        assert 'scraper' not in body['stages_completed']
        assert 'fx-updater' not in body['stages_completed']
        assert 'calculator' in body['stages_completed']
        assert 'push' in body['stages_completed']

    @patch.dict(os.environ, {
        'PROXY_ENDPOINT': 'test', 'DB_USER': 'test',
        'DB_PASSWORD': 'test', 'DATABASE_NAME': 'test',
        'TABLE_NAME': 'test',
    })
    def test_start_from_push(self):
        """start_from='push' skips all sync stages, only dispatches push."""
        mod = load_orchestrator()
        mock_client = self._make_mock_client()
        mod._lambda_client = mock_client

        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        with patch.object(mod, 'get_db_connection', return_value=mock_conn):
            result = mod.lambda_handler({'start_from': 'push'}, None)

        assert result['statusCode'] == 200
        body = json.loads(result['body'])
        assert 'scraper' not in body['stages_completed']
        assert 'push' in body['stages_completed']

        # Only async invocations
        sync_calls = [c for c in mock_client.invoke.call_args_list
                      if c[1].get('InvocationType') == 'RequestResponse']
        assert len(sync_calls) == 0

    def test_start_from_invalid_stage(self):
        """Invalid start_from stage returns 400."""
        mod = load_orchestrator()

        result = mod.lambda_handler({'start_from': 'nonexistent'}, None)

        assert result['statusCode'] == 400
        body = json.loads(result['body'])
        assert 'Unknown stage' in body['error']

    @patch.dict(os.environ, {
        'PROXY_ENDPOINT': 'test', 'DB_USER': 'test',
        'DB_PASSWORD': 'test', 'DATABASE_NAME': 'test',
        'TABLE_NAME': 'test',
    })
    def test_push_dry_run(self):
        """push_dry_run runs sync stages normally, passes dry_run=true to push lambdas."""
        mod = load_orchestrator()
        mock_client = self._make_mock_client()
        mod._lambda_client = mock_client

        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        with patch.object(mod, 'get_db_connection', return_value=mock_conn):
            result = mod.lambda_handler({'push_dry_run': True}, None)

        assert result['statusCode'] == 200
        body = json.loads(result['body'])
        assert body['status'] == 'success'
        assert 'scraper' in body['stages_completed']
        assert 'push' in body['stages_completed']

        # Sync stages invoked normally (RequestResponse, no payload)
        sync_calls = [c for c in mock_client.invoke.call_args_list
                      if c[1].get('InvocationType') == 'RequestResponse']
        assert len(sync_calls) == 4  # scraper, fx, calculator, optcg-scraper
        for c in sync_calls:
            assert 'Payload' not in c[1]

        # Push stages invoked with dry_run payload
        async_calls = [c for c in mock_client.invoke.call_args_list
                       if c[1].get('InvocationType') == 'Event']
        assert len(async_calls) == 2  # shopify, ebay
        for c in async_calls:
            assert 'Payload' in c[1]
            payload = json.loads(c[1]['Payload'])
            assert payload == {'dry_run': True}


# ---------------------------------------------------------------------------
# 4. Test: Pipeline run recording
# ---------------------------------------------------------------------------

class TestPipelineRunRecording:
    """Test that pipeline runs are correctly recorded to the database."""

    @patch.dict(os.environ, {
        'PROXY_ENDPOINT': 'test', 'DB_USER': 'test',
        'DB_PASSWORD': 'test', 'DATABASE_NAME': 'test',
        'TABLE_NAME': 'test',
    })
    def test_success_recorded(self):
        """Successful pipeline records 'success' status."""
        mod = load_orchestrator()
        mock_client = MagicMock()

        def mock_invoke(**kwargs):
            if kwargs.get('InvocationType') == 'Event':
                return {'StatusCode': 202}
            payload = {'statusCode': 200, 'body': '{}'}
            return {
                'StatusCode': 200,
                'Payload': BytesIO(json.dumps(payload).encode()),
            }

        mock_client.invoke.side_effect = mock_invoke
        mod._lambda_client = mock_client

        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        with patch.object(mod, 'get_db_connection', return_value=mock_conn), \
             patch.object(mod, 'record_pipeline_run') as mock_record:
            mod.lambda_handler({}, None)

        mock_record.assert_called_once()
        args = mock_record.call_args[0]
        assert args[0] == mock_conn  # connection
        assert args[1] == 'orchestrator'  # stage
        assert args[2] == 'success'  # status

    @patch.dict(os.environ, {
        'PROXY_ENDPOINT': 'test', 'DB_USER': 'test',
        'DB_PASSWORD': 'test', 'DATABASE_NAME': 'test',
        'TABLE_NAME': 'test', 'SNS_TOPIC_ARN': 'arn:aws:sns:us-east-1:123:test',
    })
    def test_failure_recorded(self):
        """Failed pipeline records 'failed' status with error detail."""
        mod = load_orchestrator()
        mock_client = MagicMock()
        mock_client.invoke.return_value = {
            'StatusCode': 200,
            'FunctionError': 'Unhandled',
            'Payload': BytesIO(json.dumps({'errorMessage': 'crash'}).encode()),
        }
        mod._lambda_client = mock_client
        mod._sns_client = MagicMock()

        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        with patch.object(mod, 'get_db_connection', return_value=mock_conn), \
             patch.object(mod, 'record_pipeline_run') as mock_record:
            mod.lambda_handler({}, None)

        mock_record.assert_called_once()
        args = mock_record.call_args[0]
        assert args[0] == mock_conn
        assert args[1] == 'orchestrator'
        assert args[2] == 'failed'
        assert args[3] == 0  # rows_affected
        assert 'scraper' in args[4]  # detail mentions failed stage

    def test_dry_run_no_db_recording(self):
        """Dry run does not attempt database recording."""
        mod = load_orchestrator()
        mod._lambda_client = MagicMock()

        with patch.object(mod, 'get_db_connection') as mock_get_conn:
            mod.lambda_handler({'dry_run': True}, None)

        mock_get_conn.assert_not_called()


# ---------------------------------------------------------------------------
# 5. Test: SNS alerting
# ---------------------------------------------------------------------------

class TestSnsAlerting:
    """Test SNS alert behavior."""

    def test_no_topic_arn_skips_alert(self):
        """Missing SNS_TOPIC_ARN logs warning, does not crash."""
        mod = load_orchestrator()

        # Ensure no SNS_TOPIC_ARN in env
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop('SNS_TOPIC_ARN', None)
            # Should not raise
            mod.send_sns_alert("Test", "Test message")

    @patch.dict(os.environ, {'SNS_TOPIC_ARN': 'arn:aws:sns:us-east-1:123:test'})
    def test_sns_publish_failure_does_not_crash(self):
        """SNS publish error is caught and logged, not raised."""
        mod = load_orchestrator()

        mock_sns = MagicMock()
        mock_sns.publish.side_effect = Exception("SNS down")
        mod._sns_client = mock_sns

        # Should not raise
        mod.send_sns_alert("Test", "Test message")
