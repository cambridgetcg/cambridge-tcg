#!/usr/bin/env python3
"""
Build trade-in buylist from CardRush data (wholesale + raw).

Uses wholesale data for: SKU, card name, set info, rarity, image, stock
Uses raw data for: A- condition pricing (trade-in source price)

Pricing:
  - Source: CardRush A- condition price (状態A-)
  - Fallback: if no A- price, use wholesale base GBP × 0.85 (slight discount)
  - FX: Live GBP/JPY rate
  - Cash buy: 77% of reference GBP price
  - Credit buy: 88% of reference GBP price
  - MINT bonus: +15% on both cash and credit prices
"""

import json
import os
import glob
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

# ── Config ──────────────────────────────────────────────────
WHOLESALE_DIR = os.path.expanduser("~/Desktop/tcg-wholesale/data/cardrush/wholesale")
RAW_DIR = os.path.expanduser("~/Desktop/tcg-wholesale/data/cardrush/raw")
OUTPUT_DIR = os.path.expanduser("~/Desktop/cambridge-tcg/web/tradein/data")
SET_PREFIXES = [f"OP{str(i).zfill(2)}" for i in range(1, 16)]

CASH_RATE = 0.77
CREDIT_RATE = 0.88
MINT_BONUS = 0.15
FALLBACK_DISCOUNT = 0.85  # if no A- price, use 85% of wholesale base GBP

SET_NAMES = {
    "OP01": "Romance Dawn", "OP02": "Paramount War", "OP03": "Pillars of Strength",
    "OP04": "Kingdoms of Intrigue", "OP05": "Awakening of the New Era",
    "OP06": "Wings of the Captain", "OP07": "500 Years in the Future",
    "OP08": "Two Legends", "OP09": "The Four Emperors", "OP10": "Royal Blood",
    "OP11": "Uta", "OP12": "Gear 5", "OP13": "The Three Brothers",
    "OP14": "Strongest of the Strong", "OP15": "A Fist of Divine Speed",
}


def get_fx_rate() -> float:
    """Fetch live GBP/JPY rate."""
    for url in [
        "https://open.er-api.com/v6/latest/GBP",
        "https://api.exchangerate.host/latest?base=GBP&symbols=JPY",
    ]:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "CambridgeTCG/1.0"})
            resp = urllib.request.urlopen(req, timeout=10)
            data = json.loads(resp.read())
            rate = data.get("rates", {}).get("JPY")
            if rate and rate > 100:
                return float(rate)
        except Exception:
            continue
    return 208.53


def find_latest(directory: str, prefix: str) -> str | None:
    pattern = os.path.join(directory, f"{prefix}-*.json")
    files = sorted(glob.glob(pattern))
    return files[-1] if files else None


def round_price(price: float) -> float:
    return round(round(price * 20) / 20, 2)


def build_buylist():
    print("=" * 60)
    print("Cambridge TCG — Trade-In Buylist Builder v2")
    print("Sources: wholesale (SKU/name/image) + raw (A- pricing)")
    print("=" * 60)

    # 1. FX rate
    print("\n[1/5] Fetching GBP/JPY rate...")
    gbp_jpy = get_fx_rate()
    print(f"  Rate: 1 GBP = ¥{gbp_jpy:.2f}")

    # 2. Load raw A- prices (keyed by cardNumber + parallel flag)
    print("\n[2/5] Loading raw A- prices...")
    a_minus_prices = {}  # key: "OP01|OP01-001|False" → jpy price
    mint_prices = {}

    for prefix in SET_PREFIXES:
        filepath = find_latest(RAW_DIR, prefix)
        if not filepath:
            continue
        with open(filepath) as f:
            raw = json.load(f)
        for card in raw:
            cn = card.get("cardNumber")
            if not cn:
                continue
            is_p = card.get("isParallel", False)
            key = f"{prefix}|{cn}|{is_p}"
            cond = card.get("condition")
            jpy = card.get("priceJpy", 0)
            if cond == "状態A-" and jpy > 0:
                a_minus_prices[key] = jpy
            elif cond is None and jpy > 0:
                mint_prices[key] = jpy

    print(f"  A- prices loaded: {len(a_minus_prices)}")
    print(f"  Mint prices loaded: {len(mint_prices)}")

    # 3. Load wholesale data (primary source for card info)
    print("\n[3/5] Loading wholesale card data...")
    all_cards = []
    sets_found = {}

    for prefix in SET_PREFIXES:
        filepath = find_latest(WHOLESALE_DIR, prefix)
        if not filepath:
            continue

        filename = os.path.basename(filepath)
        date_str = filename.replace(f"{prefix}-", "").replace(".json", "")

        with open(filepath) as f:
            wholesale = json.load(f)

        count = 0
        for card in wholesale:
            cn = card.get("cardNumber", "")
            if not cn:
                continue

            is_p = card.get("isParallel", False)
            key = f"{prefix}|{cn}|{is_p}"

            # Get A- price (primary) or fallback to wholesale base × discount
            a_minus_jpy = a_minus_prices.get(key, 0)
            wholesale_base_gbp = card.get("pricing", {}).get("baseGbp", 0)

            if a_minus_jpy > 0:
                ref_gbp = a_minus_jpy / gbp_jpy
                price_source = "a-minus"
            elif wholesale_base_gbp > 0:
                ref_gbp = wholesale_base_gbp * FALLBACK_DISCOUNT
                price_source = "wholesale"
            else:
                continue  # no price data at all

            # Calculate trade-in prices
            cash_price = round_price(ref_gbp * CASH_RATE)
            credit_price = round_price(ref_gbp * CREDIT_RATE)

            # Skip cards below minimum cash threshold
            if cash_price < 2.20:
                continue

            # MINT bonus
            has_mint = key in mint_prices or price_source == "wholesale"
            mint_cash = round_price(ref_gbp * CASH_RATE * (1 + MINT_BONUS)) if has_mint else None
            mint_credit = round_price(ref_gbp * CREDIT_RATE * (1 + MINT_BONUS)) if has_mint else None

            # Clean name
            name = card.get("name", "")
            name = name.replace("〔状態A-〕", "").strip()

            # Use wholesale SKU
            sku = card.get("sku", f"OP-{cn}-JP{'-P' if is_p else ''}")

            # S3 image URL (primary) — jp-op-photos bucket
            # Pattern: Official/{SET}/{CARD_NUMBER}.png  (works for regular + SP cards)
            s3_image_url = f"https://jp-op-photos.s3.us-east-1.amazonaws.com/Official/{prefix}/{cn}.png"

            all_cards.append({
                "sku": sku,
                "cardNumber": cn,
                "setCode": prefix,
                "setName": SET_NAMES.get(prefix, prefix),
                "name": name,
                "rarity": card.get("rarity", ""),
                "isParallel": is_p,
                "sourceJpy": a_minus_jpy if a_minus_jpy > 0 else int(wholesale_base_gbp * gbp_jpy),
                "sourceGbp": round(ref_gbp, 2),
                "priceSource": price_source,
                "cashPrice": cash_price,
                "creditPrice": credit_price,
                "mintCashPrice": mint_cash,
                "mintCreditPrice": mint_credit,
                "imageUrl": s3_image_url,
                "imageFallback": card.get("imageUrl", ""),  # CardRush URL as fallback
                "cardrushUrl": card.get("cardrushUrl", ""),
                "wholesalePrice": round(card.get("pricing", {}).get("price", 0), 2),
                "stock": card.get("stock", 0),
            })
            count += 1

        sets_found[prefix] = {"count": count, "date": date_str}
        a_count = sum(1 for c in all_cards if c["setCode"] == prefix and c["priceSource"] == "a-minus")
        w_count = count - a_count
        print(f"  {prefix} ({SET_NAMES.get(prefix, '?')}): {count} cards ({a_count} A- priced, {w_count} wholesale fallback)")

    # 4. Sort
    all_cards.sort(key=lambda c: (c["setCode"], c["cardNumber"], c.get("isParallel", False)))

    # 5. Stats & output
    print(f"\n[4/5] Building buylist...")
    print(f"  Total cards: {len(all_cards)}")
    a_priced = sum(1 for c in all_cards if c["priceSource"] == "a-minus")
    w_priced = len(all_cards) - a_priced
    print(f"  A- priced: {a_priced} | Wholesale fallback: {w_priced}")

    total_cash = sum(c["cashPrice"] for c in all_cards)
    total_credit = sum(c["creditPrice"] for c in all_cards)

    buylist = {
        "version": 3,
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "fxRate": round(gbp_jpy, 2),
        "pricing": {
            "source": "CardRush A- condition (primary) + wholesale base (fallback)",
            "cashRate": CASH_RATE,
            "creditRate": CREDIT_RATE,
            "mintBonus": MINT_BONUS,
            "note": "Cash = 77% of ref GBP, Credit = 88% of ref GBP, MINT = +15% bonus"
        },
        "stats": {
            "totalCards": len(all_cards),
            "setsIncluded": len(sets_found),
            "aPriced": a_priced,
            "wholesaleFallback": w_priced,
            "avgCashPrice": round(total_cash / len(all_cards), 2) if all_cards else 0,
            "totalCashValue": round(total_cash, 2),
            "totalCreditValue": round(total_credit, 2),
        },
        "sets": {code: SET_NAMES.get(code, code) for code in sorted(sets_found.keys())},
        "items": all_cards,
    }

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    output_path = os.path.join(OUTPUT_DIR, "buylist.json")
    with open(output_path, "w") as f:
        json.dump(buylist, f, ensure_ascii=False, indent=2)

    print(f"\n[5/5] Written to {output_path}")
    print(f"  File size: {os.path.getsize(output_path) / 1024:.1f} KB")
    print(f"\n  Sample prices:")
    for c in all_cards[:5]:
        mint = f" | MINT: £{c['mintCashPrice']:.2f}/£{c['mintCreditPrice']:.2f}" if c.get("mintCashPrice") else ""
        src = "A-" if c["priceSource"] == "a-minus" else "WS"
        print(f"    {c['sku']:30s} {c['name'][:25]:25s} [{src}] Cash: £{c['cashPrice']:.2f} | Credit: £{c['creditPrice']:.2f}{mint}")

    print(f"\n{'=' * 60}")
    print(f"Done! {len(all_cards)} cards from {len(sets_found)} sets.")
    print(f"{'=' * 60}")
    return buylist


if __name__ == "__main__":
    build_buylist()
