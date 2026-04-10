/**
 * SKU parser — mirrors pricing/api/lambda_function.py:_parse_sku()
 *
 * SKU formats:
 *   OP-{SET}-{NUM}-JP[-VARIANT]       → One Piece (booster)
 *   ST-{SET}-{NUM}-JP[-VARIANT]       → One Piece (starter)
 *   EB-{SET}-{NUM}-JP[-VARIANT]       → One Piece (extra booster)
 *   PRB-{SET}-{NUM}-JP[-VARIANT]      → One Piece (premium booster)
 *   P-{SET}-{NUM}-JP[-VARIANT]        → One Piece (promo)
 *   PKMN-{SET}-{NUM}-JP[-VARIANT]     → Pokemon
 *
 * Variant suffix (e.g. -V11L2) is optional — used for parallel/alt-art IDs.
 */
const SKU_PATTERN = /^(OP|ST|EB|PRB|P|PKMN)-([A-Za-z0-9]+)-(\d{2,4})-([A-Z]{2})(?:-[A-Za-z0-9]+)?$/;

function parseSku(sku) {
  if (!sku) return {};
  const m = sku.match(SKU_PATTERN);
  if (!m) return {};
  return {
    game: m[1],
    setCode: m[2],
    cardNumber: m[3],
    lang: m[4],
  };
}

/**
 * Format a SKU for display: "OP01-001"
 */
function formatSkuShort(sku) {
  const p = parseSku(sku);
  if (p.setCode && p.cardNumber) {
    return p.setCode + '-' + p.cardNumber;
  }
  return sku;
}
