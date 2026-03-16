/**
 * API client for Price History API.
 * Uses sessionStorage for caching catalog data within a browser session.
 */
const API_BASE = 'https://0okzxooy36.execute-api.us-east-1.amazonaws.com';
const CACHE_KEY = 'ctcg_catalog';
const CACHE_TTL = 5 * 60 * 1000; // 5 minutes

const PriceAPI = {
  /**
   * Fetch full catalog (cached in sessionStorage for 5 min).
   * Returns { count, gbp_to_jpy, skus: [...] }
   */
  async getCatalog() {
    const cached = sessionStorage.getItem(CACHE_KEY);
    if (cached) {
      const parsed = JSON.parse(cached);
      if (Date.now() - parsed._ts < CACHE_TTL) {
        return parsed.data;
      }
    }

    const resp = await fetch(API_BASE + '/catalog');
    if (!resp.ok) throw new Error('Catalog fetch failed: ' + resp.status);
    const data = await resp.json();

    sessionStorage.setItem(CACHE_KEY, JSON.stringify({ _ts: Date.now(), data }));
    return data;
  },

  /**
   * Fetch price history for a single SKU.
   * @param {string} sku
   * @param {number} [days] - limit to last N days
   * Returns { sku, count, gbp_to_jpy, prices: [{ date, price_yen, selling_price_gbp }] }
   */
  async getPrices(sku, days) {
    let url = API_BASE + '/prices?sku=' + encodeURIComponent(sku);
    if (days) url += '&days=' + days;
    const resp = await fetch(url);
    if (!resp.ok) throw new Error('Price fetch failed: ' + resp.status);
    return resp.json();
  },

  async getIndices(days, game, lang) {
    let url = API_BASE + '/indices';
    const p = [];
    if (days) p.push('days=' + days);
    if (game) p.push('game=' + game);
    if (lang) p.push('lang=' + lang);
    if (p.length) url += '?' + p.join('&');
    const resp = await fetch(url);
    if (!resp.ok) throw new Error('Indices fetch failed: ' + resp.status);
    return resp.json();
  },
};
