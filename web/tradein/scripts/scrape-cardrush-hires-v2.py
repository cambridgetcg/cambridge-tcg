#!/usr/bin/env python3
"""
CardRush high-res image scraper v2.

For each SKU in the buylist:
  - The cardrushUrl already points to the exact product listing
  - Fetch that page, extract the high-res image URL
  - Download and upload to S3 as hires/{SET}/{FULL_SKU}.jpg

For the broader catalog (ALL cards on CardRush, not just buylist):
  - Search CardRush by card number
  - For each NM listing found, decode the product ID from the URL
  - Map to a canonical SKU: OP-{CARD}-JP (base) or OP-{CARD}-JP-P (parallel)
  - Upload keyed by that SKU

S3 structure: jp-op-photos/hires/{SET}/{SKU}.jpg
Manifest:     scripts/hires-manifest.json  {SKU -> S3 URL}

Usage:
  python3 scrape-cardrush-hires-v2.py [--mode buylist|all|set] [--set OP01] [--limit N] [--workers N] [--dry-run]
"""

import json, os, re, sys, time, argparse, subprocess, tempfile, collections
import urllib.request, urllib.parse
import concurrent.futures
from pathlib import Path

# ── Config ──────────────────────────────────────────────────────────
S3_BUCKET   = "jp-op-photos"
S3_PREFIX   = "hires"
S3_REGION   = "us-east-1"
AWS_PROFILE = "alpha"
MANIFEST    = Path(__file__).parent / "hires-manifest.json"
BASE_URL    = "https://www.cardrush-op.jp"
HEADERS     = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/120",
    "Accept-Language": "ja,en-US;q=0.9",
}
DELAY       = 0.4
IMG_RE      = re.compile(r'https://www\.cardrush-op\.jp/data/cardrush-op/product/[^\s"\'<>]+\.jpg')
CARD_NUM_RE = re.compile(r'\{((?:OP|EB|ST)\d{2}-\d{3}|P-\d{3})\}')

# Condition filters
SKIP_CONDS  = ['状態B', '状態C', 'PSA', 'BGS', 'BVG', '傷あり', 'キズあり', '状態D']
SEARCH_URL  = f"{BASE_URL}/product-list/?keyword={{card_num}}&show=96"

# ── Helpers ─────────────────────────────────────────────────────────

def fetch(url: str) -> str | None:
    req = urllib.request.Request(url, headers=HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=12) as r:
            return r.read().decode("utf-8", errors="ignore")
    except Exception:
        return None


def extract_hires(html: str) -> str | None:
    m = IMG_RE.search(html)
    return m.group(0) if m else None


def is_bad_condition(title: str) -> bool:
    return any(c in title for c in SKIP_CONDS)


def classify_variant(title: str) -> str:
    if 'パラレル' in title or '/P】' in title or '【L/P】' in title or '【R/P】' in title:
        return 'P'
    if '【SP】' in title or 'Special' in title:
        return 'SP'
    return ''  # base


def s3_key(sku: str) -> str:
    # Extract set code from SKU
    m = re.match(r'(?:OP|ST|EB|P)-(.+?)-JP', sku)
    if m:
        parts = m.group(1).split('-')
        set_code = parts[0]
    else:
        set_code = 'UNKNOWN'
    return f"{S3_PREFIX}/{set_code}/{sku}.jpg"


def s3_url(key: str) -> str:
    return f"https://{S3_BUCKET}.s3.{S3_REGION}.amazonaws.com/{key}"


_s3_client = None
def s3_client():
    global _s3_client
    if _s3_client is None:
        import boto3
        _s3_client = boto3.Session(profile_name=AWS_PROFILE).client('s3', region_name=S3_REGION)
    return _s3_client


def already_uploaded(key: str) -> bool:
    import botocore
    try:
        s3_client().head_object(Bucket=S3_BUCKET, Key=key)
        return True
    except botocore.exceptions.ClientError:
        return False


def upload(img_url: str, key: str, dry_run: bool) -> bool:
    if dry_run:
        print(f"    [DRY] {img_url.split('/')[-1]} -> {key}")
        return True

    if already_uploaded(key):
        return True

    req = urllib.request.Request(img_url, headers=HEADERS)
    try:
        data = urllib.request.urlopen(req, timeout=15).read()
    except Exception as e:
        print(f"    ❌ download failed: {e}")
        return False

    with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as f:
        f.write(data)
        tmp = f.name

    try:
        r = subprocess.run([
            "aws", "--profile", AWS_PROFILE, "s3", "cp", tmp,
            f"s3://{S3_BUCKET}/{key}",
            "--content-type", "image/jpeg",
            "--acl", "public-read",
            "--region", S3_REGION,
        ], capture_output=True, text=True, timeout=30)
        if r.returncode != 0:
            print(f"    ❌ S3 error: {r.stderr.strip()[:100]}")
        return r.returncode == 0
    finally:
        os.unlink(tmp)


# ── Mode: Buylist (direct product page per SKU) ──────────────────────

def process_buylist_sku(item: dict, dry_run: bool) -> tuple[str, str | None]:
    """Fetch the exact product page for a buylist SKU and upload its image."""
    sku = item['sku']
    cr_url = item.get('cardrushUrl', '')
    if not cr_url:
        return sku, None

    time.sleep(DELAY)
    html = fetch(cr_url)
    if not html:
        return sku, None

    img_url = extract_hires(html)
    if not img_url:
        return sku, None

    key = s3_key(sku)
    ok = upload(img_url, key, dry_run)
    return sku, s3_url(key) if ok else None


# ── Mode: All cards (search by card number) ──────────────────────────

def process_card_number(card_num: str, dry_run: bool) -> dict[str, str]:
    """Search CardRush for a card number, get best NM image per variant."""
    time.sleep(DELAY)

    url = SEARCH_URL.format(card_num=urllib.parse.quote(card_num))
    html = fetch(url)
    if not html:
        return {}

    product_ids = list(dict.fromkeys(re.findall(r'/product/(\d+)', html)))

    # For each product, fetch page and get title + image
    listings = []
    for pid in product_ids:
        time.sleep(DELAY / 3)
        phtml = fetch(f"{BASE_URL}/product/{pid}")
        if not phtml:
            continue

        title_m = re.search(r'<title>([^<]+)</title>', phtml)
        title = title_m.group(1).strip() if title_m else ''

        # Confirm it's the right card
        card_m = CARD_NUM_RE.search(title)
        if not card_m or card_m.group(1) != card_num:
            continue

        if is_bad_condition(title):
            continue

        img_url = extract_hires(phtml)
        if not img_url:
            continue

        variant = classify_variant(title)
        listings.append({'pid': pid, 'title': title, 'img_url': img_url, 'variant': variant})

    if not listings:
        return {}

    # Best image per variant (prefer 未開封 > 状態A- > plain NM)
    def rank(item):
        t = item['title']
        if '未開封' in t: return 0
        if '状態A-' in t: return 1
        return 2

    by_variant: dict[str, list] = {}
    for l in listings:
        by_variant.setdefault(l['variant'], []).append(l)

    results = {}
    # Derive set code from card_num
    parts = card_num.split('-')
    set_code = parts[0]
    game_prefix = 'OP' if set_code.startswith(('OP', 'EB', 'ST')) else 'P'

    for variant, items in by_variant.items():
        items.sort(key=rank)
        best = items[0]
        # Build canonical SKU
        if card_num.startswith('P-'):
            base_sku = f"P-{card_num.split('-')[1]}-JP"
        else:
            base_sku = f"OP-{card_num}-JP"

        sku = f"{base_sku}-{variant}" if variant else base_sku
        key = s3_key(sku)
        ok = upload(best['img_url'], key, dry_run)
        if ok:
            results[sku] = s3_url(key)

    return results


# ── Entry point ──────────────────────────────────────────────────────

def load_buylist() -> list[dict]:
    buylist_path = Path(__file__).parent.parent / "data" / "buylist.json"
    with open(buylist_path) as f:
        return json.load(f)['items']


def get_all_card_numbers(set_filter: str | None = None) -> list[str]:
    """All unique card numbers from price feed + S3 inventory."""
    cards = set()

    for xlsx in ['/tmp/pricefeed.xlsx', '/tmp/daily_prices.xlsx']:
        try:
            import openpyxl
            wb = openpyxl.load_workbook(xlsx)
            for ws in wb.worksheets:
                for row in ws.iter_rows(min_row=2, values_only=True):
                    if row[0] and re.match(r'^(?:OP|EB|ST)\d{2}-\d{3}$', str(row[0])):
                        cards.add(str(row[0]))
        except Exception:
            pass

    # From S3
    result = subprocess.run(
        ['aws', '--profile', AWS_PROFILE, 's3', 'ls', 's3://jp-op-photos/',
         '--recursive', '--region', S3_REGION],
        capture_output=True, text=True
    )
    for line in result.stdout.split('\n'):
        fname = line.strip().split('/')[-1]
        m = re.match(r'^(?:OP|ST|EB|P)-(.+?)-JP(?:-[A-Z0-9]+)?\.jpeg$', fname)
        if m:
            raw = m.group(1)
            if re.match(r'^(?:OP|EB|ST)\d{2}-\d{3}$', raw):
                cards.add(raw)

    result = sorted(cards)
    if set_filter:
        result = [c for c in result if c.startswith(set_filter)]
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--mode', choices=['buylist', 'all', 'set'], default='buylist',
                        help='buylist: exact SKUs from buylist; all: every card on CardRush; set: one set')
    parser.add_argument('--set', help='Set code filter, e.g. OP01')
    parser.add_argument('--limit', type=int, default=0)
    parser.add_argument('--workers', type=int, default=6)
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()

    # Load manifest
    manifest = {}
    if MANIFEST.exists():
        manifest = json.loads(MANIFEST.read_text())
    print(f"Existing manifest: {len(manifest)} entries")

    if args.mode == 'buylist':
        items = load_buylist()
        if args.set:
            items = [i for i in items if args.set in i['sku']]
        if args.limit:
            items = items[:args.limit]

        print(f"{'[DRY] ' if args.dry_run else ''}Mode: buylist | {len(items)} SKUs | {args.workers} workers\n")

        done = 0
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as ex:
            futures = {ex.submit(process_buylist_sku, item, args.dry_run): item for item in items}
            for future in concurrent.futures.as_completed(futures):
                item = futures[future]
                sku, url = future.result()
                done += 1
                if url:
                    manifest[sku] = url
                    print(f"  ✅ [{done}/{len(items)}] {sku}")
                else:
                    print(f"  ⚠️  [{done}/{len(items)}] {sku} — no image")

                if done % 50 == 0 and not args.dry_run:
                    MANIFEST.write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
                    print(f"\n  --- Checkpoint {done}/{len(items)} | manifest: {len(manifest)} ---\n")

    else:  # all or set
        cards = get_all_card_numbers(args.set)
        if args.limit:
            cards = cards[:args.limit]

        print(f"{'[DRY] ' if args.dry_run else ''}Mode: {args.mode} | {len(cards)} card numbers | {args.workers} workers\n")

        done = 0
        new = 0
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as ex:
            futures = {ex.submit(process_card_number, cn, args.dry_run): cn for cn in cards}
            for future in concurrent.futures.as_completed(futures):
                cn = futures[future]
                done += 1
                try:
                    result = future.result()
                    for sku, url in result.items():
                        if sku not in manifest:
                            new += 1
                        manifest[sku] = url
                    if result:
                        print(f"  ✅ [{done}/{len(cards)}] {cn} → {len(result)} ({', '.join(result)})")
                    else:
                        print(f"  ⚠️  [{done}/{len(cards)}] {cn} → none")
                except Exception as e:
                    print(f"  ❌ [{done}/{len(cards)}] {cn} → {e}")

                if done % 50 == 0 and not args.dry_run:
                    MANIFEST.write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
                    print(f"\n  --- Checkpoint {done}/{len(cards)} | manifest: {len(manifest)} (+{new} new) ---\n")

    if not args.dry_run:
        MANIFEST.write_text(json.dumps(manifest, ensure_ascii=False, indent=2))

    print(f"\n{'='*60}")
    print(f"Done. Manifest: {len(manifest)} SKUs → {MANIFEST}")
    print(f"{'='*60}")


if __name__ == '__main__':
    main()
