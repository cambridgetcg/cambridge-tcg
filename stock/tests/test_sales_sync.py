"""Tests for the sales sync pipeline.

Covers:
    1. SaleReduction model
    2. StockStore.apply_reductions()
    3. cross_sync helpers (insert, lookup, reduce)
    4. Shopify webhook Lambda (HMAC, sale, cancellation, edge cases)
    5. eBay poller Lambda (polling, cross-sync, idempotency)

Run: python -m pytest stock/tests/test_sales_sync.py -v
"""

import base64
import hashlib
import hmac as hmac_mod
import json
import os
import sys
import tempfile

from datetime import datetime

import pytest
from unittest.mock import MagicMock, patch, call

# ── Path setup ────────────────────────────────────────────────────
BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO_ROOT = os.path.dirname(BASE)

if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

# Ensure real 'requests' is imported before mock loop so it isn't shadowed.
# Without this, if requests hasn't been imported yet, the loop below would
# replace it with a MagicMock — breaking any test file that later does
# `from requests.adapters import ...` (e.g. pricing/tests/test_pipeline_e2e.py).
import requests as _real_requests  # noqa: F401

# Pre-mock external dependencies so Lambda imports don't fail
for _mod in ['psycopg2', 'psycopg2.extras', 'boto3', 'requests',
             'ebay_auth', 'stock.sync.shopify.auth',
             'monitoring', 'monitoring.metrics']:
    if _mod not in sys.modules:
        sys.modules[_mod] = MagicMock()

from stock.count.models import SaleReduction, StockRecord, StockUpdate
from stock.count.stock_store import StockStore
from stock.sync.orders import cross_sync

# ── Test data ─────────────────────────────────────────────────────

SAMPLE_STOCK = {
    'metadata': {'last_modified': '2026-02-10T00:00:00+00:00'},
    'stock': {
        'OP-OP05-001-JP': {
            'quantity': 10,
            'total_cost_yen': 5000,
            'purchased_qty': 5,
            'last_updated': '2026-02-10T00:00:00+00:00',
        },
        'PKMN-SV06-050-JP': {
            'quantity': 3,
            'total_cost_yen': 1200,
            'purchased_qty': 3,
            'last_updated': '2026-02-10T00:00:00+00:00',
        },
        'OP-OP09-001-JP': {
            'quantity': 1,
            'total_cost_yen': 800,
            'purchased_qty': 1,
            'last_updated': '2026-02-10T00:00:00+00:00',
        },
    },
}

WEBHOOK_SECRET = 'test-secret-key-12345'

SAMPLE_SHOPIFY_ORDER = {
    'id': 5001,
    'name': '#1042',
    'line_items': [
        {'sku': 'OP-OP05-001-JP', 'quantity': 2, 'price': '9.80'},
        {'sku': 'PKMN-SV06-050-JP', 'quantity': 1, 'price': '4.80'},
        {'sku': '', 'quantity': 1, 'price': '1.00'},  # no SKU — skipped
    ],
}

SAMPLE_EBAY_ORDERS = [
    {
        'order_id': 'EBAY-ORD-001',
        'line_items': [
            {'sku': 'OP-OP05-001-JP', 'quantity': 1, 'price_gbp': 9.80, 'item_id': '111111'},
        ],
    },
    {
        'order_id': 'EBAY-ORD-002',
        'line_items': [
            {'sku': 'PKMN-SV06-050-JP', 'quantity': 2, 'price_gbp': 4.80, 'item_id': '222222'},
        ],
    },
]


class _MockCursor:
    """In-memory cursor that serves SAMPLE_STOCK data and tracks mutations."""

    def __init__(self, store_ref):
        self._store = store_ref  # dict: sku -> {quantity, total_cost_yen, purchased_qty}
        self._last_result = None

    def execute(self, sql, params=None):
        sql_upper = sql.strip().upper()
        if sql_upper.startswith('SELECT'):
            if params and 'stock_inventory' in sql.lower():
                sku = params[0]
                row = self._store.get(sku)
                self._last_result = row
            else:
                self._last_result = None
        elif sql_upper.startswith('UPDATE') and 'stock_inventory' in sql.lower():
            # UPDATE stock_inventory SET quantity = %s, last_updated = %s WHERE sku = %s
            if params and len(params) >= 3:
                new_qty, _ts, sku = params[0], params[1], params[2]
                if sku in self._store:
                    self._store[sku]['quantity'] = new_qty
        elif sql_upper.startswith('INSERT') and 'stock_inventory' in sql.lower():
            pass  # not needed for these tests

    def fetchone(self):
        return self._last_result

    def fetchall(self):
        return list(self._store.values()) if self._last_result is None else []

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass


class _MockStockConn:
    """In-memory DB connection backed by a copy of stock data."""

    def __init__(self, stock_data):
        # Build flat dict: sku -> row tuple (sku, qty, cost_yen, purchased_qty, last_updated)
        self._rows = {}
        for sku, v in stock_data['stock'].items():
            self._rows[sku] = {
                'tuple': (sku, v['quantity'], v['total_cost_yen'], v['purchased_qty'], None),
                'quantity': v['quantity'],
                'total_cost_yen': v['total_cost_yen'],
                'purchased_qty': v['purchased_qty'],
            }
        self._cursor = _InMemCursor(self._rows)

    def cursor(self):
        return self._cursor

    def commit(self):
        pass

    def close(self):
        pass


class _InMemCursor:
    """Cursor that understands the queries StockStore issues."""

    def __init__(self, rows):
        self._rows = rows  # sku -> dict with quantity/total_cost_yen/purchased_qty
        self._last = None
        self._one_col = False  # True when SELECT returns only quantity

    def execute(self, sql, params=None):
        s = sql.strip().upper()
        self._last = None
        self._one_col = False

        if s.startswith('SELECT') and params:
            sku = params[0]
            entry = self._rows.get(sku)
            if entry:
                # Detect 1-col vs 5-col SELECT
                if 'total_cost_yen' in sql.lower():
                    # get(): SELECT sku, quantity, total_cost_yen, purchased_qty, last_updated
                    self._last = (sku,
                                  entry['quantity'],
                                  entry['total_cost_yen'],
                                  entry['purchased_qty'],
                                  None)
                    self._one_col = False
                else:
                    # apply_reductions(): SELECT quantity FROM stock_inventory WHERE sku=%s
                    self._last = (entry['quantity'],)
                    self._one_col = True
        elif s.startswith('UPDATE') and params and len(params) >= 3:
            new_qty, _ts, sku = params[0], params[1], params[2]
            if sku in self._rows:
                self._rows[sku]['quantity'] = new_qty

    def fetchone(self):
        return self._last

    def fetchall(self):
        return [
            (sku, v['quantity'], v['total_cost_yen'], v['purchased_qty'], None)
            for sku, v in self._rows.items()
        ]

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass


def _make_store(stock_data=None):
    """Create a StockStore backed by an in-memory mock DB connection."""
    data = stock_data or SAMPLE_STOCK
    conn = _MockStockConn(data)
    store = StockStore(conn)
    # Attach _data shim for legacy test assertions
    store._data = data
    return store, None  # path=None (no temp file)


def _make_hmac(body_str, secret=WEBHOOK_SECRET):
    """Compute Shopify-style HMAC-SHA256 for a webhook body."""
    digest = hmac_mod.new(
        secret.encode('utf-8'),
        body_str.encode('utf-8'),
        hashlib.sha256,
    ).digest()
    return base64.b64encode(digest).decode('utf-8')


def _mock_db_cursor():
    """Create a mock connection + cursor with context manager support."""
    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
    mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
    return mock_conn, mock_cursor


# ══════════════════════════════════════════════════════════════════
# 1. SaleReduction Model
# ══════════════════════════════════════════════════════════════════

class TestSaleReductionModel:
    """Test SaleReduction dataclass."""

    def test_basic_fields(self):
        r = SaleReduction(sku='OP-OP05-001-JP', quantity_sold=3)
        assert r.sku == 'OP-OP05-001-JP'
        assert r.quantity_sold == 3
        assert r.platform == ''
        assert r.order_id == ''

    def test_with_platform(self):
        r = SaleReduction(sku='X', quantity_sold=1, platform='shopify', order_id='#1042')
        assert r.platform == 'shopify'
        assert r.order_id == '#1042'


# ══════════════════════════════════════════════════════════════════
# 2. StockStore.apply_reductions()
# ══════════════════════════════════════════════════════════════════

class TestApplyReductions:
    """Test stock reduction logic."""

    def test_basic_reduction(self):
        store, path = _make_store()
        try:
            results = store.apply_reductions([
                SaleReduction(sku='OP-OP05-001-JP', quantity_sold=3),
            ])
            assert len(results) == 1
            assert results[0]['old_qty'] == 10
            assert results[0]['new_qty'] == 7
            assert results[0]['clamped'] is False
            assert results[0]['skipped'] is False

            # Verify persisted
            record = store.get('OP-OP05-001-JP')
            assert record.quantity == 7
        finally:
            pass  # no temp file with mock conn

    def test_reduction_clamps_to_zero(self):
        store, path = _make_store()
        try:
            results = store.apply_reductions([
                SaleReduction(sku='OP-OP09-001-JP', quantity_sold=5),
            ])
            assert results[0]['old_qty'] == 1
            assert results[0]['new_qty'] == 0
            assert results[0]['clamped'] is True
        finally:
            pass  # no temp file with mock conn

    def test_reduction_preserves_purchased_qty(self):
        store, path = _make_store()
        try:
            store.apply_reductions([
                SaleReduction(sku='OP-OP05-001-JP', quantity_sold=2),
            ])
            # purchased_qty must not change
            record = store.get('OP-OP05-001-JP')
            assert record.purchased_qty == 5  # unchanged from SAMPLE_STOCK
            assert record.quantity == 8        # 10 - 2
        finally:
            pass  # no temp file with mock conn

    def test_reduction_unknown_sku_skipped(self):
        store, path = _make_store()
        try:
            results = store.apply_reductions([
                SaleReduction(sku='NONEXISTENT-SKU', quantity_sold=1),
            ])
            assert results[0]['skipped'] is True
            assert results[0]['old_qty'] == 0
            assert results[0]['new_qty'] == 0
        finally:
            pass  # no temp file with mock conn

    def test_dry_run_no_save(self):
        store, path = _make_store()
        try:
            results = store.apply_reductions([
                SaleReduction(sku='OP-OP05-001-JP', quantity_sold=3),
            ], dry_run=True)
            assert results[0]['new_qty'] == 7

            # Not persisted
            record = store.get('OP-OP05-001-JP')
            assert record.quantity == 10  # unchanged
        finally:
            pass  # no temp file with mock conn

    def test_multiple_reductions(self):
        store, path = _make_store()
        try:
            results = store.apply_reductions([
                SaleReduction(sku='OP-OP05-001-JP', quantity_sold=2),
                SaleReduction(sku='PKMN-SV06-050-JP', quantity_sold=1),
                SaleReduction(sku='OP-OP09-001-JP', quantity_sold=1),
            ])
            assert len(results) == 3
            assert results[0]['new_qty'] == 8   # 10 - 2
            assert results[1]['new_qty'] == 2   # 3 - 1
            assert results[2]['new_qty'] == 0   # 1 - 1
        finally:
            pass  # no temp file with mock conn

    def test_reduction_to_exact_zero(self):
        store, path = _make_store()
        try:
            results = store.apply_reductions([
                SaleReduction(sku='OP-OP09-001-JP', quantity_sold=1),
            ])
            assert results[0]['new_qty'] == 0
            assert results[0]['clamped'] is False  # exactly zero, not clamped
        finally:
            pass  # no temp file with mock conn

    def test_total_cost_unchanged_after_reduction(self):
        """Reductions should not alter total_cost_yen."""
        store, path = _make_store()
        try:
            store.apply_reductions([
                SaleReduction(sku='OP-OP05-001-JP', quantity_sold=5),
            ])
            record = store.get('OP-OP05-001-JP')
            assert record.total_cost_yen == 5000  # unchanged
        finally:
            pass  # no temp file with mock conn


# ══════════════════════════════════════════════════════════════════
# 3. cross_sync helpers
# ══════════════════════════════════════════════════════════════════

class TestCrossSyncHelpers:
    """Test RDS helper functions in cross_sync module."""

    def test_insert_sale_event_success(self):
        conn, cursor = _mock_db_cursor()
        cursor.fetchone.return_value = (42,)  # RETURNING id

        result = cross_sync.insert_sale_event(
            conn, platform='shopify', order_id='5001',
            sku='OP-OP05-001-JP', quantity=2,
        )
        assert result is True
        conn.commit.assert_called_once()

        # Verify SQL contains INSERT with ON CONFLICT
        sql = cursor.execute.call_args[0][0]
        assert 'INSERT INTO sales_events' in sql
        assert 'ON CONFLICT' in sql
        assert 'DO NOTHING' in sql

    def test_insert_sale_event_duplicate(self):
        conn, cursor = _mock_db_cursor()
        cursor.fetchone.return_value = None  # no RETURNING → duplicate

        result = cross_sync.insert_sale_event(
            conn, platform='shopify', order_id='5001',
            sku='OP-OP05-001-JP', quantity=2,
        )
        assert result is False

    def test_insert_sale_event_with_payload(self):
        conn, cursor = _mock_db_cursor()
        cursor.fetchone.return_value = (1,)

        cross_sync.insert_sale_event(
            conn, platform='ebay', order_id='EBAY-001',
            sku='X', quantity=1, unit_price_gbp=9.80,
            raw_payload={'item_id': '111'},
        )
        params = cursor.execute.call_args[0][1]
        assert params[0] == 'ebay'
        assert params[1] == 'EBAY-001'
        assert params[5] == 9.80
        assert '"item_id"' in params[6]  # JSON payload

    def test_mark_cross_synced_success(self):
        conn, cursor = _mock_db_cursor()
        cross_sync.mark_cross_synced(conn, 'shopify', '5001', 'OP-OP05-001-JP')

        sql = cursor.execute.call_args[0][0]
        assert 'cross_synced = TRUE' in sql
        conn.commit.assert_called_once()

    def test_mark_cross_synced_with_error(self):
        conn, cursor = _mock_db_cursor()
        cross_sync.mark_cross_synced(
            conn, 'shopify', '5001', 'OP-OP05-001-JP', error='no_ebay_listing',
        )

        sql = cursor.execute.call_args[0][0]
        assert 'cross_sync_error' in sql
        params = cursor.execute.call_args[0][1]
        assert params[0] == 'no_ebay_listing'

    def test_lookup_platform_listing_found(self):
        conn, cursor = _mock_db_cursor()
        cursor.fetchone.return_value = ('111111', None, 5)

        result = cross_sync.lookup_platform_listing(conn, 'OP-OP05-001-JP', 'ebay')
        assert result == {
            'platform_id': '111111',
            'secondary_id': None,
            'current_available': 5,
        }

    def test_lookup_platform_listing_not_found(self):
        conn, cursor = _mock_db_cursor()
        cursor.fetchone.return_value = None

        result = cross_sync.lookup_platform_listing(conn, 'MISSING-SKU', 'ebay')
        assert result is None

    def test_reduce_ebay_quantity_success(self):
        mock_client = MagicMock()
        mock_client.revise_item.return_value = {'ack': 'Success', 'errors': []}

        result = cross_sync.reduce_ebay_quantity(
            mock_client, item_id='111111', qty_to_reduce=2, current_available=5,
        )
        assert result['success'] is True
        assert result['new_quantity'] == 3
        mock_client.revise_item.assert_called_once_with('111111', quantity=3)

    def test_reduce_ebay_quantity_clamps_to_zero(self):
        mock_client = MagicMock()
        mock_client.revise_item.return_value = {'ack': 'Success', 'errors': []}

        result = cross_sync.reduce_ebay_quantity(
            mock_client, item_id='111', qty_to_reduce=10, current_available=3,
        )
        assert result['new_quantity'] == 0
        mock_client.revise_item.assert_called_once_with('111', quantity=0)

    def test_reduce_ebay_quantity_api_failure(self):
        mock_client = MagicMock()
        mock_client.revise_item.return_value = {
            'ack': 'Failure', 'errors': ['Item not found'],
        }

        result = cross_sync.reduce_ebay_quantity(
            mock_client, item_id='999', qty_to_reduce=1, current_available=5,
        )
        assert result['success'] is False
        assert result['new_quantity'] == 5  # unchanged on failure
        assert 'Item not found' in result['error']

    def test_reduce_ebay_quantity_exception(self):
        mock_client = MagicMock()
        mock_client.revise_item.side_effect = Exception('Network timeout')

        result = cross_sync.reduce_ebay_quantity(
            mock_client, item_id='111', qty_to_reduce=1, current_available=5,
        )
        assert result['success'] is False
        assert 'Network timeout' in result['error']

    def test_reduce_shopify_quantity_success(self):
        mock_client = MagicMock()
        mock_client.set_inventory_quantities.return_value = [
            {'success': True, 'errors': [], 'count': 1, 'batch': 1},
        ]

        result = cross_sync.reduce_shopify_quantity(
            mock_client,
            inventory_item_id='gid://shopify/InventoryItem/123',
            location_id='gid://shopify/Location/456',
            qty_to_reduce=2,
            current_available=5,
        )
        assert result['success'] is True
        assert result['new_quantity'] == 3

    def test_reduce_shopify_quantity_failure(self):
        mock_client = MagicMock()
        mock_client.set_inventory_quantities.return_value = [
            {'success': False, 'errors': ['Invalid item'], 'count': 1, 'batch': 1},
        ]

        result = cross_sync.reduce_shopify_quantity(
            mock_client,
            inventory_item_id='gid://x', location_id='gid://y',
            qty_to_reduce=1, current_available=5,
        )
        assert result['success'] is False
        assert result['new_quantity'] == 5

    def test_record_pipeline_run_never_raises(self):
        conn, cursor = _mock_db_cursor()
        cursor.execute.side_effect = Exception('DB down')

        # Should not raise
        cross_sync.record_pipeline_run(conn, 'test', 'failure', detail='boom')

    def test_get_last_poll_time_found(self):
        from datetime import datetime
        conn, cursor = _mock_db_cursor()
        cursor.fetchone.return_value = (datetime(2026, 2, 10, 12, 0, 0),)

        result = cross_sync.get_last_poll_time(conn, 'ebay_order_poller')
        assert '2026-02-10' in result
        assert result.endswith('Z')

    def test_get_last_poll_time_no_history(self):
        conn, cursor = _mock_db_cursor()
        cursor.fetchone.return_value = None

        result = cross_sync.get_last_poll_time(conn, 'ebay_order_poller')
        assert result is None


# ══════════════════════════════════════════════════════════════════
# 4. Shopify Webhook Lambda
# ══════════════════════════════════════════════════════════════════

class TestShopifyWebhookLambda:
    """Test shopify_webhook.py Lambda handler."""

    def _make_event(self, order=None, topic='orders/create', secret=WEBHOOK_SECRET):
        """Build an API Gateway v2 event with valid HMAC."""
        body_str = json.dumps(order or SAMPLE_SHOPIFY_ORDER)
        hmac_value = _make_hmac(body_str, secret)
        return {
            'headers': {
                'x-shopify-hmac-sha256': hmac_value,
                'x-shopify-topic': topic,
            },
            'body': body_str,
            'isBase64Encoded': False,
        }

    @patch.dict(os.environ, {'SHOPIFY_WEBHOOK_SECRET': WEBHOOK_SECRET})
    @patch('stock.sync.orders.shopify_webhook.get_db_connection')
    @patch('stock.sync.orders.shopify_webhook.insert_sale_event')
    @patch('stock.sync.orders.shopify_webhook.lookup_and_lock_platform_listing')
    @patch('stock.sync.orders.shopify_webhook.check_listing_staleness')
    @patch('stock.sync.orders.shopify_webhook.reduce_ebay_quantity')
    @patch('stock.sync.orders.shopify_webhook.mark_cross_synced')
    @patch('stock.sync.orders.shopify_webhook.update_platform_available')
    @patch('stock.sync.orders.shopify_webhook.record_pipeline_run')
    @patch('stock.sync.orders.shopify_webhook.EbayClient')
    def test_sale_order_success(self, MockEbay, mock_record, mock_update,
                                 mock_mark, mock_reduce, mock_stale, mock_lookup,
                                 mock_insert, mock_db):
        from stock.sync.orders.shopify_webhook import lambda_handler

        mock_conn = MagicMock()
        mock_db.return_value = mock_conn
        mock_insert.return_value = True  # new event
        mock_lookup.return_value = {
            'platform_id': '111111', 'secondary_id': None, 'current_available': 5,
        }
        mock_reduce.return_value = {'success': True, 'new_quantity': 3, 'error': None}

        event = self._make_event()
        result = lambda_handler(event, None)

        assert result['statusCode'] == 200
        # 2 items with SKUs (empty SKU skipped)
        assert mock_insert.call_count == 2
        # Verify first insert call
        first_call = mock_insert.call_args_list[0]
        assert first_call[1]['platform'] == 'shopify'
        assert first_call[1]['sku'] == 'OP-OP05-001-JP'
        assert first_call[1]['quantity'] == 2

    @patch.dict(os.environ, {'SHOPIFY_WEBHOOK_SECRET': WEBHOOK_SECRET})
    def test_invalid_hmac_returns_401(self):
        from stock.sync.orders.shopify_webhook import lambda_handler

        event = {
            'headers': {
                'x-shopify-hmac-sha256': 'invalid-hmac-value',
                'x-shopify-topic': 'orders/create',
            },
            'body': json.dumps(SAMPLE_SHOPIFY_ORDER),
            'isBase64Encoded': False,
        }
        result = lambda_handler(event, None)
        assert result['statusCode'] == 401

    @patch.dict(os.environ, {'SHOPIFY_WEBHOOK_SECRET': ''})
    def test_missing_secret_returns_500(self):
        from stock.sync.orders.shopify_webhook import lambda_handler

        event = self._make_event()
        result = lambda_handler(event, None)
        assert result['statusCode'] == 500

    @patch.dict(os.environ, {'SHOPIFY_WEBHOOK_SECRET': WEBHOOK_SECRET})
    @patch('stock.sync.orders.shopify_webhook.get_db_connection')
    @patch('stock.sync.orders.shopify_webhook.insert_sale_event')
    @patch('stock.sync.orders.shopify_webhook.lookup_and_lock_platform_listing')
    @patch('stock.sync.orders.shopify_webhook.check_listing_staleness')
    @patch('stock.sync.orders.shopify_webhook.mark_cross_synced')
    @patch('stock.sync.orders.shopify_webhook.record_pipeline_run')
    @patch('stock.sync.orders.shopify_webhook.EbayClient')
    def test_cancellation_order(self, MockEbay, mock_record, mock_mark,
                                 mock_stale, mock_lookup, mock_insert, mock_db):
        from stock.sync.orders.shopify_webhook import lambda_handler

        mock_conn = MagicMock()
        mock_db.return_value = mock_conn
        mock_insert.return_value = True
        mock_lookup.return_value = {
            'platform_id': '111111', 'secondary_id': None, 'current_available': 3,
        }
        mock_ebay = MagicMock()
        mock_ebay.revise_item.return_value = {'ack': 'Success', 'errors': []}
        MockEbay.return_value = mock_ebay

        event = self._make_event(topic='orders/cancelled')
        result = lambda_handler(event, None)

        assert result['statusCode'] == 200
        # Cancellation: quantity should be negative
        first_insert = mock_insert.call_args_list[0]
        assert first_insert[1]['quantity'] == -2  # negative for cancellation
        assert first_insert[1]['event_type'] == 'cancellation'

        # eBay qty should INCREASE (cancellation restores stock)
        mock_ebay.revise_item.assert_called()
        # First call: OP-OP05-001-JP qty=2 → current_available(3) + 2 = 5
        first_revise = mock_ebay.revise_item.call_args_list[0]
        assert first_revise[0][0] == '111111'
        assert first_revise[1]['quantity'] == 5
        # Second call: PKMN-SV06-050-JP qty=1 → current_available(3) + 1 = 4
        second_revise = mock_ebay.revise_item.call_args_list[1]
        assert second_revise[1]['quantity'] == 4

    @patch.dict(os.environ, {'SHOPIFY_WEBHOOK_SECRET': WEBHOOK_SECRET})
    @patch('stock.sync.orders.shopify_webhook.get_db_connection')
    @patch('stock.sync.orders.shopify_webhook.insert_sale_event')
    @patch('stock.sync.orders.shopify_webhook.lookup_and_lock_platform_listing')
    @patch('stock.sync.orders.shopify_webhook.mark_cross_synced')
    @patch('stock.sync.orders.shopify_webhook.record_pipeline_run')
    def test_no_ebay_listing_skips_cross_sync(self, mock_record, mock_mark,
                                               mock_lookup, mock_insert, mock_db):
        from stock.sync.orders.shopify_webhook import lambda_handler

        mock_db.return_value = MagicMock()
        mock_insert.return_value = True
        mock_lookup.return_value = None  # no eBay listing

        event = self._make_event()
        result = lambda_handler(event, None)

        assert result['statusCode'] == 200
        # Should mark with error, not crash
        error_calls = [c for c in mock_mark.call_args_list if c[1].get('error')]
        assert len(error_calls) >= 1
        assert 'no_ebay_listing' in str(error_calls[0])

    @patch.dict(os.environ, {'SHOPIFY_WEBHOOK_SECRET': WEBHOOK_SECRET})
    @patch('stock.sync.orders.shopify_webhook.get_db_connection')
    @patch('stock.sync.orders.shopify_webhook.insert_sale_event')
    @patch('stock.sync.orders.shopify_webhook.record_pipeline_run')
    def test_duplicate_event_skipped(self, mock_record, mock_insert, mock_db):
        from stock.sync.orders.shopify_webhook import lambda_handler

        mock_db.return_value = MagicMock()
        mock_insert.return_value = False  # duplicate

        event = self._make_event()
        result = lambda_handler(event, None)

        assert result['statusCode'] == 200

    @patch.dict(os.environ, {'SHOPIFY_WEBHOOK_SECRET': WEBHOOK_SECRET})
    def test_no_sku_items_returns_200(self):
        from stock.sync.orders.shopify_webhook import lambda_handler

        order = {'id': 9999, 'name': '#EMPTY', 'line_items': [
            {'sku': '', 'quantity': 1, 'price': '5.00'},
        ]}
        event = self._make_event(order=order)
        result = lambda_handler(event, None)
        assert result['statusCode'] == 200
        assert 'no items' in result['body']

    @patch.dict(os.environ, {'SHOPIFY_WEBHOOK_SECRET': WEBHOOK_SECRET})
    def test_base64_encoded_body(self):
        from stock.sync.orders.shopify_webhook import _verify_hmac

        body_str = json.dumps({'id': 1, 'line_items': []})
        body_bytes = body_str.encode('utf-8')
        hmac_value = _make_hmac(body_str)

        assert _verify_hmac(body_bytes, hmac_value, WEBHOOK_SECRET) is True
        assert _verify_hmac(body_bytes, 'wrong', WEBHOOK_SECRET) is False


# ══════════════════════════════════════════════════════════════════
# 5. eBay Order Poller Lambda
# ══════════════════════════════════════════════════════════════════

class TestEbayPollerLambda:
    """Test ebay_poller.py Lambda handler."""

    @patch('stock.sync.orders.ebay_poller.get_db_connection')
    @patch('stock.sync.orders.ebay_poller.EbayClient')
    @patch('stock.sync.orders.ebay_poller.ShopifyClient')
    @patch('stock.sync.orders.ebay_poller.get_last_poll_time')
    @patch('stock.sync.orders.ebay_poller.insert_sale_event')
    @patch('stock.sync.orders.ebay_poller.lookup_and_lock_platform_listing')
    @patch('stock.sync.orders.ebay_poller.check_listing_staleness')
    @patch('stock.sync.orders.ebay_poller.reduce_shopify_quantity')
    @patch('stock.sync.orders.ebay_poller.mark_cross_synced')
    @patch('stock.sync.orders.ebay_poller.update_platform_available')
    @patch('stock.sync.orders.ebay_poller.record_pipeline_run')
    def test_successful_poll_and_sync(self, mock_record, mock_update,
                                       mock_mark, mock_reduce, mock_stale,
                                       mock_lookup,
                                       mock_insert, mock_poll_time,
                                       MockShopify, MockEbay, mock_db):
        from stock.sync.orders.ebay_poller import lambda_handler

        mock_conn = MagicMock()
        mock_db.return_value = mock_conn
        mock_poll_time.return_value = '2026-02-10T11:00:00Z'

        mock_ebay = MagicMock()
        mock_ebay.get_orders.return_value = SAMPLE_EBAY_ORDERS
        MockEbay.return_value = mock_ebay

        mock_shopify = MagicMock()
        MockShopify.return_value = mock_shopify

        mock_insert.return_value = True
        mock_lookup.return_value = {
            'platform_id': 'gid://shopify/InventoryItem/123',
            'secondary_id': 'gid://shopify/Location/456',
            'current_available': 10,
        }
        mock_reduce.return_value = {'success': True, 'new_quantity': 9, 'error': None}

        result = lambda_handler({}, None)

        assert result['statusCode'] == 200
        body = json.loads(result['body'])
        assert body['orders_fetched'] == 2
        assert body['events_inserted'] == 2  # 1 line item per order × 2 orders

        # Verify pipeline run recorded as success
        mock_record.assert_called_once()
        assert mock_record.call_args[0][2] == 'success'

    @patch('stock.sync.orders.ebay_poller.get_db_connection')
    @patch('stock.sync.orders.ebay_poller.EbayClient')
    @patch('stock.sync.orders.ebay_poller.ShopifyClient')
    @patch('stock.sync.orders.ebay_poller.get_last_poll_time')
    @patch('stock.sync.orders.ebay_poller.insert_sale_event')
    @patch('stock.sync.orders.ebay_poller.record_pipeline_run')
    def test_no_orders(self, mock_record, mock_insert, mock_poll_time,
                        MockShopify, MockEbay, mock_db):
        from stock.sync.orders.ebay_poller import lambda_handler

        mock_db.return_value = MagicMock()
        mock_poll_time.return_value = '2026-02-10T11:00:00Z'
        MockEbay.return_value.get_orders.return_value = []

        result = lambda_handler({}, None)

        assert result['statusCode'] == 200
        body = json.loads(result['body'])
        assert body['orders_fetched'] == 0
        assert body['events_inserted'] == 0
        mock_insert.assert_not_called()

    @patch('stock.sync.orders.ebay_poller.get_db_connection')
    @patch('stock.sync.orders.ebay_poller.EbayClient')
    @patch('stock.sync.orders.ebay_poller.ShopifyClient')
    @patch('stock.sync.orders.ebay_poller.get_last_poll_time')
    @patch('stock.sync.orders.ebay_poller.insert_sale_event')
    @patch('stock.sync.orders.ebay_poller.lookup_and_lock_platform_listing')
    @patch('stock.sync.orders.ebay_poller.mark_cross_synced')
    @patch('stock.sync.orders.ebay_poller.record_pipeline_run')
    def test_missing_shopify_listing(self, mock_record, mock_mark, mock_lookup,
                                      mock_insert, mock_poll_time,
                                      MockShopify, MockEbay, mock_db):
        from stock.sync.orders.ebay_poller import lambda_handler

        mock_db.return_value = MagicMock()
        mock_poll_time.return_value = None  # first run
        MockEbay.return_value.get_orders.return_value = [SAMPLE_EBAY_ORDERS[0]]
        mock_insert.return_value = True
        mock_lookup.return_value = None  # no Shopify listing

        result = lambda_handler({}, None)

        assert result['statusCode'] == 200
        # Should mark error, not crash
        mock_mark.assert_called()
        assert 'no_shopify_listing' in str(mock_mark.call_args)

    @patch('stock.sync.orders.ebay_poller.get_db_connection')
    @patch('stock.sync.orders.ebay_poller.EbayClient')
    @patch('stock.sync.orders.ebay_poller.ShopifyClient')
    @patch('stock.sync.orders.ebay_poller.get_last_poll_time')
    @patch('stock.sync.orders.ebay_poller.insert_sale_event')
    @patch('stock.sync.orders.ebay_poller.lookup_and_lock_platform_listing')
    @patch('stock.sync.orders.ebay_poller.check_listing_staleness')
    @patch('stock.sync.orders.ebay_poller.reduce_shopify_quantity')
    @patch('stock.sync.orders.ebay_poller.mark_cross_synced')
    @patch('stock.sync.orders.ebay_poller.update_platform_available')
    @patch('stock.sync.orders.ebay_poller.record_pipeline_run')
    def test_cross_sync_failure_records_partial(self, mock_record, mock_update,
                                                 mock_mark, mock_reduce, mock_stale,
                                                 mock_lookup,
                                                 mock_insert, mock_poll_time,
                                                 MockShopify, MockEbay, mock_db):
        from stock.sync.orders.ebay_poller import lambda_handler

        mock_db.return_value = MagicMock()
        mock_poll_time.return_value = '2026-02-10T11:00:00Z'
        MockEbay.return_value.get_orders.return_value = [SAMPLE_EBAY_ORDERS[0]]
        mock_insert.return_value = True
        mock_lookup.return_value = {
            'platform_id': 'gid://x', 'secondary_id': 'gid://y', 'current_available': 5,
        }
        mock_reduce.return_value = {'success': False, 'new_quantity': 5, 'error': 'API error'}

        result = lambda_handler({}, None)

        assert result['statusCode'] == 200
        body = json.loads(result['body'])
        assert body['cross_failed'] == 1
        # Pipeline run recorded as 'partial'
        mock_record.assert_called_once()
        assert mock_record.call_args[0][2] == 'partial'

    @patch('stock.sync.orders.ebay_poller.get_db_connection')
    @patch('stock.sync.orders.ebay_poller.EbayClient')
    @patch('stock.sync.orders.ebay_poller.ShopifyClient')
    @patch('stock.sync.orders.ebay_poller.get_last_poll_time')
    @patch('stock.sync.orders.ebay_poller.insert_sale_event')
    @patch('stock.sync.orders.ebay_poller.lookup_and_lock_platform_listing')
    @patch('stock.sync.orders.ebay_poller.check_listing_staleness')
    @patch('stock.sync.orders.ebay_poller.reduce_shopify_quantity')
    @patch('stock.sync.orders.ebay_poller.mark_cross_synced')
    @patch('stock.sync.orders.ebay_poller.update_platform_available')
    @patch('stock.sync.orders.ebay_poller.record_pipeline_run')
    def test_duplicate_events_skipped(self, mock_record, mock_update,
                                       mock_mark, mock_reduce, mock_stale,
                                       mock_lookup,
                                       mock_insert, mock_poll_time,
                                       MockShopify, MockEbay, mock_db):
        from stock.sync.orders.ebay_poller import lambda_handler

        mock_db.return_value = MagicMock()
        mock_poll_time.return_value = '2026-02-10T11:00:00Z'
        MockEbay.return_value.get_orders.return_value = [SAMPLE_EBAY_ORDERS[0]]
        mock_insert.return_value = False  # duplicate

        result = lambda_handler({}, None)

        body = json.loads(result['body'])
        assert body['events_skipped'] == 1
        assert body['events_inserted'] == 0
        mock_lookup.assert_not_called()  # skip cross-sync for duplicates

    @patch('stock.sync.orders.ebay_poller.get_db_connection')
    @patch('stock.sync.orders.ebay_poller.record_pipeline_run')
    def test_db_connection_failure(self, mock_record, mock_db):
        from stock.sync.orders.ebay_poller import lambda_handler

        mock_db.side_effect = Exception('Connection refused')

        result = lambda_handler({}, None)

        assert result['statusCode'] == 500
        body = json.loads(result['body'])
        assert 'Connection refused' in body['error']

    @patch('stock.sync.orders.ebay_poller.get_db_connection')
    @patch('stock.sync.orders.ebay_poller.EbayClient')
    @patch('stock.sync.orders.ebay_poller.ShopifyClient')
    @patch('stock.sync.orders.ebay_poller.get_last_poll_time')
    @patch('stock.sync.orders.ebay_poller.record_pipeline_run')
    def test_first_run_uses_lookback(self, mock_record, mock_poll_time,
                                      MockShopify, MockEbay, mock_db):
        from stock.sync.orders.ebay_poller import lambda_handler

        mock_db.return_value = MagicMock()
        mock_poll_time.return_value = None  # no history
        mock_ebay = MagicMock()
        mock_ebay.get_orders.return_value = []
        MockEbay.return_value = mock_ebay

        lambda_handler({}, None)

        # Should have called get_orders with a valid time range
        mock_ebay.get_orders.assert_called_once()
        args = mock_ebay.get_orders.call_args[0]
        assert '2026' in args[0]  # create_time_from
        assert '2026' in args[1]  # create_time_to


# ══════════════════════════════════════════════════════════════════
# 6. Race Condition Fix (SELECT FOR UPDATE)
# ══════════════════════════════════════════════════════════════════

class TestLookupAndLockPlatformListing:
    """Test lookup_and_lock_platform_listing uses SELECT FOR UPDATE."""

    def test_uses_for_update(self):
        conn, cursor = _mock_db_cursor()
        cursor.fetchone.return_value = ('111111', None, 5, datetime(2026, 2, 10, 12, 0, 0))

        result = cross_sync.lookup_and_lock_platform_listing(conn, 'OP-OP05-001-JP', 'ebay')
        assert result is not None
        assert result['platform_id'] == '111111'
        assert result['current_available'] == 5
        assert result['last_refreshed'] is not None

        sql = cursor.execute.call_args[0][0]
        assert 'FOR UPDATE' in sql

    def test_returns_none_when_not_found(self):
        conn, cursor = _mock_db_cursor()
        cursor.fetchone.return_value = None

        result = cross_sync.lookup_and_lock_platform_listing(conn, 'MISSING', 'ebay')
        assert result is None

    def test_raises_on_db_error(self):
        conn, cursor = _mock_db_cursor()
        cursor.execute.side_effect = Exception('DB connection lost')

        with pytest.raises(Exception, match='DB connection lost'):
            cross_sync.lookup_and_lock_platform_listing(conn, 'SKU', 'ebay')


# ══════════════════════════════════════════════════════════════════
# 7. Error Re-raising (B2 fixes)
# ══════════════════════════════════════════════════════════════════

class TestErrorReRaising:
    """Test that cross_sync helpers re-raise DB errors instead of swallowing."""

    def test_mark_cross_synced_raises_on_db_error(self):
        conn, cursor = _mock_db_cursor()
        cursor.execute.side_effect = Exception('DB write failed')

        with pytest.raises(Exception, match='DB write failed'):
            cross_sync.mark_cross_synced(conn, 'shopify', '5001', 'OP-OP05-001-JP')
        conn.rollback.assert_called()

    def test_lookup_platform_listing_raises_on_db_error(self):
        conn, cursor = _mock_db_cursor()
        cursor.execute.side_effect = Exception('Connection timeout')

        with pytest.raises(Exception, match='Connection timeout'):
            cross_sync.lookup_platform_listing(conn, 'SKU', 'ebay')

    def test_update_platform_available_raises_on_db_error(self):
        conn, cursor = _mock_db_cursor()
        cursor.execute.side_effect = Exception('Disk full')

        with pytest.raises(Exception, match='Disk full'):
            cross_sync.update_platform_available(conn, 'SKU', 'ebay', 5)
        conn.rollback.assert_called()


# ══════════════════════════════════════════════════════════════════
# 8. Listing Staleness Check
# ══════════════════════════════════════════════════════════════════

class TestCheckListingStaleness:
    """Test check_listing_staleness helper."""

    def test_stale_listing_returns_true(self):
        from datetime import datetime as dt, timezone as tz, timedelta as td
        old_time = dt.now(tz.utc) - td(hours=48)
        listing = {'last_refreshed': old_time}
        assert cross_sync.check_listing_staleness(listing) is True

    def test_recent_listing_returns_false(self):
        from datetime import datetime as dt, timezone as tz, timedelta as td
        recent_time = dt.now(tz.utc) - td(hours=1)
        listing = {'last_refreshed': recent_time}
        assert cross_sync.check_listing_staleness(listing) is False

    def test_none_last_refreshed_returns_true(self):
        listing = {'last_refreshed': None}
        assert cross_sync.check_listing_staleness(listing) is True

    def test_custom_max_age(self):
        from datetime import datetime as dt, timezone as tz, timedelta as td
        # 5 hours old, with a 4-hour threshold → stale
        old_time = dt.now(tz.utc) - td(hours=5)
        listing = {'last_refreshed': old_time}
        assert cross_sync.check_listing_staleness(listing, max_age_hours=4) is True
        # Same age with 6-hour threshold → not stale
        assert cross_sync.check_listing_staleness(listing, max_age_hours=6) is False


# ══════════════════════════════════════════════════════════════════
# 9. FX Fallback (C2)
# ══════════════════════════════════════════════════════════════════

class TestFxFallback:
    """Test FX Lambda fallback to DB rate when API fails."""

    @staticmethod
    def _load_fx():
        """Load FX lambda module via symlink path (same as pricing tests)."""
        fx_dir = os.path.join(REPO_ROOT, 'pricing', 'lambdas', 'cardrush-fx-updater')
        if fx_dir in sys.path:
            sys.path.remove(fx_dir)
        sys.path.insert(0, fx_dir)
        # Also ensure pricing/ is on path for monitoring.metrics
        pricing_dir = os.path.join(REPO_ROOT, 'pricing')
        if pricing_dir not in sys.path:
            sys.path.insert(0, pricing_dir)
        if 'lambda_function' in sys.modules:
            del sys.modules['lambda_function']
        import lambda_function
        return lambda_function

    @patch.dict(os.environ, {
        'PROXY_ENDPOINT': 'test', 'DB_USER': 'u', 'DB_PASSWORD': 'p',
        'TABLE_NAME': 'cardrush_link', 'AMDOREN_API_KEY': 'key123',
    })
    def test_fallback_uses_db_rate(self):
        fx = self._load_fx()

        with patch.object(fx, 'fetch_gbp_to_jpy', side_effect=Exception('API timeout')), \
             patch.object(fx, 'get_db_connection') as mock_db, \
             patch.object(fx, 'record_pipeline_run'):

            # Fallback connection returns a rate
            mock_fallback_conn = MagicMock()
            mock_fallback_cursor = MagicMock()
            mock_fallback_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_fallback_cursor)
            mock_fallback_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
            mock_fallback_cursor.fetchone.return_value = (190.50,)

            # Main connection for the UPDATE
            mock_main_conn = MagicMock()
            mock_main_cursor = MagicMock()
            mock_main_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_main_cursor)
            mock_main_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
            mock_main_cursor.rowcount = 100

            # First call = fallback conn, second = main conn
            mock_db.side_effect = [mock_fallback_conn, mock_main_conn]

            result = fx.lambda_handler({}, None)

            assert result['statusCode'] == 200
            body = json.loads(result['body'])
            assert body['rate'] == 190.50
            assert body['rate_source'] == 'db_fallback'

    @patch.dict(os.environ, {
        'PROXY_ENDPOINT': 'test', 'DB_USER': 'u', 'DB_PASSWORD': 'p',
        'TABLE_NAME': 'cardrush_link', 'AMDOREN_API_KEY': 'key123',
    })
    def test_fallback_no_rate_raises(self):
        fx = self._load_fx()

        with patch.object(fx, 'fetch_gbp_to_jpy', side_effect=Exception('API timeout')), \
             patch.object(fx, 'get_db_connection') as mock_db, \
             patch.object(fx, 'record_pipeline_run'):

            # Fallback connection returns no rate
            mock_fallback_conn = MagicMock()
            mock_fallback_cursor = MagicMock()
            mock_fallback_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_fallback_cursor)
            mock_fallback_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
            mock_fallback_cursor.fetchone.return_value = (None,)

            mock_db.side_effect = [mock_fallback_conn]

            result = fx.lambda_handler({}, None)

            assert result['statusCode'] == 500
            body = json.loads(result['body'])
            assert 'no previous rate' in body['error'].lower() or 'API timeout' in body['error']


# ══════════════════════════════════════════════════════════════════
# 10. Shopify Webhook Registration
# ══════════════════════════════════════════════════════════════════

class TestShopifyWebhookRegistration:
    """Test ShopifyClient webhook CRUD methods."""

    def _make_client(self):
        mock_session = MagicMock()
        client = MagicMock()
        # We test the actual methods by importing the class and mocking _graphql
        from stock.sync.shopify.client import ShopifyClient
        c = ShopifyClient.__new__(ShopifyClient)
        c.store = 'test.myshopify.com'
        c.api_version = '2024-01'
        c.graphql_url = 'https://test.myshopify.com/admin/api/2024-01/graphql.json'
        c._static_token = 'test-token'
        c.session = MagicMock()
        c._graphql = MagicMock()
        return c

    def test_list_webhooks(self):
        c = self._make_client()
        c._graphql.return_value = {
            'webhookSubscriptions': {
                'edges': [
                    {
                        'node': {
                            'id': 'gid://shopify/WebhookSubscription/1',
                            'topic': 'ORDERS_CREATE',
                            'endpoint': {'callbackUrl': 'https://example.com/hook'},
                        }
                    },
                    {
                        'node': {
                            'id': 'gid://shopify/WebhookSubscription/2',
                            'topic': 'ORDERS_CANCELLED',
                            'endpoint': {'callbackUrl': 'https://example.com/hook'},
                        }
                    },
                ]
            }
        }

        from stock.sync.shopify.client import ShopifyClient
        result = ShopifyClient.list_webhooks(c)
        assert len(result) == 2
        assert result[0]['topic'] == 'ORDERS_CREATE'
        assert result[0]['callback_url'] == 'https://example.com/hook'
        assert result[1]['topic'] == 'ORDERS_CANCELLED'

    def test_register_webhook_success(self):
        c = self._make_client()
        c._graphql.return_value = {
            'webhookSubscriptionCreate': {
                'webhookSubscription': {
                    'id': 'gid://shopify/WebhookSubscription/99',
                    'topic': 'ORDERS_CREATE',
                    'endpoint': {'callbackUrl': 'https://example.com/hook'},
                },
                'userErrors': [],
            }
        }

        from stock.sync.shopify.client import ShopifyClient
        result = ShopifyClient.register_webhook(c, 'ORDERS_CREATE', 'https://example.com/hook')
        assert result['success'] is True
        assert result['webhook_id'] == 'gid://shopify/WebhookSubscription/99'

    def test_register_webhook_failure(self):
        c = self._make_client()
        c._graphql.return_value = {
            'webhookSubscriptionCreate': {
                'webhookSubscription': None,
                'userErrors': [{'field': 'topic', 'message': 'already exists'}],
            }
        }

        from stock.sync.shopify.client import ShopifyClient
        result = ShopifyClient.register_webhook(c, 'ORDERS_CREATE', 'https://example.com/hook')
        assert result['success'] is False
        assert 'already exists' in result['errors'][0]

    def test_delete_webhook_success(self):
        c = self._make_client()
        c._graphql.return_value = {
            'webhookSubscriptionDelete': {
                'deletedWebhookSubscriptionId': 'gid://shopify/WebhookSubscription/1',
                'userErrors': [],
            }
        }

        from stock.sync.shopify.client import ShopifyClient
        result = ShopifyClient.delete_webhook(c, 'gid://shopify/WebhookSubscription/1')
        assert result['success'] is True

    def test_ensure_webhooks_creates_both(self):
        c = self._make_client()

        # list_webhooks returns empty
        list_response = {'webhookSubscriptions': {'edges': []}}
        create_response = {
            'webhookSubscriptionCreate': {
                'webhookSubscription': {
                    'id': 'gid://shopify/WebhookSubscription/new',
                    'topic': 'ORDERS_CREATE',
                    'endpoint': {'callbackUrl': 'https://api.example.com/hook'},
                },
                'userErrors': [],
            }
        }
        c._graphql.side_effect = [list_response, create_response, create_response]

        from stock.sync.shopify.client import ShopifyClient
        result = ShopifyClient.ensure_webhooks(c, 'https://api.example.com/hook')
        assert len(result['created']) == 2
        assert result['existing'] == []
        assert result['errors'] == []

    def test_ensure_webhooks_skips_existing(self):
        c = self._make_client()

        list_response = {
            'webhookSubscriptions': {
                'edges': [
                    {
                        'node': {
                            'id': 'gid://shopify/WebhookSubscription/1',
                            'topic': 'ORDERS_CREATE',
                            'endpoint': {'callbackUrl': 'https://api.example.com/hook'},
                        }
                    },
                    {
                        'node': {
                            'id': 'gid://shopify/WebhookSubscription/2',
                            'topic': 'ORDERS_CANCELLED',
                            'endpoint': {'callbackUrl': 'https://api.example.com/hook'},
                        }
                    },
                ]
            }
        }
        c._graphql.side_effect = [list_response]

        from stock.sync.shopify.client import ShopifyClient
        result = ShopifyClient.ensure_webhooks(c, 'https://api.example.com/hook')
        assert result['created'] == []
        assert len(result['existing']) == 2
        # Only 1 _graphql call (the list query), no creates needed
        assert c._graphql.call_count == 1

    def test_ensure_webhooks_replaces_stale_url(self):
        c = self._make_client()

        list_response = {
            'webhookSubscriptions': {
                'edges': [
                    {
                        'node': {
                            'id': 'gid://shopify/WebhookSubscription/old',
                            'topic': 'ORDERS_CREATE',
                            'endpoint': {'callbackUrl': 'https://old-api.example.com/hook'},
                        }
                    },
                ]
            }
        }
        delete_response = {
            'webhookSubscriptionDelete': {
                'deletedWebhookSubscriptionId': 'gid://shopify/WebhookSubscription/old',
                'userErrors': [],
            }
        }
        create_response = {
            'webhookSubscriptionCreate': {
                'webhookSubscription': {
                    'id': 'gid://shopify/WebhookSubscription/new',
                    'topic': 'ORDERS_CREATE',
                    'endpoint': {'callbackUrl': 'https://new-api.example.com/hook'},
                },
                'userErrors': [],
            }
        }
        # list → delete old → create ORDERS_CREATE → create ORDERS_CANCELLED
        c._graphql.side_effect = [list_response, delete_response, create_response, create_response]

        from stock.sync.shopify.client import ShopifyClient
        result = ShopifyClient.ensure_webhooks(c, 'https://new-api.example.com/hook')
        assert len(result['replaced']) == 1
        assert result['replaced'][0]['old_url'] == 'https://old-api.example.com/hook'
        assert 'ORDERS_CREATE' in result['created']
        assert 'ORDERS_CANCELLED' in result['created']


# ══════════════════════════════════════════════════════════════════
# Runner
# ══════════════════════════════════════════════════════════════════

if __name__ == '__main__':
    pytest.main([__file__, '-v'])
