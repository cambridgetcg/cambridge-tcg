/**
 * SKU parser — mirrors pricing/api/lambda_function.py:_parse_sku()
 *
 * SKU formats:
 *   OP-{SET}-{NUM}-JP       → One Piece (base)
 *   OP-{SET}-{NUM}-EN-P1    → One Piece (parallel variant)
 *   PKMN-{SET}-{NUM}-JP     → Pokemon
 */
const SKU_PATTERN = /^(OP|PKMN)-([A-Za-z0-9]+)-(\d{2,4})-([A-Z]{2})(?:-(P\d+))?$/;

function parseSku(sku) {
  if (!sku) return {};
  const m = sku.match(SKU_PATTERN);
  if (!m) return {};
  var result = {
    game: m[1],
    setCode: m[2],
    cardNumber: m[3],
    lang: m[4],
  };
  if (m[5]) result.variant = m[5];
  return result;
}

/**
 * Format a SKU for display: "OP01-001" or "OP01-001 P1"
 */
function formatSkuShort(sku) {
  const p = parseSku(sku);
  if (p.setCode && p.cardNumber) {
    var display = p.setCode + '-' + p.cardNumber;
    if (p.variant) display += ' ' + p.variant;
    return display;
  }
  return sku;
}
