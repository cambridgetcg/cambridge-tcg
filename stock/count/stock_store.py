"""RDS-backed stock count persistence.

Stores stock quantities and cost basis per SKU in the stock_inventory table.
Listing tier configuration lives in the stock_config table.
Tier caps use shopify_selling_price from cardrush_link via JOIN.

Supports dry-run preview and incremental updates.
"""

import json
import os
import re
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

import psycopg2
from psycopg2.extras import execute_batch

from stock.count.models import SaleReduction, StockRecord, StockUpdate


def _safe_table_name(name):
    """Validate table name to prevent SQL injection."""
    if not re.match(r'^[a-zA-Z_][a-zA-Z0-9_]*$', name):
        raise ValueError(f"Invalid table name: {name}")
    return name


class StockStore:
    """RDS-backed stock count store."""

    def __init__(self, conn=None):
        """Initialize with an existing connection or create one from env vars.

        Args:
            conn: Existing psycopg2 connection (caller manages lifecycle).
                  If None, creates a new connection from env vars.
        """
        if conn is not None:
            self._conn = conn
            self._owns_conn = False
        else:
            self._conn = psycopg2.connect(
                host=os.environ['PROXY_ENDPOINT'],
                user=os.environ['DB_USER'],
                password=os.environ['DB_PASSWORD'],
                port=int(os.environ.get('DB_PORT', '5432')),
                dbname=os.environ.get('DATABASE_NAME', 'op_cardrush_link'),
                connect_timeout=10,
            )
            self._owns_conn = True

    def close(self):
        """Close connection if we own it."""
        if self._owns_conn and self._conn:
            try:
                self._conn.close()
            except Exception:
                pass

    # --- Listing tiers ---

    def get_listing_tiers(self) -> Optional[dict]:
        """Get listing tier config, or None if no tiers set.

        Returns {"tiers": [{"under_gbp": N, "cap": M}, ...], "default_cap": int}
        """
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT config_key, config_value FROM stock_config "
                "WHERE config_key IN ('listing_tiers', 'listing_default_cap')"
            )
            rows = {row[0]: row[1] for row in cur.fetchall()}

        tiers = rows.get('listing_tiers')
        if tiers is None:
            return None
        default_cap = rows.get('listing_default_cap', 1)
        return {"tiers": tiers, "default_cap": default_cap}

    def set_listing_tiers(self, tiers: List[dict], default_cap: int):
        """Set listing tiers. Each tier: {"under_gbp": N, "cap": M}."""
        with self._conn.cursor() as cur:
            cur.execute(
                "INSERT INTO stock_config (config_key, config_value) "
                "VALUES ('listing_tiers', %s::jsonb) "
                "ON CONFLICT (config_key) DO UPDATE SET "
                "config_value = EXCLUDED.config_value, updated_at = NOW()",
                (json.dumps(tiers),),
            )
            cur.execute(
                "INSERT INTO stock_config (config_key, config_value) "
                "VALUES ('listing_default_cap', %s::jsonb) "
                "ON CONFLICT (config_key) DO UPDATE SET "
                "config_value = EXCLUDED.config_value, updated_at = NOW()",
                (json.dumps(default_cap),),
            )
            self._conn.commit()

    def clear_listing_tiers(self):
        """Remove listing tiers (list everything at actual qty)."""
        with self._conn.cursor() as cur:
            cur.execute(
                "DELETE FROM stock_config WHERE config_key IN ('listing_tiers', 'listing_default_cap')"
            )
            self._conn.commit()

    def _cap_for_price(self, price_gbp: float) -> Optional[int]:
        """Walk tiers and return cap for a given price. None if no tiers."""
        config = self.get_listing_tiers()
        if config is None:
            return None
        for tier in config['tiers']:
            if price_gbp < tier['under_gbp']:
                return tier['cap']
        return config['default_cap']

    def get_listed_qty(self, sku: str) -> Optional[int]:
        """Get listed quantity for a SKU.

        Uses shopify_selling_price from cardrush_link via JOIN.
        Returns min(actual_qty, tier_cap) based on price.
        Returns None if tiers are set but no price for this SKU.
        Returns actual qty if no tiers are set.
        """
        record = self.get(sku)
        if record is None:
            return 0
        config = self.get_listing_tiers()
        if config is None:
            return record.quantity

        table = _safe_table_name(os.environ.get('TABLE_NAME', 'cardrush_link'))
        with self._conn.cursor() as cur:
            cur.execute(
                f"SELECT shopify_selling_price FROM {table} WHERE sku = %s",
                (sku,),
            )
            row = cur.fetchone()

        if row is None or row[0] is None:
            return None
        price = float(row[0])
        cap = self._cap_for_price(price)
        if cap is None:
            return record.quantity
        return min(record.quantity, cap)

    def get_listed_qty_detail(self, sku: str) -> Tuple[Optional[int], str]:
        """Get listed qty with human-readable reasoning.

        Returns (listed_qty, reason_string).
        """
        record = self.get(sku)
        if record is None:
            return (0, "no stock record")
        config = self.get_listing_tiers()
        if config is None:
            return (record.quantity, "no tiers set")

        table = _safe_table_name(os.environ.get('TABLE_NAME', 'cardrush_link'))
        with self._conn.cursor() as cur:
            cur.execute(
                f"SELECT shopify_selling_price FROM {table} WHERE sku = %s",
                (sku,),
            )
            row = cur.fetchone()

        if row is None or row[0] is None:
            return (None, "no price in cardrush_link")
        price = float(row[0])
        for tier in config['tiers']:
            if price < tier['under_gbp']:
                cap = tier['cap']
                listed = min(record.quantity, cap)
                return (listed, f"tier: under \u00a3{tier['under_gbp']} \u2192 max {cap}, price: \u00a3{price:.2f}")
        cap = config['default_cap']
        listed = min(record.quantity, cap)
        return (listed, f"tier: \u00a3{config['tiers'][-1]['under_gbp']}+ \u2192 max {cap}, price: \u00a3{price:.2f}")

    def get(self, sku: str) -> Optional[StockRecord]:
        """Get stock record for a SKU, or None."""
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT sku, quantity, total_cost_yen, purchased_qty, last_updated "
                "FROM stock_inventory WHERE sku = %s",
                (sku,),
            )
            row = cur.fetchone()
        if row is None:
            return None
        return StockRecord(
            sku=row[0],
            quantity=row[1],
            total_cost_yen=row[2],
            purchased_qty=row[3],
            last_updated=row[4].isoformat() if row[4] else '',
        )

    def get_all(self) -> List[StockRecord]:
        """Get all stock records, sorted by SKU."""
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT sku, quantity, total_cost_yen, purchased_qty, last_updated "
                "FROM stock_inventory ORDER BY sku"
            )
            rows = cur.fetchall()
        return [
            StockRecord(
                sku=row[0],
                quantity=row[1],
                total_cost_yen=row[2],
                purchased_qty=row[3],
                last_updated=row[4].isoformat() if row[4] else '',
            )
            for row in rows
        ]

    def apply_updates(self, updates: List[StockUpdate], dry_run: bool = False) -> List[StockRecord]:
        """Apply stock updates (add quantities). Returns updated records.

        Args:
            updates: List of StockUpdate with quantities to add.
            dry_run: If True, compute results without writing to DB.
        """
        now = datetime.now(timezone.utc)
        results = []

        if dry_run:
            for update in updates:
                existing = self.get(update.sku)
                old_qty = existing.quantity if existing else 0
                old_cost = existing.total_cost_yen if existing else 0
                results.append(StockRecord(
                    sku=update.sku,
                    quantity=old_qty + update.quantity_to_add,
                    total_cost_yen=old_cost + update.cost_yen_total,
                    last_updated=now.isoformat(),
                ))
            return results

        with self._conn.cursor() as cur:
            for update in updates:
                cur.execute(
                    "INSERT INTO stock_inventory (sku, quantity, total_cost_yen, purchased_qty, last_updated) "
                    "VALUES (%s, %s, %s, %s, %s) "
                    "ON CONFLICT (sku) DO UPDATE SET "
                    "quantity = stock_inventory.quantity + EXCLUDED.quantity, "
                    "total_cost_yen = stock_inventory.total_cost_yen + EXCLUDED.total_cost_yen, "
                    "purchased_qty = stock_inventory.purchased_qty + EXCLUDED.purchased_qty, "
                    "last_updated = EXCLUDED.last_updated "
                    "RETURNING sku, quantity, total_cost_yen, purchased_qty, last_updated",
                    (update.sku, update.quantity_to_add, update.cost_yen_total,
                     update.quantity_to_add, now),
                )
                row = cur.fetchone()
                results.append(StockRecord(
                    sku=row[0],
                    quantity=row[1],
                    total_cost_yen=row[2],
                    purchased_qty=row[3],
                    last_updated=row[4].isoformat() if row[4] else '',
                ))
            self._conn.commit()

        return results

    def apply_reductions(self, reductions: List[SaleReduction], dry_run: bool = False) -> List[dict]:
        """Reduce stock from sales. Clamps to 0, never touches purchased_qty.

        Args:
            reductions: List of SaleReduction with quantities sold.
            dry_run: If True, compute results without writing to DB.

        Returns:
            List of {sku, old_qty, new_qty, clamped} dicts.
        """
        now = datetime.now(timezone.utc)
        results = []

        with self._conn.cursor() as cur:
            for r in reductions:
                cur.execute(
                    "SELECT quantity FROM stock_inventory WHERE sku = %s",
                    (r.sku,),
                )
                row = cur.fetchone()
                if row is None:
                    results.append({
                        'sku': r.sku,
                        'old_qty': 0,
                        'new_qty': 0,
                        'clamped': False,
                        'skipped': True,
                    })
                    continue

                old_qty = row[0]
                new_qty = old_qty - r.quantity_sold
                clamped = new_qty < 0
                if clamped:
                    new_qty = 0

                results.append({
                    'sku': r.sku,
                    'old_qty': old_qty,
                    'new_qty': new_qty,
                    'clamped': clamped,
                    'skipped': False,
                })

                if not dry_run:
                    cur.execute(
                        "UPDATE stock_inventory SET quantity = %s, last_updated = %s WHERE sku = %s",
                        (new_qty, now, r.sku),
                    )

            if not dry_run and any(not r.get('skipped') for r in results):
                self._conn.commit()

        return results

    def backfill_cost(self, dry_run: bool = False) -> List[dict]:
        """Backfill cost for legacy (seeded) stock using purchase price data.

        For each SKU where purchased_qty > 0 and total_qty > purchased_qty,
        assumes seeded units cost the same per-unit as purchased units.

        Returns list of {sku, old_cost, new_cost, unit_price, seeded_qty} for changed SKUs.
        """
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT sku, quantity, total_cost_yen, purchased_qty "
                "FROM stock_inventory "
                "WHERE purchased_qty > 0 AND total_cost_yen > 0 AND quantity > purchased_qty"
            )
            rows = cur.fetchall()

        changes = []
        for sku, total_qty, total_cost, purchased_qty in rows:
            unit_price = total_cost / purchased_qty
            new_cost = round(total_qty * unit_price)
            seeded_qty = total_qty - purchased_qty

            if new_cost != total_cost:
                changes.append({
                    'sku': sku,
                    'old_cost': total_cost,
                    'new_cost': new_cost,
                    'unit_price': unit_price,
                    'seeded_qty': seeded_qty,
                })

        if not dry_run and changes:
            with self._conn.cursor() as cur:
                for c in changes:
                    cur.execute(
                        "UPDATE stock_inventory SET total_cost_yen = %s WHERE sku = %s",
                        (c['new_cost'], c['sku']),
                    )
                self._conn.commit()

        return changes

    def seed(self, records: Dict[str, int], source: str = "zoho"):
        """Seed stock from an external source (e.g. Zoho export).

        Replaces all existing stock data. Cost is unknown from Zoho
        so total_cost_yen is set to 0 for seeded records.

        Args:
            records: {sku: quantity} mapping.
            source: Label for the import source.
        """
        now = datetime.now(timezone.utc)
        with self._conn.cursor() as cur:
            cur.execute("DELETE FROM stock_inventory")
            rows = [
                (sku, qty, 0, 0, now)
                for sku, qty in sorted(records.items())
            ]
            execute_batch(
                cur,
                "INSERT INTO stock_inventory (sku, quantity, total_cost_yen, purchased_qty, last_updated) "
                "VALUES (%s, %s, %s, %s, %s)",
                rows,
                page_size=100,
            )
            self._conn.commit()

    def export_json(self, path: str):
        """Export RDS stock to JSON file for offline backup.

        Produces same format as the old stock_data.json.
        """
        records = self.get_all()
        tier_config = self.get_listing_tiers()

        metadata = {
            'exported_at': datetime.now(timezone.utc).isoformat(),
            'source': 'rds',
        }
        if tier_config:
            metadata['listing_tiers'] = tier_config['tiers']
            metadata['listing_default_cap'] = tier_config['default_cap']

        stock = {}
        for r in records:
            stock[r.sku] = {
                'quantity': r.quantity,
                'total_cost_yen': r.total_cost_yen,
                'purchased_qty': r.purchased_qty,
                'last_updated': r.last_updated,
            }

        data = {'metadata': metadata, 'stock': stock}
        with open(path, 'w') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
