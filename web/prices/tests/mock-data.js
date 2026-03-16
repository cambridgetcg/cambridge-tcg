/**
 * Mock API responses for Playwright tests.
 * Shapes match the real price-history-api Lambda responses.
 */

const MOCK_CATALOG = {
  count: 12,
  gbp_to_jpy: 208.46,
  skus: [
    {
      sku: 'OP-OP01-001-JP',
      game: 'OP',
      set_code: 'OP01',
      card_number: '001',
      lang: 'JP',
      price_yen: 17800,
      price_usd: null,
      shopify_price: 142.80,
      ebay_price: 155.80,
      cardmarket_price: 148.80,
      in_stock: true,
      card_name: null,
      rarity: null,
      card_color: null,
      card_type: null,
      card_image_id: null,
      variant: null,
    },
    {
      sku: 'OP-OP01-002-JP',
      game: 'OP',
      set_code: 'OP01',
      card_number: '002',
      lang: 'JP',
      price_yen: 980,
      price_usd: null,
      shopify_price: 8.80,
      ebay_price: 10.80,
      cardmarket_price: 9.80,
      in_stock: true,
      card_name: null,
      rarity: null,
      card_color: null,
      card_type: null,
      card_image_id: null,
      variant: null,
    },
    {
      sku: 'OP-OP02-001-JP',
      game: 'OP',
      set_code: 'OP02',
      card_number: '001',
      lang: 'JP',
      price_yen: 2500,
      price_usd: null,
      shopify_price: 20.80,
      ebay_price: 23.80,
      cardmarket_price: 21.80,
      in_stock: false,
      card_name: null,
      rarity: null,
      card_color: null,
      card_type: null,
      card_image_id: null,
      variant: null,
    },
    {
      sku: 'OP-ST01-001-JP',
      game: 'OP',
      set_code: 'ST01',
      card_number: '001',
      lang: 'JP',
      price_yen: 500,
      price_usd: null,
      shopify_price: 4.80,
      ebay_price: 6.80,
      cardmarket_price: 5.80,
      in_stock: true,
      card_name: null,
      rarity: null,
      card_color: null,
      card_type: null,
      card_image_id: null,
      variant: null,
    },
    {
      sku: 'PKMN-SV1a-001-JP',
      game: 'PKMN',
      set_code: 'SV1a',
      card_number: '001',
      lang: 'JP',
      price_yen: 3200,
      price_usd: null,
      shopify_price: 25.80,
      ebay_price: 28.80,
      cardmarket_price: 26.80,
      in_stock: true,
      card_name: null,
      rarity: null,
      card_color: null,
      card_type: null,
      card_image_id: null,
      variant: null,
    },
    {
      sku: 'PKMN-SV1a-002-JP',
      game: 'PKMN',
      set_code: 'SV1a',
      card_number: '002',
      lang: 'JP',
      price_yen: 1500,
      price_usd: null,
      shopify_price: 12.80,
      ebay_price: null,
      cardmarket_price: 13.80,
      in_stock: false,
      card_name: null,
      rarity: null,
      card_color: null,
      card_type: null,
      card_image_id: null,
      variant: null,
    },
    {
      sku: 'PKMN-SV6-001-JP',
      game: 'PKMN',
      set_code: 'SV6',
      card_number: '001',
      lang: 'JP',
      price_yen: 8900,
      price_usd: null,
      shopify_price: 72.80,
      ebay_price: 78.80,
      cardmarket_price: 74.80,
      in_stock: true,
      card_name: null,
      rarity: null,
      card_color: null,
      card_type: null,
      card_image_id: null,
      variant: null,
    },
    {
      sku: 'OP-EB01-001-JP',
      game: 'OP',
      set_code: 'EB01',
      card_number: '001',
      lang: 'JP',
      price_yen: 4500,
      price_usd: null,
      shopify_price: 36.80,
      ebay_price: 40.80,
      cardmarket_price: 38.80,
      in_stock: true,
      card_name: null,
      rarity: null,
      card_color: null,
      card_type: null,
      card_image_id: null,
      variant: null,
    },
    {
      sku: 'OP-OP01-001-EN',
      game: 'OP',
      set_code: 'OP01',
      card_number: '001',
      lang: 'EN',
      price_yen: null,
      price_usd: 3.50,
      shopify_price: 2.80,
      ebay_price: 3.80,
      cardmarket_price: null,
      in_stock: false,
      card_name: 'Roronoa Zoro',
      rarity: 'SR',
      card_color: 'Green',
      card_type: 'Character',
      card_image_id: 'OP01-001',
      variant: null,
    },
    {
      sku: 'OP-OP01-002-EN',
      game: 'OP',
      set_code: 'OP01',
      card_number: '002',
      lang: 'EN',
      price_yen: null,
      price_usd: 1.20,
      shopify_price: 1.80,
      ebay_price: null,
      cardmarket_price: null,
      in_stock: true,
      card_name: 'Nami',
      rarity: 'C',
      card_color: 'Blue',
      card_type: 'Character',
      card_image_id: 'OP01-002',
      variant: null,
    },
    {
      sku: 'OP-OP01-001-EN-P1',
      game: 'OP',
      set_code: 'OP01',
      card_number: '001',
      lang: 'EN',
      price_yen: null,
      price_usd: 45.00,
      shopify_price: 42.80,
      ebay_price: 48.80,
      cardmarket_price: null,
      in_stock: true,
      card_name: 'Roronoa Zoro',
      rarity: 'SR',
      card_color: 'Green',
      card_type: 'Character',
      card_image_id: 'OP01-001_p1',
      variant: 'P1',
    },
    {
      sku: 'OP-OP01-001-EN-P2',
      game: 'OP',
      set_code: 'OP01',
      card_number: '001',
      lang: 'EN',
      price_yen: null,
      price_usd: 120.00,
      shopify_price: 112.80,
      ebay_price: 128.80,
      cardmarket_price: null,
      in_stock: false,
      card_name: 'Roronoa Zoro',
      rarity: 'SR',
      card_color: 'Green',
      card_type: 'Character',
      card_image_id: 'OP01-001_p2',
      variant: 'P2',
    },
  ],
};

// 30 days of price history for OP-OP01-001-JP
function generatePriceHistory(sku, days) {
  const prices = [];
  const baseYen = 17800;
  const now = new Date();
  const count = days || 180;

  for (let i = count; i >= 0; i--) {
    const d = new Date(now);
    d.setDate(d.getDate() - i);
    const dateStr = d.toISOString().split('T')[0];
    // Slight price variation
    const variation = Math.sin(i * 0.1) * 500;
    const priceYen = Math.round(baseYen + variation);
    prices.push({
      date: dateStr,
      price_yen: priceYen,
      selling_price_gbp: Math.ceil(priceYen / 208.46 * 1.05 + 1.0) * 1.22 * 1.2 / (1 - 0.05 * 1.2) + 0.80,
    });
  }

  return {
    sku: sku,
    count: prices.length,
    gbp_to_jpy: 208.46,
    prices: prices,
  };
}

function generateMockIndices(days, gameFilter, langFilter) {
  var count = days || 14;
  var now = new Date();
  var allHistory = [];
  var opHistory = [];
  var pkmnHistory = [];

  // Language scaling: JP ~70%, EN ~30% of total
  var scale = langFilter === 'JP' ? 0.7 : langFilter === 'EN' ? 0.3 : 1.0;

  var opJpHistory = [];
  var opEnHistory = [];
  var pkmnJpHistory = [];
  var pkmnEnHistory = [];

  for (var i = count; i >= 0; i--) {
    var d = new Date(now);
    d.setDate(d.getDate() - i);
    var dateStr = d.toISOString().split('T')[0];
    var allIdx = 100 + (count - i) * 0.15 + Math.sin(i * 0.3) * 0.5;
    var opJpIdx = 100 + (count - i) * 0.2 + Math.sin(i * 0.25) * 0.8;
    var opEnIdx = 100 + (count - i) * 0.18 + Math.sin(i * 0.25) * 0.6;
    var pkmnJpIdx = 100 + (count - i) * 0.08 + Math.sin(i * 0.35) * 0.3;
    var pkmnEnIdx = 100 + (count - i) * 0.06 + Math.sin(i * 0.35) * 0.25;

    allHistory.push({ date: dateStr, index: Math.round(allIdx * 100) / 100, total: Math.round((28000 + i * 10) * scale) });
    opJpHistory.push({ date: dateStr, index: Math.round(opJpIdx * 100) / 100, total: Math.round((13300 + i * 6) * scale) });
    opEnHistory.push({ date: dateStr, index: Math.round(opEnIdx * 100) / 100, total: Math.round((5700 + i * 2) * scale) });
    pkmnJpHistory.push({ date: dateStr, index: Math.round(pkmnJpIdx * 100) / 100, total: Math.round((6300 + i * 4) * scale) });
    pkmnEnHistory.push({ date: dateStr, index: Math.round(pkmnEnIdx * 100) / 100, total: Math.round((2700 + i * 1) * scale) });
  }

  var result = {
    base_date: allHistory[0].date,
    latest_date: allHistory[allHistory.length - 1].date,
    series: {
      ALL: {
        name: 'CTCG All Cards',
        current_index: allHistory[allHistory.length - 1].index,
        change_1d: 0.42,
        total_value: Math.round(28456.40 * scale * 100) / 100,
        sku_count: Math.round(486 * scale),
        history: allHistory,
      },
      'OP-JP': {
        name: 'One Piece Japanese',
        current_index: opJpHistory[opJpHistory.length - 1].index,
        change_1d: 0.55,
        total_value: Math.round(13440.56 * scale * 100) / 100,
        sku_count: Math.round(230 * scale),
        history: opJpHistory,
      },
      'OP-EN': {
        name: 'One Piece English',
        current_index: opEnHistory[opEnHistory.length - 1].index,
        change_1d: 0.38,
        total_value: Math.round(5760.24 * scale * 100) / 100,
        sku_count: Math.round(99 * scale),
        history: opEnHistory,
      },
      'PKMN-JP': {
        name: 'Pokemon Japanese',
        current_index: pkmnJpHistory[pkmnJpHistory.length - 1].index,
        change_1d: -0.15,
        total_value: Math.round(6440.42 * scale * 100) / 100,
        sku_count: Math.round(126 * scale),
        history: pkmnJpHistory,
      },
      'PKMN-EN': {
        name: 'Pokemon English',
        current_index: pkmnEnHistory[pkmnEnHistory.length - 1].index,
        change_1d: -0.08,
        total_value: Math.round(2760.18 * scale * 100) / 100,
        sku_count: Math.round(54 * scale),
        history: pkmnEnHistory,
      },
    },
    sets: [
      { set_code: 'OP01', game: 'OP', lang: 'EN', card_count: 10, avg_price: 14.20, total_value: 142.00, min_price: 1.80, max_price: 112.80, pct_change: 0.25 },
      { set_code: 'OP01', game: 'OP', lang: 'JP', card_count: 22, avg_price: 15.98, total_value: 351.44, min_price: 1.80, max_price: 142.80, pct_change: 0.38 },
      { set_code: 'OP02', game: 'OP', lang: 'EN', card_count: 8, avg_price: 11.50, total_value: 92.00, min_price: 1.80, max_price: 65.80, pct_change: -0.10 },
      { set_code: 'OP02', game: 'OP', lang: 'JP', card_count: 20, avg_price: 13.32, total_value: 266.40, min_price: 1.80, max_price: 85.80, pct_change: -0.17 },
      { set_code: 'ST01', game: 'OP', lang: 'EN', card_count: 3, avg_price: 4.80, total_value: 14.40, min_price: 1.80, max_price: 9.80, pct_change: 0 },
      { set_code: 'ST01', game: 'OP', lang: 'JP', card_count: 7, avg_price: 5.37, total_value: 37.60, min_price: 1.80, max_price: 12.80, pct_change: 0 },
      { set_code: 'EB01', game: 'OP', lang: 'EN', card_count: 5, avg_price: 20.10, total_value: 100.50, min_price: 3.80, max_price: 52.80, pct_change: 0.95 },
      { set_code: 'EB01', game: 'OP', lang: 'JP', card_count: 10, avg_price: 23.10, total_value: 231.00, min_price: 3.80, max_price: 68.80, pct_change: 1.32 },
      { set_code: 'SV1a', game: 'PKMN', lang: 'EN', card_count: 6, avg_price: 16.80, total_value: 100.80, min_price: 1.80, max_price: 75.80, pct_change: 0.65 },
      { set_code: 'SV1a', game: 'PKMN', lang: 'JP', card_count: 14, avg_price: 18.80, total_value: 263.20, min_price: 1.80, max_price: 95.80, pct_change: 0.89 },
      { set_code: 'SV6', game: 'PKMN', lang: 'EN', card_count: 8, avg_price: 13.20, total_value: 105.60, min_price: 1.80, max_price: 58.80, pct_change: -0.30 },
      { set_code: 'SV6', game: 'PKMN', lang: 'JP', card_count: 17, avg_price: 15.11, total_value: 256.90, min_price: 1.80, max_price: 72.80, pct_change: -0.52 },
    ],
  };

  // Only include set_series when game filter is provided (matches API lazy-load behavior)
  if (gameFilter) {
    var setNames = { OP: ['OP01', 'OP02', 'ST01', 'EB01'], PKMN: ['SV1a', 'SV6'] };
    var setsForGame = setNames[gameFilter] || [];
    var langs = ['EN', 'JP'];
    var set_series = {};
    for (var s = 0; s < setsForGame.length; s++) {
      for (var l = 0; l < langs.length; l++) {
        var sc = setsForGame[s];
        var lang = langs[l];
        var key = sc + '-' + lang;
        var langScale = lang === 'EN' ? 0.3 : 0.7;
        var setHist = [];
        for (var i = count; i >= 0; i--) {
          var d2 = new Date(now);
          d2.setDate(d2.getDate() - i);
          var ds = d2.toISOString().split('T')[0];
          var si = 100 + (count - i) * (0.1 + s * 0.05) + Math.sin(i * 0.2 + s + l) * 0.5;
          setHist.push({ date: ds, index: Math.round(si * 100) / 100, total: Math.round((400 + s * 50 + i * 2) * langScale) });
        }
        set_series[key] = { set_code: sc, lang: lang, game: gameFilter, history: setHist };
      }
    }
    result.set_series = set_series;
  }

  return result;
}

module.exports = { MOCK_CATALOG, generatePriceHistory, generateMockIndices };
