"""Cambridge TCG — Stock Inventory Admin

Streamlit frontend for viewing/editing stock inventory via Lambda invoke (boto3).

Usage:
    streamlit run stock/inventory/app.py

Requires:
    - AWS credentials configured (same as used for deploy)
    - pip install streamlit pandas boto3 python-dotenv
"""

import json
import os
import re
import sys
from pathlib import Path

import boto3
import pandas as pd
import streamlit as st
from dotenv import load_dotenv

# Load .env from repo root
_repo_root = Path(__file__).resolve().parent.parent.parent
load_dotenv(_repo_root / ".env")

FUNCTION_NAME = os.environ.get("STOCK_LAMBDA_NAME", "stock-inventory-api")
REGION = os.environ.get("AWS_REGION", "us-east-1")

_lambda_client = boto3.client("lambda", region_name=REGION)

st.set_page_config(
    page_title="Stock Inventory — Cambridge TCG",
    page_icon="\U0001f4e6",
    layout="wide",
)


# --- Lambda invoke helpers ---

def _invoke(method: str, path: str, body: dict | None = None, params: dict | None = None) -> dict:
    """Invoke the Lambda with a Function URL-style event."""
    # Build query string
    qs = params or {}
    event = {
        "requestContext": {"http": {"method": method, "path": path}},
        "headers": {"x-api-key": "__local__"},
        "queryStringParameters": qs if qs else None,
    }
    if body is not None:
        event["body"] = json.dumps(body)

    resp = _lambda_client.invoke(
        FunctionName=FUNCTION_NAME,
        InvocationType="RequestResponse",
        Payload=json.dumps(event),
    )
    payload = json.loads(resp["Payload"].read())

    if payload.get("statusCode", 200) >= 400:
        error = json.loads(payload.get("body", "{}")).get("error", "Unknown error")
        raise RuntimeError(f"Lambda {payload['statusCode']}: {error}")

    return json.loads(payload.get("body", "{}"))


@st.cache_data(ttl=30)
def fetch_inventory() -> list[dict]:
    """Fetch all inventory from Lambda."""
    return _invoke("GET", "/inventory")["items"]


def save_absolute(sku: str, quantity: int | None = None, total_cost_yen: int | None = None) -> dict:
    """Update absolute qty/cost for a single SKU."""
    body = {"sku": sku}
    if quantity is not None:
        body["quantity"] = quantity
    if total_cost_yen is not None:
        body["total_cost_yen"] = total_cost_yen
    return _invoke("POST", "/inventory/update", body=body)


def submit_order(items: list[dict]) -> dict:
    """Submit a purchase order via POST /inventory/order."""
    return _invoke("POST", "/inventory/order", body={"items": items})


def get_promo_next(card_number: str) -> dict:
    """Get next available promo SKU version."""
    return _invoke("POST", "/inventory/promo/next", body={"card_number": card_number})


@st.cache_data(ttl=300)
def fetch_catalog_skus() -> list[str]:
    """Fetch all SKUs from cardrush_link (full card catalog)."""
    return _invoke("GET", "/inventory/catalog")["skus"]


@st.cache_data(ttl=60)
def fetch_restock() -> list[dict]:
    """Fetch SKUs needing restock (qty < listing cap)."""
    return _invoke("GET", "/inventory/restock")["items"]


@st.cache_data(ttl=30)
def fetch_sales(days: int = 30, sku: str = "", platform: str = "") -> dict:
    """Fetch sales events from Lambda."""
    params = {"days": str(days)}
    if sku:
        params["sku"] = sku
    if platform:
        params["platform"] = platform
    return _invoke("GET", "/inventory/sales", params=params)


def _extract_set(sku: str) -> str:
    """Extract set code from SKU. OP-P-* cards grouped into PROMO."""
    if sku.startswith('OP-P-'):
        return "PROMO"
    m = re.match(r'^[A-Z]+-([A-Z0-9]+)-', sku)
    return m.group(1) if m else "OTHER"


# --- Column registry ---
# Toggleable columns: (display_name, default_on)
# SKU is always shown and not toggleable.
TOGGLEABLE_COLUMNS = [
    ("Set", True),
    ("Qty", True),
    ("Listed", False),
    ("Cost \u00a5", True),
    ("Avg \u00a5", True),
    ("CR \u00a5", False),
    ("CR A- \u00a5", False),
    ("Diff \u00a5", False),
    ("Diff %", False),
    ("Price \u00a3", True),
    ("Vel", False),
    ("Margin", False),
    ("Range \u00a3", False),
    ("Sold", False),
    ("Rev \u00a3", False),
    ("Last Sale", False),
    ("CardRush", False),
    ("CR A-", False),
    ("Stock", False),
    ("A- Stock", False),
]
ALL_TOGGLE_NAMES = [name for name, _ in TOGGLEABLE_COLUMNS]
DEFAULT_ON = [name for name, on in TOGGLEABLE_COLUMNS if on]


# --- Sidebar ---

st.sidebar.title("Filters")

search = st.sidebar.text_input("Search SKU", placeholder="e.g. luffy, OP05")

try:
    all_items = fetch_inventory()
except Exception as e:
    st.error(f"Failed to fetch inventory: {e}")
    st.stop()

sets = sorted(set(_extract_set(item["sku"]) for item in all_items))
set_options = ["All"] + sets
selected_set = st.sidebar.selectbox("Set", set_options)

stock_filter = st.sidebar.radio("Show", ["All", "In Stock", "Zero Stock"], index=0)

visible_columns = st.sidebar.multiselect(
    "Columns", options=ALL_TOGGLE_NAMES, default=DEFAULT_ON,
)

if st.sidebar.button("Refresh", use_container_width=True):
    st.cache_data.clear()
    st.rerun()



# --- Apply filters ---

items = all_items

if search:
    search_lower = search.lower()
    items = [i for i in items if search_lower in i["sku"].lower()]

if selected_set != "All":
    items = [i for i in items if _extract_set(i["sku"]) == selected_set]

if stock_filter == "In Stock":
    items = [i for i in items if i["quantity"] > 0]
elif stock_filter == "Zero Stock":
    items = [i for i in items if i["quantity"] == 0]


# --- Title ---

st.title("Stock Inventory")


# --- Tabs ---

tab_inventory, tab_order, tab_restock, tab_sales, tab_cardrush, tab_push = st.tabs(["Inventory", "Record Order", "Restock", "Sales", "CardRush", "Stock Push"])


# ==================== TAB 1: Inventory ====================

with tab_inventory:

    # --- Metrics ---

    total_skus = len(items)
    total_qty = sum(i["quantity"] for i in items)
    total_cost_yen = sum(i["total_cost_yen"] for i in items if i["quantity"] > 0)
    total_market_yen = sum(
        i["price_yen"] * i["quantity"] if i["price_yen"]
        else i["total_cost_yen"] if i["quantity"] > 0  # fallback: purchase cost for in-stock only
        else 0
        for i in items
    )
    total_value_gbp = sum(
        (i["selling_price_gbp"] or 0) * i["quantity"]
        for i in items
    )
    in_stock = sum(1 for i in items if i["quantity"] > 0)
    zero_stock = total_skus - in_stock

    c1, c2, c3, c4, c5, c6, c7 = st.columns(7)
    c1.metric("SKUs", f"{total_skus:,}")
    c2.metric("Total Qty", f"{total_qty:,}")
    c3.metric("Held Cost", f"\u00a5{total_cost_yen:,.0f}")
    c4.metric("Market Value", f"\u00a5{total_market_yen:,.0f}")
    c5.metric("Retail Value", f"\u00a3{total_value_gbp:,.2f}")
    c6.metric("In Stock", f"{in_stock:,}")
    c7.metric("Zero Stock", f"{zero_stock:,}")

    # --- Data table ---

    if not items:
        st.info("No items match the current filters.")
    else:
        df = pd.DataFrame(items)
        df["set"] = df["sku"].apply(_extract_set)
        df["avg_cost_yen"] = df.apply(
            lambda r: round(r["total_cost_yen"] / r["quantity"]) if r["quantity"] > 0 and r["total_cost_yen"] > 0 else 0,
            axis=1,
        )

        # Ensure optional columns exist (Lambda may not return them yet)
        for col, default in [("velocity", 0), ("margin_pct", None), ("price_range_gbp", None),
                             ("total_sold", 0), ("revenue_30d", 0), ("last_sale", None),
                             ("cardrush_url", None), ("cardrush_url_subgrade", None),
                             ("cardrush_stock", None), ("cardrush_stock_subgrade", None),
                             ("price_yen_subgrade", None)]:
            if col not in df.columns:
                df[col] = default

        # Format margin, price range, and last sale for display
        df["margin_display"] = df["margin_pct"].apply(lambda v: f"{v:.0f}%" if v is not None else "\u2014")
        df["range_display"] = df["price_range_gbp"].apply(lambda v: v if v else "\u2014")
        df["last_sale_display"] = df["last_sale"].apply(
            lambda v: v[:10] if v else "\u2014"
        )

        # Computed: price difference between conditions
        df["price_diff"] = df.apply(
            lambda r: int(r["price_yen"] - r["price_yen_subgrade"])
            if pd.notna(r["price_yen"]) and pd.notna(r["price_yen_subgrade"]) else None,
            axis=1,
        )
        df["price_diff_pct"] = df.apply(
            lambda r: round((r["price_yen"] - r["price_yen_subgrade"]) / r["price_yen"] * 100)
            if pd.notna(r["price_yen"]) and pd.notna(r["price_yen_subgrade"]) and r["price_yen"] != 0 else None,
            axis=1,
        )

        # Reorder and rename for display
        all_internal = ["sku", "set", "quantity", "listed_qty", "total_cost_yen", "avg_cost_yen", "price_yen", "price_yen_subgrade", "price_diff", "price_diff_pct", "selling_price_gbp", "velocity", "margin_display", "range_display", "total_sold", "revenue_30d", "last_sale_display", "cardrush_url", "cardrush_url_subgrade", "cardrush_stock", "cardrush_stock_subgrade"]
        all_display = ["SKU", "Set", "Qty", "Listed", "Cost \u00a5", "Avg \u00a5", "CR \u00a5", "CR A- \u00a5", "Diff \u00a5", "Diff %", "Price \u00a3", "Vel", "Margin", "Range \u00a3", "Sold", "Rev \u00a3", "Last Sale", "CardRush", "CR A-", "Stock", "A- Stock"]
        full_df = df[all_internal].copy()
        full_df.columns = all_display

        # Filter to SKU + visible columns
        shown_cols = ["SKU"] + [c for c in all_display[1:] if c in visible_columns]
        display_df = full_df[shown_cols].copy()

        # Column configs — full set, filtered to visible
        all_column_config = {
            "SKU": st.column_config.TextColumn("SKU", disabled=True),
            "Set": st.column_config.TextColumn("Set", disabled=True),
            "Qty": st.column_config.NumberColumn("Qty", min_value=0, step=1, format="%d"),
            "Listed": st.column_config.NumberColumn("Listed", disabled=True, format="%d"),
            "Cost \u00a5": st.column_config.NumberColumn("Cost \u00a5", min_value=0, step=1, format="%d"),
            "Avg \u00a5": st.column_config.NumberColumn("Avg \u00a5", disabled=True, format="%d"),
            "CR \u00a5": st.column_config.NumberColumn("CR \u00a5", disabled=True, format="%d"),
            "CR A- \u00a5": st.column_config.NumberColumn("CR A- \u00a5", disabled=True, format="%d"),
            "Diff \u00a5": st.column_config.NumberColumn("Diff \u00a5", disabled=True, format="%d"),
            "Diff %": st.column_config.NumberColumn("Diff %", disabled=True, format="%d%%"),
            "Price \u00a3": st.column_config.NumberColumn("Price \u00a3", disabled=True, format="%.2f"),
            "Vel": st.column_config.NumberColumn("Vel", disabled=True, format="%d"),
            "Margin": st.column_config.TextColumn("Margin", disabled=True),
            "Range \u00a3": st.column_config.TextColumn("Range \u00a3", disabled=True),
            "Sold": st.column_config.NumberColumn("Sold", disabled=True, format="%d"),
            "Rev \u00a3": st.column_config.NumberColumn("Rev \u00a3", disabled=True, format="\u00a3%.2f"),
            "Last Sale": st.column_config.TextColumn("Last Sale", disabled=True),
            "CardRush": st.column_config.LinkColumn("CardRush", display_text="Link", disabled=True),
            "CR A-": st.column_config.LinkColumn("CR A-", display_text="Link", disabled=True),
            "Stock": st.column_config.NumberColumn("Stock", disabled=True, format="%d"),
            "A- Stock": st.column_config.NumberColumn("A- Stock", disabled=True, format="%d"),
        }
        active_config = {k: v for k, v in all_column_config.items() if k in shown_cols}

        # Data editor — Qty and Cost editable (when visible), rest read-only
        edited_df = st.data_editor(
            display_df,
            column_config=active_config,
            use_container_width=True,
            num_rows="fixed",
            hide_index=True,
            key="stock_editor",
        )

        # --- Detect changes (only when editable columns are visible) ---

        qty_visible = "Qty" in shown_cols
        cost_visible = "Cost \u00a5" in shown_cols
        changes = []
        if qty_visible or cost_visible:
            for idx in range(len(display_df)):
                sku = display_df.iloc[idx]["SKU"]
                orig_qty = int(display_df.iloc[idx]["Qty"]) if qty_visible else 0
                new_qty = int(edited_df.iloc[idx]["Qty"]) if qty_visible else 0
                orig_cost = int(display_df.iloc[idx]["Cost \u00a5"]) if cost_visible else 0
                new_cost = int(edited_df.iloc[idx]["Cost \u00a5"]) if cost_visible else 0

                if new_qty != orig_qty or new_cost != orig_cost:
                    changes.append({
                        "sku": sku,
                        "old_qty": orig_qty,
                        "new_qty": new_qty,
                        "old_cost": orig_cost,
                        "new_cost": new_cost,
                    })

        if changes:
            with st.expander(f"Pending changes ({len(changes)})", expanded=True):
                change_df = pd.DataFrame(changes)
                change_df.columns = ["SKU", "Old Qty", "New Qty", "Old Cost \u00a5", "New Cost \u00a5"]
                st.dataframe(change_df, use_container_width=True, hide_index=True)

            col_save, col_discard = st.columns([1, 1])
            with col_save:
                if st.button("Save Changes", type="primary", use_container_width=True):
                    try:
                        for c in changes:
                            payload = {"sku": c["sku"]}
                            if c["new_qty"] != c["old_qty"]:
                                payload["quantity"] = c["new_qty"]
                            if c["new_cost"] != c["old_cost"]:
                                payload["total_cost_yen"] = c["new_cost"]
                            save_absolute(**payload)
                        st.success(f"Saved {len(changes)} change(s)")
                        st.cache_data.clear()
                        st.rerun()
                    except Exception as e:
                        st.error(f"Save failed: {e}")

            with col_discard:
                if st.button("Discard", use_container_width=True):
                    st.cache_data.clear()
                    st.rerun()


# ==================== TAB 2: Record Order ====================

with tab_order:

    # Initialize session state for order items
    if "order_items" not in st.session_state:
        st.session_state["order_items"] = []

    # Build SKU list from full catalog (cardrush_link) for autocomplete
    try:
        catalog_skus = fetch_catalog_skus()
    except Exception:
        catalog_skus = []
    # Union of catalog + inventory SKUs (catalog may lack promo SKUs that are inventory-only)
    inventory_skus = {i["sku"] for i in all_items}
    sku_list = sorted(set(catalog_skus) | inventory_skus)
    # Build lookup for current avg cost
    inv_lookup = {i["sku"]: i for i in all_items}

    # --- Add Item form ---

    st.subheader("Add Item")
    with st.form("add_item_form", clear_on_submit=True):
        col_sku, col_qty, col_price = st.columns([3, 1, 1])
        with col_sku:
            selected_sku = st.selectbox("SKU", options=sku_list, index=None, placeholder="Select SKU...")
        with col_qty:
            item_qty = st.number_input("Qty", min_value=1, value=1, step=1, key="add_qty")
        with col_price:
            item_price = st.number_input("Unit Price \u00a5", min_value=0, value=0, step=100, key="add_price")

        if st.form_submit_button("Add to Order"):
            if selected_sku and item_qty > 0 and item_price > 0:
                st.session_state["order_items"].append({
                    "sku": selected_sku,
                    "quantity": item_qty,
                    "unit_price_yen": item_price,
                })
                st.rerun()
            else:
                st.warning("Fill in SKU, qty (> 0), and unit price (> 0).")

    # --- Add Promo Card form ---

    st.subheader("Add Promo Card")
    with st.form("add_promo_form", clear_on_submit=True):
        col_card, col_gen = st.columns([2, 1])
        with col_card:
            card_number = st.text_input("Card Number", placeholder="e.g. 001", max_chars=5)
        with col_gen:
            generate_pressed = st.form_submit_button("Generate SKU")

    # Show generated promo SKU outside the form so it persists
    if generate_pressed and card_number:
        try:
            promo_resp = get_promo_next(card_number)
            st.session_state["promo_next_sku"] = promo_resp["next_sku"]
            st.session_state["promo_existing"] = promo_resp.get("existing", [])
        except Exception as e:
            st.error(f"Failed to get promo SKU: {e}")

    if st.session_state.get("promo_next_sku"):
        next_sku = st.session_state["promo_next_sku"]
        existing = st.session_state.get("promo_existing", [])

        st.info(f"Next available: **{next_sku}**")
        if existing:
            st.caption(f"Existing: {', '.join(existing)}")

        with st.form("add_promo_item_form", clear_on_submit=True):
            col_pq, col_pp = st.columns(2)
            with col_pq:
                promo_qty = st.number_input("Qty", min_value=1, value=1, step=1, key="promo_qty")
            with col_pp:
                promo_price = st.number_input("Unit Price \u00a5", min_value=0, value=0, step=100, key="promo_price")

            if st.form_submit_button("Add Promo to Order"):
                if promo_qty > 0 and promo_price > 0:
                    st.session_state["order_items"].append({
                        "sku": next_sku,
                        "quantity": promo_qty,
                        "unit_price_yen": promo_price,
                    })
                    st.session_state.pop("promo_next_sku", None)
                    st.session_state.pop("promo_existing", None)
                    st.rerun()
                else:
                    st.warning("Qty and unit price must be > 0.")

    # --- Current Order ---

    order_items = st.session_state["order_items"]

    if order_items:
        st.subheader("Current Order")

        # Build display rows
        order_rows = []
        for i, item in enumerate(order_items):
            order_rows.append({
                "#": i + 1,
                "SKU": item["sku"],
                "Qty": item["quantity"],
                "Unit \u00a5": f"{item['unit_price_yen']:,}",
                "Total \u00a5": f"{item['quantity'] * item['unit_price_yen']:,}",
            })

        order_df = pd.DataFrame(order_rows)
        st.dataframe(order_df, use_container_width=True, hide_index=True)

        total_items = sum(item["quantity"] for item in order_items)
        total_order_yen = sum(item["quantity"] * item["unit_price_yen"] for item in order_items)
        st.markdown(f"**Total: {total_items} items, \u00a5{total_order_yen:,}**")

        # Remove item buttons
        cols = st.columns(min(len(order_items), 6))
        for i, item in enumerate(order_items):
            with cols[i % len(cols)]:
                if st.button(f"Remove {item['sku']}", key=f"rm_{i}"):
                    st.session_state["order_items"].pop(i)
                    st.rerun()

        # --- Avg Cost Impact Preview ---

        st.subheader("Avg Cost Impact Preview")
        preview_rows = []
        for item in order_items:
            sku = item["sku"]
            add_qty = item["quantity"]
            add_cost = item["quantity"] * item["unit_price_yen"]

            existing = inv_lookup.get(sku)
            if existing:
                old_cost = existing["total_cost_yen"]
                old_qty = existing["quantity"]
                old_avg = round(old_cost / old_qty) if old_qty > 0 and old_cost > 0 else 0
                new_cost = old_cost + add_cost
                new_qty = old_qty + add_qty
                new_avg = round(new_cost / new_qty) if new_qty > 0 else 0
                delta = new_avg - old_avg
                preview_rows.append({
                    "SKU": sku,
                    "Before": f"\u00a5{old_avg:,}",
                    "After": f"\u00a5{new_avg:,}",
                    "Delta": f"{'+' if delta >= 0 else ''}\u00a5{delta:,}/unit",
                })
            else:
                unit_price = item["unit_price_yen"]
                preview_rows.append({
                    "SKU": sku,
                    "Before": "\u2014",
                    "After": f"\u00a5{unit_price:,}",
                    "Delta": "NEW",
                })

        preview_df = pd.DataFrame(preview_rows)
        st.dataframe(preview_df, use_container_width=True, hide_index=True)

        # --- Submit / Clear ---

        col_submit, col_clear = st.columns(2)
        with col_submit:
            if st.button("Submit Order", type="primary", use_container_width=True):
                try:
                    result = submit_order(order_items)
                    st.success(
                        f"Order submitted: {result['total_items']} items, "
                        f"\u00a5{result['total_cost_yen']:,} total"
                    )
                    for r in result.get("results", []):
                        st.write(f"  {r['sku']}: {r['action']} (qty={r['quantity']}, cost=\u00a5{r['total_cost_yen']:,})")
                    st.session_state["order_items"] = []
                    st.cache_data.clear()
                except Exception as e:
                    st.error(f"Order failed: {e}")

        with col_clear:
            if st.button("Clear Order", use_container_width=True):
                st.session_state["order_items"] = []
                st.rerun()
    else:
        st.info("No items in order. Use the forms above to add items.")


# ==================== TAB 3: Restock ====================

with tab_restock:

    try:
        restock_items = fetch_restock()
    except Exception as e:
        st.error(f"Failed to fetch restock data: {e}")
        restock_items = []

    if restock_items:
        # --- Metrics ---
        total_units = sum(r["restock_qty"] for r in restock_items)
        total_capital = sum((r["price_yen"] or 0) * r["restock_qty"] for r in restock_items)

        rc1, rc2, rc3 = st.columns(3)
        rc1.metric("SKUs to Restock", len(restock_items))
        rc2.metric("Units Needed", total_units)
        rc3.metric("Restock Capital", f"\u00a5{total_capital:,}")

        # --- Data table ---
        table_rows = []
        for r in restock_items:
            table_rows.append({
                "SKU": r["sku"],
                "Qty": r["quantity"],
                "Cap": r["listing_cap"],
                "Restock": r["restock_qty"],
                "CR \u00a5": r["price_yen"],
                "CR A- \u00a5": r.get("price_yen_subgrade"),
                "Stock": r.get("cardrush_stock"),
                "A- Stock": r.get("cardrush_stock_subgrade"),
                "CardRush": r["cardrush_url"],
                "CR A-": r.get("cardrush_url_subgrade"),
            })

        restock_df = pd.DataFrame(table_rows)
        st.dataframe(
            restock_df,
            use_container_width=True,
            hide_index=True,
            column_config={
                "CR \u00a5": st.column_config.NumberColumn(format="%d"),
                "CR A- \u00a5": st.column_config.NumberColumn("CR A- \u00a5", format="%d"),
                "Stock": st.column_config.NumberColumn("Stock", format="%d"),
                "A- Stock": st.column_config.NumberColumn("A- Stock", format="%d"),
                "CardRush": st.column_config.LinkColumn("CardRush", display_text="Link"),
                "CR A-": st.column_config.LinkColumn("CR A-", display_text="Link"),
            },
        )

        # --- Quick Restock ---
        st.subheader("Quick Restock")
        restock_skus = [r["sku"] for r in restock_items]
        restock_lookup = {r["sku"]: r for r in restock_items}

        qr_sku = st.selectbox("SKU to restock", options=restock_skus, index=None, placeholder="Select SKU...", key="qr_sku")

        if qr_sku:
            info = restock_lookup[qr_sku]
            default_price = info["price_yen"] or 0

            col_rq, col_rp = st.columns(2)
            with col_rq:
                qr_qty = st.number_input("Qty", min_value=1, value=info["restock_qty"], step=1, key="qr_qty")
            with col_rp:
                qr_price = st.number_input("Unit Price \u00a5", min_value=0, value=default_price, step=100, key="qr_price")

            if info.get("cardrush_url"):
                st.markdown(f"[CardRush listing]({info['cardrush_url']})")

            if st.button("Add to Order", key="qr_add"):
                if qr_qty > 0 and qr_price > 0:
                    if "order_items" not in st.session_state:
                        st.session_state["order_items"] = []
                    st.session_state["order_items"].append({
                        "sku": qr_sku,
                        "quantity": qr_qty,
                        "unit_price_yen": qr_price,
                    })
                    st.success(f"Added {qr_qty}x {qr_sku} to order")
                else:
                    st.warning("Qty and unit price must be > 0.")
    else:
        st.info("All SKUs are fully stocked to their listing caps.")


# ==================== TAB 4: Sales ====================

with tab_sales:

    # --- Filters ---
    sc1, sc2, sc3 = st.columns(3)
    with sc1:
        sales_days = st.selectbox("Period", [7, 14, 30, 60, 90, 365], index=2, format_func=lambda d: f"Last {d} days")
    with sc2:
        sales_platform = st.selectbox("Platform", ["All", "shopify", "ebay"], index=0)
    with sc3:
        sales_sku = st.text_input("SKU filter", placeholder="e.g. OP05, luffy", key="sales_sku")

    platform_param = "" if sales_platform == "All" else sales_platform

    try:
        sales_data = fetch_sales(days=sales_days, sku=sales_sku, platform=platform_param)
    except Exception as e:
        st.error(f"Failed to fetch sales: {e}")
        sales_data = None

    if sales_data:
        events = sales_data["events"]

        # --- Metrics ---
        sm1, sm2, sm3, sm4 = st.columns(4)
        sm1.metric("Orders", f"{sales_data['total_orders']:,}")
        sm2.metric("Units Sold", f"{sales_data['total_units']:,}")
        sm3.metric("Revenue", f"\u00a3{sales_data['total_revenue']:,.2f}")
        avg_order = sales_data['total_revenue'] / sales_data['total_orders'] if sales_data['total_orders'] > 0 else 0
        sm4.metric("Avg Order", f"\u00a3{avg_order:,.2f}")

        if events:
            edf = pd.DataFrame(events)
            edf["created_at"] = pd.to_datetime(edf["created_at"])
            edf["set"] = edf["sku"].apply(_extract_set)

            # --- Daily revenue chart ---
            st.subheader("Daily Revenue")
            daily = edf.copy()
            daily["date"] = daily["created_at"].dt.date
            daily_rev = daily.groupby("date")["total_gbp"].sum().reset_index()
            daily_rev.columns = ["Date", "Revenue \u00a3"]
            daily_rev = daily_rev.set_index("Date").sort_index()
            st.bar_chart(daily_rev)

            # --- Top SKUs ---
            st.subheader("Top SKUs")
            sku_agg = edf.groupby("sku").agg(
                units=("quantity", "sum"),
                revenue=("total_gbp", "sum"),
                orders=("order_id", "nunique"),
            ).sort_values("revenue", ascending=False).head(20).reset_index()
            sku_agg.columns = ["SKU", "Units", "Revenue \u00a3", "Orders"]
            st.dataframe(
                sku_agg,
                use_container_width=True,
                hide_index=True,
                column_config={
                    "Revenue \u00a3": st.column_config.NumberColumn("Revenue \u00a3", format="\u00a3%.2f"),
                },
            )

            # --- Top Sets ---
            st.subheader("Top Sets")
            set_agg = edf.groupby("set").agg(
                units=("quantity", "sum"),
                revenue=("total_gbp", "sum"),
                skus=("sku", "nunique"),
            ).sort_values("revenue", ascending=False).reset_index()
            set_agg.columns = ["Set", "Units", "Revenue \u00a3", "Unique SKUs"]
            st.dataframe(
                set_agg,
                use_container_width=True,
                hide_index=True,
                column_config={
                    "Revenue \u00a3": st.column_config.NumberColumn("Revenue \u00a3", format="\u00a3%.2f"),
                },
            )

            # --- Platform split ---
            st.subheader("Platform Split")
            plat_agg = edf.groupby("platform").agg(
                units=("quantity", "sum"),
                revenue=("total_gbp", "sum"),
                orders=("order_id", "nunique"),
            ).reset_index()
            plat_agg.columns = ["Platform", "Units", "Revenue \u00a3", "Orders"]
            st.dataframe(
                plat_agg,
                use_container_width=True,
                hide_index=True,
                column_config={
                    "Revenue \u00a3": st.column_config.NumberColumn("Revenue \u00a3", format="\u00a3%.2f"),
                },
            )

            # --- Raw events table ---
            with st.expander(f"All events ({len(events)})"):
                display_edf = edf[["created_at", "platform", "order_id", "sku", "quantity", "unit_price_gbp", "total_gbp"]].copy()
                display_edf["created_at"] = display_edf["created_at"].dt.strftime("%Y-%m-%d %H:%M")
                display_edf.columns = ["Date", "Platform", "Order ID", "SKU", "Qty", "Unit \u00a3", "Total \u00a3"]
                st.dataframe(
                    display_edf,
                    use_container_width=True,
                    hide_index=True,
                    column_config={
                        "Unit \u00a3": st.column_config.NumberColumn("Unit \u00a3", format="\u00a3%.2f"),
                        "Total \u00a3": st.column_config.NumberColumn("Total \u00a3", format="\u00a3%.2f"),
                    },
                )
        else:
            st.info("No sales events found for the selected filters.")


# ==================== TAB 5: CardRush ====================

with tab_cardrush:

    cr_items = [i for i in all_items if i.get("cardrush_url") or i.get("cardrush_url_subgrade")]

    if cr_items:
        # --- Metrics ---
        total_with_url = len([i for i in all_items if i.get("cardrush_url")])
        total_with_sub = len([i for i in all_items if i.get("cardrush_url_subgrade")])
        total_missing = len([i for i in all_items if i["sku"].startswith("OP-") and not i.get("cardrush_url")])
        total_in_stock = len([i for i in cr_items if (i.get("cardrush_stock") or 0) > 0])

        cc1, cc2, cc3, cc4 = st.columns(4)
        cc1.metric("With CardRush Link", total_with_url)
        cc2.metric("With A- Link", total_with_sub)
        cc3.metric("OP Missing Link", total_missing)
        cc4.metric("CR In Stock", total_in_stock)

        # --- Set filter ---
        cr_sets = sorted(set(_extract_set(i["sku"]) for i in cr_items))
        cr_set_options = ["All"] + cr_sets
        cr_selected_set = st.selectbox("Set", cr_set_options, key="cr_set_filter")

        cr_filtered = cr_items
        if cr_selected_set != "All":
            cr_filtered = [i for i in cr_filtered if _extract_set(i["sku"]) == cr_selected_set]

        st.caption("Stock data updated automatically by the pipeline scraper.")

        # --- Table ---
        cr_rows = []
        for i in cr_filtered:
            p = i.get("price_yen")
            pa = i.get("price_yen_subgrade")
            diff = int(p - pa) if p and pa else None
            diff_pct = round((p - pa) / p * 100) if p and pa else None
            cr_rows.append({
                "SKU": i["sku"],
                "Set": _extract_set(i["sku"]),
                "CR \u00a5": p,
                "CR A- \u00a5": pa,
                "Diff \u00a5": diff,
                "Diff %": diff_pct,
                "Stock": i.get("cardrush_stock"),
                "A- Stock": i.get("cardrush_stock_subgrade"),
                "CardRush": i.get("cardrush_url"),
                "CR A-": i.get("cardrush_url_subgrade"),
            })

        cr_df = pd.DataFrame(cr_rows)
        st.dataframe(
            cr_df,
            use_container_width=True,
            hide_index=True,
            column_config={
                "CR \u00a5": st.column_config.NumberColumn(format="%d"),
                "CR A- \u00a5": st.column_config.NumberColumn("CR A- \u00a5", format="%d"),
                "Diff \u00a5": st.column_config.NumberColumn("Diff \u00a5", format="%d"),
                "Diff %": st.column_config.NumberColumn("Diff %", format="%d%%"),
                "Stock": st.column_config.NumberColumn("Stock", format="%d"),
                "A- Stock": st.column_config.NumberColumn("A- Stock", format="%d"),
                "CardRush": st.column_config.LinkColumn("CardRush", display_text="Link"),
                "CR A-": st.column_config.LinkColumn("CR A-", display_text="Link"),
            },
        )
    else:
        st.info("No CardRush links found in inventory.")


# ==================== TAB 6: Stock Push ====================

with tab_push:

    import subprocess

    def _run_push(script_module: str, dry_run: bool) -> str:
        """Run a stock push script as a subprocess and return output."""
        cmd = [sys.executable, "-m", script_module]
        if dry_run:
            cmd.append("--dry-run")
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            cwd=str(_repo_root),
            timeout=300,
        )
        output = result.stdout
        if result.stderr:
            output += "\n--- STDERR ---\n" + result.stderr
        return output

    st.subheader("Push Stock to Marketplaces")
    st.caption("Pushes tier-capped listed quantities from stock_inventory to eBay / Shopify.")

    dry_run = st.checkbox("Dry run (preview only)", value=True)

    col_ebay, col_shopify, col_both = st.columns(3)

    with col_ebay:
        push_ebay = st.button("Push to eBay", use_container_width=True)
    with col_shopify:
        push_shopify = st.button("Push to Shopify", use_container_width=True)
    with col_both:
        push_both = st.button("Push to Both", type="primary", use_container_width=True)

    targets = []
    if push_ebay or push_both:
        targets.append(("eBay", "stock.count.push_ebay_stock"))
    if push_shopify or push_both:
        targets.append(("Shopify", "stock.count.push_shopify_stock"))

    for label, module in targets:
        st.divider()
        mode = "DRY RUN" if dry_run else "LIVE"
        with st.spinner(f"Pushing stock to {label} ({mode})..."):
            try:
                output = _run_push(module, dry_run)
                st.code(output, language="text")
            except subprocess.TimeoutExpired:
                st.error(f"{label} stock push timed out after 5 minutes.")
            except Exception as e:
                st.error(f"{label} stock push failed: {e}")

    # --- Price Push ---

    st.divider()
    st.subheader("Push Prices to Marketplaces")
    st.caption("Invokes the pricing Lambda functions to push selling prices from RDS to eBay / Shopify.")

    def _invoke_price_push(function_name: str) -> dict:
        """Invoke a price-push Lambda and return its response body."""
        resp = _lambda_client.invoke(
            FunctionName=function_name,
            InvocationType="RequestResponse",
            Payload=json.dumps({}),
        )
        payload = json.loads(resp["Payload"].read())
        if payload.get("statusCode", 200) >= 400:
            body = json.loads(payload.get("body", "{}"))
            raise RuntimeError(f"Lambda {payload['statusCode']}: {body.get('error', body)}")
        return json.loads(payload.get("body", "{}"))

    col_price_ebay, col_price_shopify, col_price_both = st.columns(3)

    with col_price_ebay:
        price_ebay = st.button("Push Prices to eBay", use_container_width=True)
    with col_price_shopify:
        price_shopify = st.button("Push Prices to Shopify", use_container_width=True)
    with col_price_both:
        price_both = st.button("Push Prices to Both", type="primary", use_container_width=True)

    price_targets = []
    if price_ebay or price_both:
        price_targets.append(("eBay", "ebay-price-push"))
    if price_shopify or price_both:
        price_targets.append(("Shopify", "shopify-price-push"))

    for label, func_name in price_targets:
        st.divider()
        with st.spinner(f"Pushing prices to {label}..."):
            try:
                result = _invoke_price_push(func_name)
                st.json(result)
            except Exception as e:
                st.error(f"{label} price push failed: {e}")
