#!/usr/bin/env python3
"""
Scrape high-res card images from CardRush product pages,
upload to jp-op-photos S3 bucket under cardrush/ prefix, make public,
then update buylist.json with the new image URLs.

Usage: python3 scrape-cardrush-images.py [--dry-run] [--limit N]
"""

import json
import os
import re
import sys
import time
import argparse
import urllib.request
import urllib.error
import concurrent.futures
from pathlib import Path

# ── Config ──────────────────────────────────────────────────────────
BUYLIST_PATH = Path(__file__).parent.parent / "data" / "buylist.json"
S3_BUCKET = "jp-op-photos"
S3_PREFIX = "cardrush"
S3_REGION = "us-east-1"
AWS_PROFILE = "alpha"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "ja,en-US;q=0.9",
}
CONCURRENCY = 8
DELAY = 0.3  # seconds between requests per worker

# High-res image pattern on product pages
# e.g. https://www.cardrush-op.jp/data/cardrush-op/product/OP01para_26.jpg
IMG_PATTERN = re.compile(
    r'https://www\.cardrush-op\.jp/data/cardrush-op/product/[^\s"\'<>]+\.jpg'
)


def fetch_page(url: str) -> str | None:
    req = urllib.request.Request(url, headers=HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=12) as r:
            return r.read().decode("utf-8", errors="ignore")
    except Exception as e:
        return None


def extract_hires_image(html: str) -> str | None:
    """Extract first product image URL from page HTML."""
    matches = IMG_PATTERN.findall(html)
    if not matches:
        return None
    # Deduplicate, return first
    return matches[0]


def s3_key(sku: str) -> str:
    """Generate S3 key for a SKU. e.g. cardrush/OP01/OP-OP01-001-JP.jpg"""
    # Extract set code from SKU: OP-OP01-001-JP-xxx → OP01
    parts = sku.split("-")
    set_code = parts[1] if len(parts) > 1 else "UNKNOWN"
    return f"{S3_PREFIX}/{set_code}/{sku}.jpg"


def s3_public_url(key: str) -> str:
    return f"https://{S3_BUCKET}.s3.{S3_REGION}.amazonaws.com/{key}"


def upload_to_s3(image_url: str, key: str, dry_run: bool = False) -> bool:
    """Download image from CardRush and upload to S3 with public-read ACL."""
    if dry_run:
        print(f"  [DRY RUN] Would upload {image_url} → s3://{S3_BUCKET}/{key}")
        return True

    # Download image bytes
    req = urllib.request.Request(image_url, headers=HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            data = r.read()
    except Exception as e:
        print(f"  ❌ Download failed: {image_url} — {e}")
        return False

    # Upload via AWS CLI (simplest, uses alpha profile)
    import tempfile, subprocess
    with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as f:
        f.write(data)
        tmp_path = f.name

    try:
        result = subprocess.run([
            "aws", "--profile", AWS_PROFILE,
            "s3", "cp", tmp_path,
            f"s3://{S3_BUCKET}/{key}",
            "--content-type", "image/jpeg",
            "--acl", "public-read",
            "--region", S3_REGION,
        ], capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            print(f"  ❌ S3 upload failed: {result.stderr.strip()}")
            return False
        return True
    except Exception as e:
        print(f"  ❌ Upload error: {e}")
        return False
    finally:
        os.unlink(tmp_path)


def process_item(item: dict, dry_run: bool) -> tuple[str, str | None]:
    """Scrape + upload one card. Returns (sku, new_image_url | None)."""
    sku = item["sku"]
    cr_url = item.get("cardrushUrl", "")
    if not cr_url:
        return sku, None

    key = s3_key(sku)
    public_url = s3_public_url(key)

    # Check if already uploaded (skip re-upload)
    if not dry_run:
        import boto3, botocore
        session = boto3.Session(profile_name=AWS_PROFILE)
        s3 = session.client("s3", region_name=S3_REGION)
        try:
            s3.head_object(Bucket=S3_BUCKET, Key=key)
            # Already exists
            return sku, public_url
        except botocore.exceptions.ClientError:
            pass  # doesn't exist, continue

    # Scrape product page
    time.sleep(DELAY)
    html = fetch_page(cr_url)
    if not html:
        print(f"  ⚠️  {sku}: failed to fetch {cr_url}")
        return sku, None

    img_url = extract_hires_image(html)
    if not img_url:
        print(f"  ⚠️  {sku}: no image found on page")
        return sku, None

    # Upload
    ok = upload_to_s3(img_url, key, dry_run=dry_run)
    if ok:
        return sku, public_url
    return sku, None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args()

    with open(BUYLIST_PATH) as f:
        data = json.load(f)

    items = data["items"]
    if args.limit:
        items = items[:args.limit]

    print(f"{'[DRY RUN] ' if args.dry_run else ''}Processing {len(items)} cards...")
    print(f"Concurrency: {CONCURRENCY} | Delay: {DELAY}s")
    print()

    results = {}  # sku → new_url
    done = 0

    with concurrent.futures.ThreadPoolExecutor(max_workers=CONCURRENCY) as executor:
        futures = {executor.submit(process_item, item, args.dry_run): item for item in items}
        for future in concurrent.futures.as_completed(futures):
            sku, url = future.result()
            results[sku] = url
            done += 1
            if url:
                print(f"  ✅ [{done}/{len(items)}] {sku}")
            if done % 50 == 0:
                print(f"  --- Progress: {done}/{len(items)} ---")

    # Update buylist.json
    ok_count = sum(1 for v in results.values() if v)
    fail_count = len(results) - ok_count
    print(f"\nDone: {ok_count} succeeded, {fail_count} failed")

    if not args.dry_run and ok_count > 0:
        for item in data["items"]:
            new_url = results.get(item["sku"])
            if new_url:
                item["imageFallback"] = item.get("imageUrl", "")
                item["imageUrl"] = new_url

        with open(BUYLIST_PATH, "w") as f:
            json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
        print(f"Updated buylist.json with {ok_count} CardRush high-res images")


if __name__ == "__main__":
    main()
