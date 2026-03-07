"""
Tests for the eBay Browse API competitor price monitor.

Mocks: RDS (psycopg2), HTTP (requests), Secrets Manager, SNS, boto3
Run: python -m pytest pricing/tests/test_browse_monitor.py -v
"""

import os
import sys
import json
from unittest import mock
from unittest.mock import MagicMock, patch, call
from datetime import datetime

import pytest

# ---------------------------------------------------------------------------
# Paths — add Lambda directory to sys.path so imports work
# ---------------------------------------------------------------------------
BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LAMBDA_DIR = os.path.join(BASE, "lambdas")
PRICING_DIR = BASE

if PRICING_DIR not in sys.path:
    sys.path.insert(0, PRICING_DIR)

# Mock Lambda-only dependencies if not installed locally
for _mod in ['psycopg2', 'psycopg2.extras', 'boto3', 'dotenv']:
    if _mod not in sys.modules:
        sys.modules[_mod] = MagicMock()


def load_lambda(subdir):
    """Import lambda_function.py from a specific Lambda directory."""
    path = os.path.join(LAMBDA_DIR, subdir)
    if path in sys.path:
        sys.path.remove(path)
    sys.path.insert(0, path)

    # Also make browse_client importable
    if "lambda_function" in sys.modules:
        del sys.modules["lambda_function"]
    if "browse_client" in sys.modules:
        del sys.modules["browse_client"]

    import lambda_function
    return lambda_function


def load_browse_client():
    """Import browse_client from the browse Lambda directory."""
    path = os.path.join(LAMBDA_DIR, "browse-monitor")
    if path in sys.path:
        sys.path.remove(path)
    sys.path.insert(0, path)

    if "browse_client" in sys.modules:
        del sys.modules["browse_client"]

    import browse_client
    return browse_client


# ---------------------------------------------------------------------------
# Test data
# ---------------------------------------------------------------------------

SAMPLE_BROWSE_RESPONSE = {
    "total": 3,
    "itemSummaries": [
        {
            "itemId": "v1|111111|0",
            "title": "One Piece OP01-062 Nami Japanese",
            "price": {"value": "3.50", "currency": "GBP"},
            "seller": {"username": "seller_a"},
            "itemWebUrl": "https://www.ebay.co.uk/itm/111111",
        },
        {
            "itemId": "v1|222222|0",
            "title": "OP01-062 Nami JP Card",
            "price": {"value": "4.20", "currency": "GBP"},
            "seller": {"username": "seller_b"},
            "itemWebUrl": "https://www.ebay.co.uk/itm/222222",
        },
        {
            "itemId": "v1|333333|0",
            "title": "One Piece Card Game OP01-062",
            "price": {"value": "5.80", "currency": "GBP"},
            "seller": {"username": "seller_c"},
            "itemWebUrl": "https://www.ebay.co.uk/itm/333333",
        },
    ]
}

EMPTY_BROWSE_RESPONSE = {"total": 0}


# ---------------------------------------------------------------------------
# 1. Test: parse_sku
# ---------------------------------------------------------------------------

class TestParseSku:
    """Test SKU parsing for Browse API search parameters."""

    def setup_method(self):
        self.lf = load_lambda("browse-monitor")

    def test_op_sku(self):
        result = self.lf.parse_sku("OP-OP01-062-JP")
        assert result == {
            'game': 'OP',
            'set_code': 'OP01',
            'card_number': '062',
            'language': 'Japanese',
        }

    def test_pkmn_sku(self):
        result = self.lf.parse_sku("PKMN-SV6-045-JP")
        assert result == {
            'game': 'PKMN',
            'set_code': 'SV6',
            'card_number': '045',
            'language': 'Japanese',
        }

    def test_eb_set_code(self):
        """EB set codes are still under OP game prefix."""
        result = self.lf.parse_sku("OP-EB01-035-JP")
        assert result == {
            'game': 'OP',
            'set_code': 'EB01',
            'card_number': '035',
            'language': 'Japanese',
        }

    def test_invalid_sku_too_short(self):
        assert self.lf.parse_sku("OP-OP01") is None

    def test_invalid_sku_unknown_game(self):
        assert self.lf.parse_sku("MTG-SET1-001-EN") is None

    def test_empty_sku(self):
        assert self.lf.parse_sku("") is None
        assert self.lf.parse_sku(None) is None


# ---------------------------------------------------------------------------
# 2. Test: classify_price_ratio
# ---------------------------------------------------------------------------

class TestClassifyPriceRatio:
    """Test price ratio classification with default and custom thresholds."""

    def setup_method(self):
        self.lf = load_lambda("browse-monitor")

    def test_acquisition(self):
        assert self.lf.classify_price_ratio(0.30) == 'ACQUISITION'
        assert self.lf.classify_price_ratio(0.49) == 'ACQUISITION'

    def test_underpriced(self):
        assert self.lf.classify_price_ratio(0.50) == 'UNDERPRICED'
        assert self.lf.classify_price_ratio(0.69) == 'UNDERPRICED'

    def test_competitive(self):
        assert self.lf.classify_price_ratio(0.70) == 'COMPETITIVE'
        assert self.lf.classify_price_ratio(1.00) == 'COMPETITIVE'

    def test_above(self):
        assert self.lf.classify_price_ratio(1.01) == 'ABOVE'
        assert self.lf.classify_price_ratio(1.50) == 'ABOVE'

    def test_boundary_acquisition(self):
        """Exactly 0.50 is UNDERPRICED, not ACQUISITION."""
        assert self.lf.classify_price_ratio(0.50) == 'UNDERPRICED'

    def test_boundary_competitive(self):
        """Exactly 0.70 is COMPETITIVE, not UNDERPRICED."""
        assert self.lf.classify_price_ratio(0.70) == 'COMPETITIVE'

    @mock.patch.dict(os.environ, {
        'ACQUISITION_THRESHOLD': '0.40',
        'UNDERPRICED_THRESHOLD': '0.60',
    })
    def test_custom_thresholds(self):
        """Thresholds are configurable via env vars."""
        lf = load_lambda("browse-monitor")
        assert lf.classify_price_ratio(0.39) == 'ACQUISITION'
        assert lf.classify_price_ratio(0.40) == 'UNDERPRICED'
        assert lf.classify_price_ratio(0.59) == 'UNDERPRICED'
        assert lf.classify_price_ratio(0.60) == 'COMPETITIVE'


# ---------------------------------------------------------------------------
# 3. Test: Browse API response parsing
# ---------------------------------------------------------------------------

class TestBrowseResponseParsing:
    """Test parsing of Browse API ItemSummary responses."""

    def setup_method(self):
        self.bc = load_browse_client()

    def test_parse_three_items(self):
        results = self.bc.BrowseClient._parse_search_response(SAMPLE_BROWSE_RESPONSE)
        assert len(results) == 3
        assert results[0]['price'] == 3.50
        assert results[0]['seller'] == 'seller_a'
        assert results[0]['item_id'] == 'v1|111111|0'
        assert results[0]['url'] == 'https://www.ebay.co.uk/itm/111111'
        assert results[0]['title'] == 'One Piece OP01-062 Nami Japanese'

    def test_parse_empty_response(self):
        results = self.bc.BrowseClient._parse_search_response(EMPTY_BROWSE_RESPONSE)
        assert results == []

    def test_parse_missing_price(self):
        """Items without a price value are skipped."""
        data = {
            "itemSummaries": [
                {"itemId": "v1|111|0", "title": "Test", "price": {}, "seller": {"username": "x"}},
                {"itemId": "v1|222|0", "title": "Test2", "price": {"value": "5.00"}, "seller": {"username": "y"}},
            ]
        }
        results = self.bc.BrowseClient._parse_search_response(data)
        assert len(results) == 1
        assert results[0]['price'] == 5.00

    def test_parse_missing_seller(self):
        """Items without seller info still parse (empty username)."""
        data = {
            "itemSummaries": [
                {"itemId": "v1|111|0", "title": "Test", "price": {"value": "3.00"}},
            ]
        }
        results = self.bc.BrowseClient._parse_search_response(data)
        assert len(results) == 1
        assert results[0]['seller'] == ''


# ---------------------------------------------------------------------------
# 4. Test: store_results
# ---------------------------------------------------------------------------

class TestStoreResults:
    """Test storing competitor results in browse_price_monitor."""

    def setup_method(self):
        self.lf = load_lambda("browse-monitor")

    def test_stores_top_5(self):
        """Only top-5 cheapest competitors are stored."""
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        competitors = [{'price': float(i), 'seller': f's{i}', 'item_id': f'id{i}',
                        'url': f'http://x/{i}', 'title': f'Title {i}'}
                       for i in range(1, 8)]  # 7 competitors

        self.lf.store_results(mock_conn, 'OP-OP01-001-JP', 6.00, 10.00,
                              competitors, self.lf.classify_price_ratio)

        assert mock_cursor.execute.call_count == 5  # only top 5

    def test_rank_ordering(self):
        """Rank 1 = cheapest, stored in ascending price order."""
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        competitors = [
            {'price': 5.00, 'seller': 'b', 'item_id': 'id2', 'url': '', 'title': ''},
            {'price': 3.00, 'seller': 'a', 'item_id': 'id1', 'url': '', 'title': ''},
        ]

        self.lf.store_results(mock_conn, 'OP-OP01-001-JP', 6.00, 10.00,
                              competitors, self.lf.classify_price_ratio)

        # First insert should be the cheaper one (rank=1)
        first_call_args = mock_cursor.execute.call_args_list[0][0][1]
        assert first_call_args[3] == 3.00   # competitor_price (index shifted by cost_gbp)
        assert first_call_args[10] == 1     # rank

        second_call_args = mock_cursor.execute.call_args_list[1][0][1]
        assert second_call_args[3] == 5.00
        assert second_call_args[10] == 2

    def test_ratio_computed_against_cost_gbp(self):
        """price_ratio = competitor_price / cost_gbp, not selling_price."""
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        competitors = [{'price': 4.00, 'seller': 'a', 'item_id': '1', 'url': '', 'title': ''}]
        # cost_gbp=8.00, selling_price=15.00 → ratio should be 4.00/8.00 = 0.50
        self.lf.store_results(mock_conn, 'OP-OP01-001-JP', 8.00, 15.00,
                              competitors, self.lf.classify_price_ratio)

        call_args = mock_cursor.execute.call_args_list[0][0][1]
        assert call_args[8] == 0.5  # price_ratio = 4.00 / 8.00

    def test_commit_called(self):
        """Connection.commit() is called after inserts."""
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        competitors = [{'price': 5.00, 'seller': 'a', 'item_id': '1', 'url': '', 'title': ''}]
        self.lf.store_results(mock_conn, 'OP-OP01-001-JP', 6.00, 10.00,
                              competitors, self.lf.classify_price_ratio)

        mock_conn.commit.assert_called_once()

    def test_empty_competitors(self):
        """No competitors → no inserts, returns None."""
        mock_conn = MagicMock()
        result = self.lf.store_results(mock_conn, 'OP-OP01-001-JP', 6.00, 10.00,
                                       [], self.lf.classify_price_ratio)
        assert result is None
        mock_conn.cursor.assert_not_called()


# ---------------------------------------------------------------------------
# 5. Test: SNS alert
# ---------------------------------------------------------------------------

class TestSnsAlert:
    """Test SNS acquisition alert sending."""

    def setup_method(self):
        self.lf = load_lambda("browse-monitor")

    def test_sends_for_acquisitions(self):
        mock_sns = MagicMock()
        topic_arn = 'arn:aws:sns:us-east-1:123:test-topic'

        acquisitions = [{
            'sku': 'OP-OP01-062-JP',
            'cost_gbp': 6.00,
            'selling_price': 10.00,
            'competitor_price': 3.50,
            'ratio': 0.58,
            'seller': 'cheap_seller',
            'url': 'https://ebay.co.uk/itm/123',
        }]

        self.lf.send_acquisition_alert(mock_sns, topic_arn, acquisitions)

        mock_sns.publish.assert_called_once()
        call_kwargs = mock_sns.publish.call_args[1]
        assert call_kwargs['TopicArn'] == topic_arn
        assert '1 targets' in call_kwargs['Subject']
        assert 'OP-OP01-062-JP' in call_kwargs['Message']
        assert '£3.50' in call_kwargs['Message']
        assert '£6.00' in call_kwargs['Message']  # cost shown

    def test_silent_when_empty(self):
        """No alert sent if acquisitions list is empty."""
        mock_sns = MagicMock()
        self.lf.send_acquisition_alert(mock_sns, 'arn:test', [])
        mock_sns.publish.assert_not_called()

    def test_silent_when_no_topic(self):
        """No alert sent if topic ARN is empty/missing."""
        mock_sns = MagicMock()
        acquisitions = [{'sku': 'X', 'cost_gbp': 6, 'selling_price': 10,
                        'competitor_price': 3, 'ratio': 0.5, 'seller': 'x', 'url': ''}]
        self.lf.send_acquisition_alert(mock_sns, '', acquisitions)
        mock_sns.publish.assert_not_called()


# ---------------------------------------------------------------------------
# 6. Test: Full handler (integration)
# ---------------------------------------------------------------------------

class TestBrowseMonitorHandler:
    """Test full lambda_handler with mocked Browse API + DB."""

    def setup_method(self):
        self.lf = load_lambda("browse-monitor")

    @mock.patch.dict(os.environ, {
        'PROXY_ENDPOINT': 'test-proxy',
        'DB_USER': 'test',
        'DB_PASSWORD': 'test',
        'TABLE_NAME': 'cardrush_link',
        'SNS_TOPIC_ARN': '',
    })
    @patch('lambda_function.BrowseClient')
    @patch('lambda_function.get_db_connection')
    @patch('lambda_function.record_pipeline_run')
    def test_full_handler_dry_run(self, mock_record, mock_db, mock_client_cls):
        """Dry run: searches but does not store results."""
        # Mock DB returning 2 SKUs with cost_gbp
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        mock_cursor.fetchall.return_value = [
            {'sku': 'OP-OP01-062-JP', 'cost_gbp': 3.50, 'ebay_business_selling_price': 5.80},
            {'sku': 'PKMN-SV6-045-JP', 'cost_gbp': 7.50, 'ebay_business_selling_price': 12.80},
        ]
        mock_db.return_value = mock_conn

        # Mock Browse API client
        mock_client = MagicMock()
        mock_client.search_competitors.return_value = [
            {'price': 3.50, 'seller': 'seller_a', 'item_id': '111', 'url': 'http://x', 'title': 'T'},
        ]
        mock_client_cls.return_value = mock_client

        result = self.lf.lambda_handler({'dry_run': True}, {})

        assert result['statusCode'] == 200
        body = json.loads(result['body'])
        assert body['dry_run'] is True
        assert body['scanned'] == 2

    @mock.patch.dict(os.environ, {
        'PROXY_ENDPOINT': 'test-proxy',
        'DB_USER': 'test',
        'DB_PASSWORD': 'test',
        'TABLE_NAME': 'cardrush_link',
        'SNS_TOPIC_ARN': 'arn:aws:sns:us-east-1:123:test',
    })
    @patch('lambda_function.boto3')
    @patch('lambda_function.BrowseClient')
    @patch('lambda_function.get_db_connection')
    @patch('lambda_function.record_pipeline_run')
    def test_handler_with_acquisition(self, mock_record, mock_db, mock_client_cls, mock_boto3):
        """Handler detects acquisition target — ratio computed against cost_gbp."""
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        mock_cursor.fetchall.return_value = [
            {'sku': 'OP-OP01-062-JP', 'cost_gbp': 6.50, 'ebay_business_selling_price': 10.80},
        ]
        mock_db.return_value = mock_conn

        # Return a competitor at £3.24 → ratio = 3.24/6.50 = 0.498 → ACQUISITION
        mock_client = MagicMock()
        mock_client.search_competitors.return_value = [
            {'price': 3.24, 'seller': 'bargain', 'item_id': '999', 'url': 'http://deal', 'title': 'Cheap'},
        ]
        mock_client_cls.return_value = mock_client

        mock_sns = MagicMock()
        mock_boto3.client.return_value = mock_sns

        result = self.lf.lambda_handler({}, {})

        assert result['statusCode'] == 200
        body = json.loads(result['body'])
        assert body['acquisitions'] == 1
        assert body['classifications']['ACQUISITION'] == 1
        mock_sns.publish.assert_called_once()


# ---------------------------------------------------------------------------
# 7. Test: Fallback search
# ---------------------------------------------------------------------------

class TestFallbackSearch:
    """Test aspect→keyword fallback in BrowseClient."""

    def setup_method(self):
        self.bc = load_browse_client()

    @patch('browse_client.requests.Session')
    def test_fallback_on_empty_aspect_result(self, mock_session_cls):
        """When aspect search returns 0 results, fallback keyword search fires."""
        mock_session = MagicMock()
        mock_session_cls.return_value = mock_session

        # First call (aspect): empty response
        # Second call (fallback keyword): has results
        mock_resp_empty = MagicMock()
        mock_resp_empty.status_code = 200
        mock_resp_empty.json.return_value = EMPTY_BROWSE_RESPONSE

        mock_resp_results = MagicMock()
        mock_resp_results.status_code = 200
        mock_resp_results.json.return_value = SAMPLE_BROWSE_RESPONSE

        mock_session.get.side_effect = [mock_resp_empty, mock_resp_results]

        client = self.bc.BrowseClient(app_id='test_app', cert_id='test_cert', our_seller_id='me')
        # Pre-set token to avoid OAuth call
        client._token = 'fake_token'
        client._token_expiry = 9999999999

        results = client.search_competitors(
            game='OP', set_code='OP01', card_number='062',
            language='Japanese', our_price=5.80,
        )

        # Two searches should have been made
        assert mock_session.get.call_count == 2
        assert len(results) == 3
        assert results[0]['price'] == 3.50

    @patch('browse_client.requests.Session')
    def test_no_fallback_when_aspect_has_results(self, mock_session_cls):
        """When aspect search returns results, no fallback is needed."""
        mock_session = MagicMock()
        mock_session_cls.return_value = mock_session

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = SAMPLE_BROWSE_RESPONSE

        mock_session.get.return_value = mock_resp

        client = self.bc.BrowseClient(app_id='test_app', cert_id='test_cert')
        client._token = 'fake_token'
        client._token_expiry = 9999999999

        results = client.search_competitors(
            game='OP', set_code='OP01', card_number='062',
            language='Japanese', our_price=5.80,
        )

        assert mock_session.get.call_count == 1
        assert len(results) == 3


# ---------------------------------------------------------------------------
# 8. Test: Monitoring integration
# ---------------------------------------------------------------------------

class TestMonitoringIntegration:
    """Test that record_pipeline_run is called with 'browse-monitor' stage."""

    def setup_method(self):
        self.lf = load_lambda("browse-monitor")

    @mock.patch.dict(os.environ, {
        'PROXY_ENDPOINT': 'test-proxy',
        'DB_USER': 'test',
        'DB_PASSWORD': 'test',
        'TABLE_NAME': 'cardrush_link',
    })
    @patch('lambda_function.BrowseClient')
    @patch('lambda_function.get_db_connection')
    @patch('lambda_function.record_pipeline_run')
    def test_pipeline_run_recorded_on_success(self, mock_record, mock_db, mock_client_cls):
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        mock_cursor.fetchall.return_value = [
            {'sku': 'OP-OP01-062-JP', 'cost_gbp': 3.50, 'ebay_business_selling_price': 5.80},
        ]
        mock_db.return_value = mock_conn

        mock_client = MagicMock()
        mock_client.search_competitors.return_value = []
        mock_client_cls.return_value = mock_client

        self.lf.lambda_handler({}, {})

        mock_record.assert_called_once_with(mock_conn, 'browse-monitor', 'success', 1)

    @mock.patch.dict(os.environ, {
        'PROXY_ENDPOINT': 'test-proxy',
        'DB_USER': 'test',
        'DB_PASSWORD': 'test',
        'TABLE_NAME': 'cardrush_link',
    })
    @patch('lambda_function.get_db_connection')
    @patch('lambda_function.record_pipeline_run')
    def test_pipeline_run_recorded_on_failure(self, mock_record, mock_db):
        """On exception, record_pipeline_run is called with 'failure'."""
        mock_conn = MagicMock()
        mock_db.return_value = mock_conn

        # Make cursor raise to simulate DB error
        mock_conn.cursor.side_effect = Exception("DB error")

        result = self.lf.lambda_handler({}, {})

        assert result['statusCode'] == 500
        mock_record.assert_called_once()
        call_args = mock_record.call_args[0]
        assert call_args[1] == 'browse-monitor'
        assert call_args[2] == 'failure'

    @mock.patch.dict(os.environ, {
        'PROXY_ENDPOINT': 'test-proxy',
        'DB_USER': 'test',
        'DB_PASSWORD': 'test',
        'TABLE_NAME': 'cardrush_link',
    })
    @patch('lambda_function.BrowseClient')
    @patch('lambda_function.get_db_connection')
    @patch('lambda_function.record_pipeline_run')
    def test_no_skus_records_success(self, mock_record, mock_db, mock_client_cls):
        """When no SKUs found, still records success with 0 rows."""
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        mock_cursor.fetchall.return_value = []
        mock_db.return_value = mock_conn

        result = self.lf.lambda_handler({}, {})

        assert result['statusCode'] == 200
        mock_record.assert_called_once_with(mock_conn, 'browse-monitor', 'success', 0, 'No SKUs to scan')
