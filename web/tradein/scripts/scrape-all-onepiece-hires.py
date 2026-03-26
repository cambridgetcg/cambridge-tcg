#!/usr/bin/env python3
"""
Scrape high-res images for ALL One Piece SKUs from the wholesale DB.

Source of truth: wholesale PostgreSQL DB (3,255 SKUs, all with cardrush_url)
Target: jp-op-photos/hires/{SET}/{SKU}.jpg (public-read)

Each SKU has an exact cardrush_url — no search needed, just fetch the product page
and extract the high-res image. Skips SKUs already uploaded.

Usage:
  python3 scrape-all-onepiece-hires.py [--workers N] [--limit N] [--dry-run]

─────────────────────────────────────────────────────────────────────────────
⚠️  HIRES IMAGE PROTECTION — DO NOT MODIFY WITHOUT READING THIS ⚠️

These are the authoritative hi-res card images (100–200KB each, scraped from
Cardrush product pages). They are the SOURCE OF TRUTH for wholesaletcgdirect.com.

RULES:
 1. already_uploaded() checks S3 before every write — existing keys are NEVER
    overwritten. This is intentional and must not be removed.
 2. The hires-manifest.json tracks all uploaded SKU→URL mappings. Keep it.
 3. The wholesale DB image_url column is restored from this manifest. The scraper
    in tcg-wholesale uses a CASE guard to never overwrite hires/ URLs.
 4. S3 key format: hires/{SET_CODE}/{SKU}.jpg — must stay in sync with
    tcg-wholesale/tools/lib/s3-images.ts s3Key() function.

To restore DB image_url from manifest (e.g. after accidental overwrite):
  python3 scrape-all-onepiece-hires.py --restore-db-only
─────────────────────────────────────────────────────────────────────────────
"""

import os, re, sys, time, json, argparse, subprocess, tempfile
import urllib.request
import concurrent.futures
from pathlib import Path

# ── Config ───────────────────────────────────────────────────────────
DB_URL     = "***REMOVED-ROTATE-ME***"
S3_BUCKET  = "jp-op-photos"
S3_PREFIX  = "hires"
S3_REGION  = "us-east-1"
AWS_PROFILE= "alpha"
MANIFEST   = Path(__file__).parent / "hires-manifest.json"
HEADERS    = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/120",
    "Accept-Language": "ja,en-US;q=0.9",
}
IMG_RE     = re.compile(r'https://www\.cardrush-op\.jp/data/cardrush-op/product/[^\s"\'<>]+\.jpg')
DELAY      = 0.35

# ── Helpers ──────────────────────────────────────────────────────────

def fetch(url: str) -> str | None:
    req = urllib.request.Request(url, headers=HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=12) as r:
            return r.read().decode("utf-8", errors="ignore")
    except Exception:
        return None


def s3_key(sku: str) -> str:
    # Extract set code: OP-OP01-001-JP-xxx → OP01, EB-EB01-... → EB01, DON-... → DON
    parts = sku.split("-")
    if len(parts) >= 3 and parts[0] in ("OP", "EB", "ST", "P"):
        set_code = parts[1]  # e.g. OP01, EB01, ST01
    elif parts[0] == "DON":
        set_code = "DON"
    else:
        set_code = parts[0]
    return f"{S3_PREFIX}/{set_code}/{sku}.jpg"


def s3_public_url(key: str) -> str:
    return f"https://{S3_BUCKET}.s3.{S3_REGION}.amazonaws.com/{key}"


_s3 = None
def s3_client():
    global _s3
    if _s3 is None:
        import boto3
        _s3 = boto3.Session(profile_name=AWS_PROFILE).client("s3", region_name=S3_REGION)
    return _s3


def already_uploaded(key: str) -> bool:
    import botocore
    try:
        s3_client().head_object(Bucket=S3_BUCKET, Key=key)
        return True
    except botocore.exceptions.ClientError:
        return False


def upload(img_url: str, key: str, dry_run: bool) -> bool:
    if dry_run:
        print(f"    [DRY] {img_url.split('/')[-1]} → {key}")
        return True
    if already_uploaded(key):
        return True
    req = urllib.request.Request(img_url, headers=HEADERS)
    try:
        data = urllib.request.urlopen(req, timeout=15).read()
    except Exception as e:
        print(f"    ❌ download: {e}")
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
        return r.returncode == 0
    finally:
        os.unlink(tmp)


def process(sku: str, cr_url: str, dry_run: bool) -> tuple[str, str | None]:
    """Fetch product page, extract image, upload. Returns (sku, s3_url|None)."""
    key = s3_key(sku)
    if not dry_run and already_uploaded(key):
        return sku, s3_public_url(key)

    time.sleep(DELAY)
    html = fetch(cr_url)
    if not html:
        return sku, None

    m = IMG_RE.search(html)
    if not m:
        return sku, None

    ok = upload(m.group(0), key, dry_run)
    return sku, s3_public_url(key) if ok else None


def load_wholesale_skus() -> list[tuple[str, str]]:
    """Load all One Piece SKUs + cardrush_url from wholesale DB."""
    result = subprocess.run(
        ["psql", DB_URL, "-t", "-A",
         "-c", "SELECT sku || '|' || cardrush_url FROM cards WHERE game_id = 1 ORDER BY sku"],
        capture_output=True, text=True, timeout=30
    )
    rows = []
    for line in result.stdout.strip().split("\n"):
        if "|" in line:
            sku, url = line.strip().split("|", 1)
            rows.append((sku, url))
    return rows


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--restore-db-only", action="store_true",
                        help="Restore image_url in DB from manifest without re-scraping S3")
    args = parser.parse_args()

    # ── Fast DB restore from manifest ────────────────────────────────────────
    if args.restore_db_only:
        if not MANIFEST.exists():
            print("ERROR: hires-manifest.json not found. Cannot restore.")
            return
        manifest = json.loads(MANIFEST.read_text())
        print(f"Restoring {len(manifest)} image_url entries from manifest…")
        import tempfile, os
        rows = ", ".join(f"('{k.replace(chr(39), chr(39)*2)}', '{v.replace(chr(39), chr(39)*2)}')"
                         for k, v in manifest.items())
        sql = f"UPDATE cards SET image_url = v.url FROM (VALUES {rows}) AS v(sku, url) WHERE cards.sku = v.sku;"
        with tempfile.NamedTemporaryFile(mode='w', suffix='.sql', delete=False) as f:
            f.write(sql); tmpfile = f.name
        r = subprocess.run(["psql", DB_URL, "-f", tmpfile], capture_output=True, text=True, timeout=60)
        os.unlink(tmpfile)
        print(r.stdout.strip() or "Done")
        if r.returncode != 0:
            print("STDERR:", r.stderr.strip()[:300])
        return

    print("Loading SKUs from wholesale DB...")
    all_skus = load_wholesale_skus()
    print(f"Total One Piece SKUs: {len(all_skus)}")

    # Load manifest
    manifest = {}
    if MANIFEST.exists():
        manifest = json.loads(MANIFEST.read_text())

    # Skip already done
    todo = [(sku, url) for sku, url in all_skus if sku not in manifest]
    print(f"Already in manifest: {len(all_skus) - len(todo)}")
    print(f"To scrape: {len(todo)}")

    if args.limit:
        todo = todo[:args.limit]

    if not todo:
        print("Nothing to do!")
        return

    print(f"\n{'[DRY RUN] ' if args.dry_run else ''}Starting with {args.workers} workers...\n")

    done = 0
    ok_count = 0

    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as ex:
        futures = {ex.submit(process, sku, url, args.dry_run): (sku, url) for sku, url in todo}
        for future in concurrent.futures.as_completed(futures):
            sku, url = futures[future]
            done += 1
            try:
                _, s3_url = future.result()
                if s3_url:
                    manifest[sku] = s3_url
                    ok_count += 1
                    if ok_count % 50 == 0:
                        print(f"  ✅ {done}/{len(todo)} done ({ok_count} uploaded)")
                        if not args.dry_run:
                            MANIFEST.write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
                else:
                    print(f"  ⚠️  {sku} — no image found")
            except Exception as e:
                print(f"  ❌ {sku} — {e}")

    if not args.dry_run:
        MANIFEST.write_text(json.dumps(manifest, ensure_ascii=False, indent=2))

    print(f"\n{'='*60}")
    print(f"Done: {ok_count}/{len(todo)} uploaded")
    print(f"Total manifest: {len(manifest)} SKUs")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
