"""Cambridge TCG — Price Archive Browser

Streamlit frontend for exploring price_history data via Lambda invoke (boto3).

Usage:
    streamlit run pricing/archive/app.py

Requires:
    - AWS credentials configured (same as used for deploy)
    - pip install streamlit pandas boto3 python-dotenv
"""

import json
import os
import re
from pathlib import Path

import boto3
import pandas as pd
import streamlit as st
from dotenv import load_dotenv

# Load .env from repo root
_repo_root = Path(__file__).resolve().parent.parent.parent
load_dotenv(_repo_root / ".env")

FUNCTION_NAME = os.environ.get("PRICE_HISTORY_LAMBDA_NAME", "price-history-api")
REGION = os.environ.get("AWS_REGION", "us-east-1")

_lambda_client = boto3.client("lambda", region_name=REGION)

st.set_page_config(
    page_title="Price Archive — Cambridge TCG",
    page_icon="\U0001f4c8",
    layout="wide",
)


# --- Lambda invoke helper ---

def _invoke(method: str, path: str, params: dict | None = None) -> dict:
    """Invoke the price-history-api Lambda with a Function URL-style event."""
    event = {
        "requestContext": {"http": {"method": method, "path": path}},
        "headers": {},
        "queryStringParameters": params if params else None,
    }
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


# --- Data fetchers (cached) ---

@st.cache_data(ttl=60)
def fetch_catalog() -> dict:
    return _invoke("GET", "/catalog")


@st.cache_data(ttl=60)
def fetch_skus() -> list[str]:
    return _invoke("GET", "/skus")["skus"]


@st.cache_data(ttl=60)
def fetch_prices(sku: str, days: int | None = None) -> dict:
    params = {"sku": sku}
    if days:
        params["days"] = str(days)
    return _invoke("GET", "/prices", params=params)


@st.cache_data(ttl=60)
def fetch_indices(days: int | None = None) -> dict:
    params = {}
    if days:
        params["days"] = str(days)
    return _invoke("GET", "/indices", params=params)


def _extract_set(sku: str) -> str:
    """Extract set code from SKU."""
    if sku.startswith("OP-P-"):
        return "PROMO"
    m = re.match(r"^[A-Z]+-([A-Z0-9]+)-", sku)
    return m.group(1) if m else "OTHER"


# --- Sidebar ---

st.sidebar.title("Price Archive")

search = st.sidebar.text_input("Search SKU", placeholder="e.g. OP01, luffy")

days_options = {"30 days": 30, "90 days": 90, "365 days": 365, "All time": None}
days_label = st.sidebar.selectbox("Time range", list(days_options.keys()), index=1)
selected_days = days_options[days_label]

if st.sidebar.button("Refresh", use_container_width=True):
    st.cache_data.clear()
    st.rerun()


# --- Tabs ---

tab_catalog, tab_history, tab_indices = st.tabs(["Catalog", "Price History", "Market Indices"])


# ==================== TAB 1: Catalog ====================

with tab_catalog:
    st.header("Catalog")

    try:
        catalog_data = fetch_catalog()
    except Exception as e:
        st.error(f"Failed to fetch catalog: {e}")
        st.stop()

    catalog_items = catalog_data["skus"]
    gbp_to_jpy = catalog_data.get("gbp_to_jpy")

    # Extract sets for filter
    all_sets = sorted(set(_extract_set(item["sku"]) for item in catalog_items))
    set_options = ["All"] + all_sets
    selected_set = st.sidebar.selectbox("Set filter", set_options)

    # Apply filters
    items = catalog_items
    if search:
        search_lower = search.lower()
        items = [i for i in items if search_lower in i["sku"].lower()
                 or (i.get("card_name") and search_lower in i["card_name"].lower())]
    if selected_set != "All":
        items = [i for i in items if _extract_set(i["sku"]) == selected_set]

    # Metrics
    total_skus = len(items)
    total_market_yen = sum(i["price_yen"] or 0 for i in items)
    in_stock = sum(1 for i in items if i.get("in_stock"))
    jp_count = sum(1 for i in items if i.get("lang") == "JP")
    en_count = sum(1 for i in items if i.get("lang") == "EN")

    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("SKUs", f"{total_skus:,}")
    c2.metric("Market Value", f"\u00a5{total_market_yen:,.0f}")
    c3.metric("In Stock", f"{in_stock:,}")
    c4.metric("JP", f"{jp_count:,}")
    c5.metric("EN", f"{en_count:,}")

    if not items:
        st.info("No items match the current filters.")
    else:
        rows = []
        for i in items:
            price_yen = i.get("price_yen")
            price_usd = i.get("price_usd")
            shopify = i.get("shopify_price")
            ebay = i.get("ebay_price")

            # Source price display
            if price_yen:
                source_price = f"\u00a5{price_yen:,}"
            elif price_usd:
                source_price = f"${price_usd:,.2f}"
            else:
                source_price = "\u2014"

            rows.append({
                "SKU": i["sku"],
                "Set": _extract_set(i["sku"]),
                "Name": i.get("card_name") or "\u2014",
                "Rarity": i.get("rarity") or "\u2014",
                "Lang": i.get("lang", ""),
                "Source \u00a5/$": source_price,
                "Shopify \u00a3": shopify,
                "eBay \u00a3": ebay,
                "In Stock": i.get("in_stock", False),
                "Variant": i.get("variant") or "",
            })

        df = pd.DataFrame(rows)
        st.dataframe(
            df,
            use_container_width=True,
            hide_index=True,
            column_config={
                "Shopify \u00a3": st.column_config.NumberColumn("Shopify \u00a3", format="\u00a3%.2f"),
                "eBay \u00a3": st.column_config.NumberColumn("eBay \u00a3", format="\u00a3%.2f"),
                "In Stock": st.column_config.CheckboxColumn("In Stock", disabled=True),
            },
        )


# ==================== TAB 2: Price History ====================

with tab_history:
    st.header("Price History")

    try:
        all_skus = fetch_skus()
    except Exception as e:
        st.error(f"Failed to fetch SKUs: {e}")
        all_skus = []

    # Filter SKU list by search
    filtered_skus = all_skus
    if search:
        search_lower = search.lower()
        filtered_skus = [s for s in all_skus if search_lower in s.lower()]

    selected_sku = st.selectbox(
        "Select SKU",
        options=filtered_skus,
        index=0 if filtered_skus else None,
        placeholder="Choose a SKU...",
    )

    if selected_sku:
        try:
            price_data = fetch_prices(selected_sku, selected_days)
        except Exception as e:
            st.error(f"Failed to fetch prices: {e}")
            price_data = None

        if price_data and price_data.get("prices"):
            prices = price_data["prices"]
            st.caption(f"{price_data['count']} data points | FX: \u00a5{price_data.get('gbp_to_jpy', 'N/A')}/\u00a3")

            pdf = pd.DataFrame(prices)
            pdf["date"] = pd.to_datetime(pdf["date"])
            pdf = pdf.sort_values("date")

            # Force numeric types (JSON None → NaN so charts skip gaps)
            for col in ["price_yen", "price_usd", "price_yen_subgrade",
                        "cardrush_stock", "cardrush_stock_subgrade", "selling_price_gbp"]:
                if col in pdf.columns:
                    pdf[col] = pd.to_numeric(pdf[col], errors="coerce")

            # --- Price chart ---
            price_cols = []
            if "price_yen" in pdf.columns and pdf["price_yen"].notna().any():
                price_cols.append("price_yen")
            if "price_yen_subgrade" in pdf.columns and pdf["price_yen_subgrade"].notna().any():
                price_cols.append("price_yen_subgrade")

            if price_cols:
                st.subheader("Price (Yen)")
                chart_df = pdf.set_index("date")[price_cols].dropna(how="all").copy()
                rename = {"price_yen": "CR \u00a5", "price_yen_subgrade": "CR A- \u00a5"}
                chart_df = chart_df.rename(columns={k: v for k, v in rename.items() if k in chart_df.columns})
                st.line_chart(chart_df)

            # USD chart for EN cards
            if "price_usd" in pdf.columns and pdf["price_usd"].notna().any():
                st.subheader("Price (USD)")
                usd_df = pdf.set_index("date")[["price_usd"]].dropna().rename(columns={"price_usd": "Price $"})
                st.line_chart(usd_df)

            # --- Stock chart ---
            stock_cols = []
            if "cardrush_stock" in pdf.columns and pdf["cardrush_stock"].notna().any():
                stock_cols.append("cardrush_stock")
            if "cardrush_stock_subgrade" in pdf.columns and pdf["cardrush_stock_subgrade"].notna().any():
                stock_cols.append("cardrush_stock_subgrade")

            if stock_cols:
                st.subheader("CardRush Stock")
                stock_df = pdf.set_index("date")[stock_cols].dropna(how="all").copy()
                rename_s = {"cardrush_stock": "Stock", "cardrush_stock_subgrade": "A- Stock"}
                stock_df = stock_df.rename(columns={k: v for k, v in rename_s.items() if k in stock_df.columns})
                st.line_chart(stock_df)

            # --- GBP selling price chart ---
            if "selling_price_gbp" in pdf.columns and pdf["selling_price_gbp"].notna().any():
                st.subheader("Selling Price (GBP)")
                gbp_df = pdf.set_index("date")[["selling_price_gbp"]].dropna().rename(columns={"selling_price_gbp": "Shopify \u00a3"})
                st.line_chart(gbp_df)

            # --- Data table ---
            with st.expander("Raw data"):
                display_pdf = pdf.copy()
                display_pdf["date"] = display_pdf["date"].dt.strftime("%Y-%m-%d")
                st.dataframe(display_pdf, use_container_width=True, hide_index=True)

        elif price_data:
            st.info(f"No price history found for {selected_sku}.")
    else:
        st.info("Select a SKU above to view its price history.")


# ==================== TAB 3: Market Indices ====================

with tab_indices:
    st.header("Market Indices")

    try:
        idx_data = fetch_indices(selected_days)
    except Exception as e:
        st.error(f"Failed to fetch indices: {e}")
        idx_data = None

    if idx_data and idx_data.get("series"):
        series = idx_data["series"]
        base_date = idx_data.get("base_date", "?")
        latest_date = idx_data.get("latest_date", "?")
        st.caption(f"Base date: {base_date} = 100 | Latest: {latest_date}")

        # --- Summary metrics ---
        cols = st.columns(min(len(series), 5))
        for i, (key, s) in enumerate(series.items()):
            with cols[i % len(cols)]:
                idx_val = s.get("current_index", 100)
                change = s.get("change_1d", 0)
                label = s.get("name", key)
                st.metric(label, f"{idx_val:.1f}", f"{change:+.2f}%")

        # --- Index time series chart ---
        st.subheader("Index Time Series")

        # Build a combined dataframe from all series
        chart_frames = []
        for key, s in series.items():
            if not s.get("history"):
                continue
            sdf = pd.DataFrame(s["history"])
            sdf["date"] = pd.to_datetime(sdf["date"])
            sdf = sdf.rename(columns={"index": s.get("name", key)})
            sdf = sdf.set_index("date")[[s.get("name", key)]]
            chart_frames.append(sdf)

        if chart_frames:
            combined = chart_frames[0]
            for frame in chart_frames[1:]:
                combined = combined.join(frame, how="outer")
            st.line_chart(combined)

        # --- Set breakdown table ---
        sets = idx_data.get("sets", [])
        if sets:
            st.subheader("Set Breakdown")
            set_df = pd.DataFrame(sets)
            # Reorder columns
            col_order = ["set_code", "game", "lang", "card_count", "avg_price", "total_value",
                         "min_price", "max_price", "pct_change"]
            col_order = [c for c in col_order if c in set_df.columns]
            set_df = set_df[col_order]
            set_df.columns = ["Set", "Game", "Lang", "Cards", "Avg \u00a3", "Total \u00a3",
                              "Min \u00a3", "Max \u00a3", "Change %"][:len(col_order)]
            st.dataframe(
                set_df,
                use_container_width=True,
                hide_index=True,
                column_config={
                    "Avg \u00a3": st.column_config.NumberColumn("Avg \u00a3", format="\u00a3%.2f"),
                    "Total \u00a3": st.column_config.NumberColumn("Total \u00a3", format="\u00a3%.2f"),
                    "Min \u00a3": st.column_config.NumberColumn("Min \u00a3", format="\u00a3%.2f"),
                    "Max \u00a3": st.column_config.NumberColumn("Max \u00a3", format="\u00a3%.2f"),
                    "Change %": st.column_config.NumberColumn("Change %", format="%.2f%%"),
                },
            )
    elif idx_data:
        st.info("No index data available for the selected time range.")
