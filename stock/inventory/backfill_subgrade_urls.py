"""Backfill cardrush_url_subgrade for SKUs in cardrush_link.

Strategy:
  1. Group SKUs by set prefix (EB01, OP05, etc.)
  2. For each set, search CardRush for "状態A- {set}" to get all A- listings
  3. Fetch each regular product page title (concurrent, with timeouts)
  4. Match: 〔状態A-〕 + regular_title == A- listing title
  5. Write matched subgrade URLs back to RDS via Lambda

Usage:
    python3 stock/inventory/backfill_subgrade_urls.py [--dry-run] [--limit N] [--set EB01] [--all]
"""

import argparse
import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import quote

import boto3
import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter, Retry

FUNCTION_NAME = "stock-inventory-api"
REGION = "us-east-1"
BASE = "https://www.cardrush-op.jp"
REQUEST_TIMEOUT = 10  # seconds per request

A_MINUS_PREFIX = "\u3014\u72b6\u614bA-\u3015"  # 〔状態A-〕


def _session():
    s = requests.Session()
    retries = Retry(total=2, backoff_factor=0.5, status_forcelist=[429, 500, 502, 503, 504])
    s.mount("https://", HTTPAdapter(max_retries=retries))
    s.headers["User-Agent"] = "Mozilla/5.0 (compatible; CambridgeTCG/1.0)"
    return s


def _lambda_client():
    return boto3.client("lambda", region_name=REGION)


def fetch_restock_items():
    """Get all restock items from Lambda."""
    client = _lambda_client()
    resp = client.invoke(
        FunctionName=FUNCTION_NAME,
        InvocationType="RequestResponse",
        Payload=json.dumps({
            "requestContext": {"http": {"method": "GET", "path": "/inventory/restock"}},
            "headers": {},
        }),
    )
    data = json.loads(resp["Payload"].read())
    return json.loads(data["body"])["items"]


def fetch_all_inventory():
    """Get all inventory items from Lambda."""
    client = _lambda_client()
    resp = client.invoke(
        FunctionName=FUNCTION_NAME,
        InvocationType="RequestResponse",
        Payload=json.dumps({
            "requestContext": {"http": {"method": "GET", "path": "/inventory"}},
            "headers": {},
        }),
    )
    data = json.loads(resp["Payload"].read())
    return json.loads(data["body"])["items"]


def extract_set_code(sku: str) -> str:
    """OP-EB01-001-JP → EB01, OP-OP05-001-JP → OP05."""
    m = re.match(r"^[A-Z]+-([A-Z0-9]+)-", sku)
    return m.group(1) if m else ""


def _parse_search_page(html: str) -> list[dict]:
    """Parse A- listings from a search results page."""
    soup = BeautifulSoup(html, "html.parser")
    results = []
    seen = set()
    for a_tag in soup.select("a[href*='/product/']"):
        href = a_tag.get("href", "")
        if not re.search(r"/product/\d+$", href):
            continue
        full_url = href if href.startswith("http") else BASE + href
        if full_url in seen:
            continue
        title = a_tag.get_text(strip=True)
        if not title or A_MINUS_PREFIX not in title:
            continue
        # Extract title up to card number brace: ...{XX-NNN} or {XX-NNN[YY]}
        m_title = re.search(r"^(.+?\{[^}]+\})", title)
        clean = m_title.group(1) if m_title else title
        seen.add(full_url)
        results.append({
            "base_title": clean.replace(A_MINUS_PREFIX, "", 1),
            "url": full_url,
        })
    return results


def search_all_a_minus(session, set_code: str) -> dict[str, str]:
    """Search all A- listings for a set. Returns {base_title: url}.

    Uses count=100 to get max results per page. Pagination via p= doesn't
    work on CardRush, so we get up to 100 results per search.
    """
    keyword = f"\u72b6\u614bA- {set_code}"
    url = f"{BASE}/product-list?keyword={quote(keyword)}&count=100"
    try:
        resp = session.get(url, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
    except Exception as e:
        print(f"    Search failed: {e}", flush=True)
        return {}
    results = _parse_search_page(resp.text)
    a_minus_map = {}
    for r in results:
        a_minus_map.setdefault(r["base_title"], r["url"])
    return a_minus_map


def fetch_product_title(session, url: str) -> str | None:
    """Fetch a CardRush product page and extract the title."""
    try:
        resp = session.get(url, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        h1 = soup.find("h1")
        raw = h1.get_text(strip=True) if h1 else None
        if not raw:
            og = soup.find("meta", property="og:title")
            raw = og.get("content", "").strip() if og else None
        if raw:
            # Truncate at card number brace to match search result format
            m = re.search(r"^(.+?\{[^}]+\})", raw)
            return m.group(1) if m else raw
        return None
    except Exception as e:
        print(f"    ERROR {url}: {e}", flush=True)
    return None


def fetch_titles_concurrent(session, items: list[dict], workers: int = 5) -> dict[str, str]:
    """Fetch product titles concurrently. Returns {sku: title}."""
    titles = {}

    def _fetch(item):
        time.sleep(0.2)
        title = fetch_product_title(session, item["cardrush_url"])
        return item["sku"], title

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_fetch, item): item["sku"] for item in items}
        for future in as_completed(futures):
            sku, title = future.result()
            if title:
                titles[sku] = title
    return titles


def update_subgrade_urls(matches: list[dict]):
    """Write matched subgrade URLs to RDS via migration runner Lambda."""
    if not matches:
        return
    cases = []
    skus = []
    for m in matches:
        sku_escaped = m["sku"].replace("'", "''")
        url_escaped = m["subgrade_url"].replace("'", "''")
        cases.append(f"WHEN '{sku_escaped}' THEN '{url_escaped}'")
        skus.append(f"'{sku_escaped}'")

    sql = (
        "UPDATE cardrush_link SET cardrush_url_subgrade = CASE sku "
        + " ".join(cases)
        + f" END WHERE sku IN ({', '.join(skus)})"
    )

    client = _lambda_client()
    resp = client.invoke(
        FunctionName="db-migration-runner",
        InvocationType="RequestResponse",
        Payload=json.dumps({"sql": sql}),
    )
    result = json.loads(resp["Payload"].read())
    print(f"  DB update: {json.loads(result.get('body', '{}'))}", flush=True)


def main():
    parser = argparse.ArgumentParser(description="Backfill CardRush subgrade URLs")
    parser.add_argument("--dry-run", action="store_true", help="Don't write to DB")
    parser.add_argument("--limit", type=int, default=0, help="Limit items to process")
    parser.add_argument("--set", type=str, default="", help="Only process this set code")
    parser.add_argument("--all", action="store_true", help="Fetch from full inventory (not just restock)")
    args = parser.parse_args()

    if args.all:
        print("Fetching all inventory items...", flush=True)
        items = fetch_all_inventory()
    else:
        print("Fetching restock items...", flush=True)
        items = fetch_restock_items()
    items = [i for i in items if i.get("cardrush_url") and not i.get("cardrush_url_subgrade")]
    print(f"  {len(items)} items need subgrade URLs", flush=True)

    if args.set:
        items = [i for i in items if extract_set_code(i["sku"]) == args.set.upper()]
        print(f"  Filtered to set {args.set.upper()}: {len(items)} items", flush=True)

    if args.limit:
        items = items[:args.limit]
        print(f"  Limited to {args.limit} items", flush=True)

    if not items:
        print("Nothing to do.")
        return

    # Group by set
    by_set: dict[str, list[dict]] = {}
    for item in items:
        sc = extract_set_code(item["sku"])
        by_set.setdefault(sc, []).append(item)

    session = _session()
    all_matches = []

    for set_code, set_items in sorted(by_set.items()):
        print(f"\n--- Set {set_code} ({len(set_items)} SKUs) ---", flush=True)

        # Step 1: Search for all A- listings
        print(f"  Searching A- listings...", flush=True)
        a_minus_map = search_all_a_minus(session, set_code)
        print(f"  Total A- listings: {len(a_minus_map)}", flush=True)

        if not a_minus_map:
            print("  Skipping — no A- results", flush=True)
            continue

        # Step 2: Fetch regular product titles concurrently
        print(f"  Fetching {len(set_items)} product titles...", flush=True)
        titles = fetch_titles_concurrent(session, set_items, workers=5)
        print(f"  Got {len(titles)} titles", flush=True)

        # Step 3: Match
        for item in set_items:
            sku = item["sku"]
            title = titles.get(sku)
            if not title:
                continue
            subgrade_url = a_minus_map.get(title)
            if subgrade_url:
                print(f"  {sku}: MATCHED", flush=True)
                all_matches.append({"sku": sku, "subgrade_url": subgrade_url})
            else:
                print(f"  {sku}: no A- match", flush=True)

    # Summary
    print(f"\n=== {len(all_matches)} matched / {len(items)} total ===", flush=True)

    if all_matches and not args.dry_run:
        for i in range(0, len(all_matches), 50):
            batch = all_matches[i:i + 50]
            print(f"  Writing batch {i // 50 + 1} ({len(batch)} items)...", flush=True)
            update_subgrade_urls(batch)
        print("Done!", flush=True)
    elif args.dry_run:
        print("Dry run — no DB writes.", flush=True)
        for m in all_matches:
            print(f"  {m['sku']} → {m['subgrade_url']}", flush=True)


if __name__ == "__main__":
    main()
