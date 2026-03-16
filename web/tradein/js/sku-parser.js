/**
 * SKU parser — mirrors pricing/api/lambda_function.py:_parse_sku()
 *
 * SKU formats:
 *   OP-{SET}-{NUM}-JP     → One Piece
 *   PKMN-{SET}-{NUM}-JP   → Pokemon
 */
const SKU_PATTERN = /^(OP|PKMN)-([A-Za-z0-9]+)-(\d{2,4})-([A-Z]{2})$/;

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
