// @ts-check
const { test, expect } = require('@playwright/test');
const { MOCK_CATALOG, generatePriceHistory, generateMockIndices } = require('./mock-data');

const BASE_URL = 'http://localhost:8091';
const API_BASE = 'https://0okzxooy36.execute-api.us-east-1.amazonaws.com';

/**
 * Intercept API calls and return mock data.
 */
async function mockAPI(page) {
  await page.route('https://api.frankfurter.app/**', async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ base: 'GBP', date: '2026-02-16', rates: { USD: 1.36, EUR: 1.15, JPY: 208.0 } }),
    });
  });

  await page.route(`${API_BASE}/catalog`, async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(MOCK_CATALOG),
    });
  });

  await page.route(`${API_BASE}/prices**`, async (route) => {
    const url = new URL(route.request().url());
    const sku = url.searchParams.get('sku') || 'OP-OP01-001-JP';
    const days = url.searchParams.get('days');
    const data = generatePriceHistory(sku, days ? parseInt(days) : null);
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(data),
    });
  });

  await page.route(`${API_BASE}/indices**`, async (route) => {
    const url = new URL(route.request().url());
    const days = url.searchParams.get('days');
    const game = url.searchParams.get('game');
    const lang = url.searchParams.get('lang');
    const data = generateMockIndices(days ? parseInt(days) : null, game, lang);
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(data),
    });
  });
}

// ════════════════════════════════════════════════════════════════
// Page Load & Structure
// ════════════════════════════════════════════════════════════════

test.describe('Page Load', () => {
  test('loads index.html with correct title', async ({ page }) => {
    await mockAPI(page);
    await page.goto(BASE_URL);
    await expect(page).toHaveTitle(/Cambridge TCG/);
  });

  test('shows site header with title and shop link', async ({ page }) => {
    await mockAPI(page);
    await page.goto(BASE_URL);
    await expect(page.locator('.site-header__title')).toContainText('Card Price Checker');
    await expect(page.locator('.site-header__link')).toContainText('Shop Cambridge TCG');
    await expect(page.locator('.site-header__link')).toHaveAttribute('href', 'https://cambridgetcg.com');
  });

  test('shows intro text about collection value', async ({ page }) => {
    await mockAPI(page);
    await page.goto(BASE_URL);
    await expect(page.locator('.intro')).toBeVisible();
    await expect(page.locator('.intro')).toContainText('market value');
  });

  test('loads catalog and shows table', async ({ page }) => {
    await mockAPI(page);
    await page.goto(BASE_URL);
    await expect(page.locator('.catalog-table')).toBeVisible();
    await expect(page.locator('.catalog-table tbody tr')).toHaveCount(12);
  });

  test('shows loading state before data arrives', async ({ page }) => {
    await page.route(`${API_BASE}/catalog`, async (route) => {
      await new Promise((r) => setTimeout(r, 500));
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(MOCK_CATALOG),
      });
    });
    await page.goto(BASE_URL);
    await expect(page.locator('.loading')).toBeVisible();
    await expect(page.locator('.catalog-table')).toBeVisible({ timeout: 5000 });
  });

  test('shows error message on API failure', async ({ page }) => {
    await page.route(`${API_BASE}/catalog`, async (route) => {
      await route.fulfill({ status: 500, body: '{"error":"db down"}' });
    });
    await page.goto(BASE_URL);
    await expect(page.locator('.error-msg')).toBeVisible({ timeout: 5000 });
    await expect(page.locator('.error-msg')).toContainText('Failed to load prices');
  });
});

// ════════════════════════════════════════════════════════════════
// Catalog Page
// ════════════════════════════════════════════════════════════════

test.describe('Catalog Page', () => {
  test.beforeEach(async ({ page }) => {
    await mockAPI(page);
    await page.goto(BASE_URL);
    await expect(page.locator('.catalog-table')).toBeVisible();
  });

  test('shows game tabs: All, One Piece, Pokemon', async ({ page }) => {
    const tabs = page.locator('.game-tabs .tab');
    await expect(tabs).toHaveCount(3);
    await expect(tabs.nth(0)).toContainText('All');
    await expect(tabs.nth(1)).toContainText('One Piece');
    await expect(tabs.nth(2)).toContainText('Pokemon');
  });

  test('"All" tab is active by default', async ({ page }) => {
    const allTab = page.locator('.tab[data-game=""]');
    await expect(allTab).toHaveClass(/active/);
  });

  test('shows set pills for all sets', async ({ page }) => {
    const pills = page.locator('.set-pills .pill');
    // "All Sets" + EB01, OP01, OP02, ST01 (OP) + SV1a, SV6 (PKMN) = 7
    await expect(pills).toHaveCount(7);
    await expect(pills.nth(0)).toContainText('All Sets');
  });

  test('displays card count in stats', async ({ page }) => {
    await expect(page.locator('.count')).toContainText('12 cards');
  });

  test('table header says Value not Stock, has Name column', async ({ page }) => {
    const headers = page.locator('.catalog-table thead th');
    const texts = await headers.allTextContents();
    const joined = texts.join(' ');
    expect(joined).toContain('Value');
    expect(joined).toContain('Name');
    expect(joined).not.toContain('Stock');
  });

  test('every row has a Buy link to cambridgetcg.com', async ({ page }) => {
    const buyLinks = page.locator('.catalog-table tbody .table-buy-link');
    await expect(buyLinks).toHaveCount(12);
    const href = await buyLinks.first().getAttribute('href');
    expect(href).toContain('cambridgetcg.com/search?q=');
    await expect(buyLinks.first()).toHaveAttribute('target', '_blank');
  });

  test('no stock badges in table', async ({ page }) => {
    await expect(page.locator('.badge.in-stock')).toHaveCount(0);
    await expect(page.locator('.badge.out-of-stock')).toHaveCount(0);
  });
});

// ════════════════════════════════════════════════════════════════
// Search & Filtering
// ════════════════════════════════════════════════════════════════

test.describe('Search & Filtering', () => {
  test.beforeEach(async ({ page }) => {
    await mockAPI(page);
    await page.goto(BASE_URL);
    await expect(page.locator('.catalog-table')).toBeVisible();
  });

  test('search filters rows by text', async ({ page }) => {
    await page.fill('#search-input', 'OP01');
    const visibleRows = page.locator('.catalog-table tbody tr:visible');
    await expect(visibleRows).toHaveCount(6);
  });

  test('search is case-insensitive', async ({ page }) => {
    await page.fill('#search-input', 'op01');
    const visibleRows = page.locator('.catalog-table tbody tr:visible');
    await expect(visibleRows).toHaveCount(6);
  });

  test('clearing search shows all rows', async ({ page }) => {
    await page.fill('#search-input', 'OP01');
    await expect(page.locator('.catalog-table tbody tr:visible')).toHaveCount(6);
    await page.fill('#search-input', '');
    await expect(page.locator('.catalog-table tbody tr:visible')).toHaveCount(12);
  });

  test('search with no matches shows empty table', async ({ page }) => {
    await page.fill('#search-input', 'NONEXISTENT');
    await expect(page.locator('.catalog-table tbody tr:visible')).toHaveCount(0);
  });

  test('game tab filters to One Piece only', async ({ page }) => {
    await page.click('.tab[data-game="OP"]');
    await expect(page.locator('.catalog-table tbody tr')).toHaveCount(9);
    await expect(page.locator('.tab[data-game="OP"]')).toHaveClass(/active/);
  });

  test('game tab filters to Pokemon only', async ({ page }) => {
    await page.click('.tab[data-game="PKMN"]');
    await expect(page.locator('.catalog-table tbody tr')).toHaveCount(3);
  });

  test('clicking All tab resets filter', async ({ page }) => {
    await page.click('.tab[data-game="PKMN"]');
    await expect(page.locator('.catalog-table tbody tr')).toHaveCount(3);
    await page.click('.tab[data-game=""]');
    await expect(page.locator('.catalog-table tbody tr')).toHaveCount(12);
  });
});

// ════════════════════════════════════════════════════════════════
// Set Navigation
// ════════════════════════════════════════════════════════════════

test.describe('Set Navigation', () => {
  test.beforeEach(async ({ page }) => {
    await mockAPI(page);
    await page.goto(BASE_URL);
    await expect(page.locator('.catalog-table')).toBeVisible();
  });

  test('clicking set pill navigates to set view', async ({ page }) => {
    await page.click('.pill >> text=OP01');
    await expect(page).toHaveURL(/.*#\/set\/OP01/);
    await expect(page.locator('.catalog-table tbody tr')).toHaveCount(6);
  });

  test('set view shows set name in title', async ({ page }) => {
    await page.goto(`${BASE_URL}#/set/OP01`);
    await expect(page.locator('.catalog-table')).toBeVisible();
    await expect(page.locator('.page-title')).toContainText('OP01');
    await expect(page.locator('.page-title')).toContainText('Romance Dawn');
  });

  test('set view shows price range', async ({ page }) => {
    await page.goto(`${BASE_URL}#/set/OP01`);
    await expect(page.locator('.catalog-table')).toBeVisible();
    await expect(page.locator('.range')).toBeVisible();
    await expect(page.locator('.range')).toContainText('\u00a3');
  });

  test('set view shows correct card count', async ({ page }) => {
    await page.goto(`${BASE_URL}#/set/OP01`);
    await expect(page.locator('.catalog-table')).toBeVisible();
    await expect(page.locator('.count')).toContainText('6 cards');
  });

  test('set pill is highlighted as active', async ({ page }) => {
    await page.goto(`${BASE_URL}#/set/OP01`);
    await expect(page.locator('.catalog-table')).toBeVisible();
    await expect(page.locator('.pill.active >> text=OP01')).toBeVisible();
  });

  test('"All Sets" pill links back to catalog', async ({ page }) => {
    await page.goto(`${BASE_URL}#/set/OP01`);
    await expect(page.locator('.catalog-table')).toBeVisible();
    await page.click('.pill >> text=All Sets');
    // Wait for re-render showing all 12 cards
    await expect(page.locator('.count')).toContainText('12 cards');
  });

  test('set page shows set index chart', async ({ page }) => {
    await page.goto(`${BASE_URL}#/set/OP01`);
    await expect(page.locator('#set-chart')).toBeVisible();
    const width = await page.locator('#set-chart').evaluate((el) => el.width);
    expect(width).toBeGreaterThan(0);
  });

  test('set page has range buttons for index chart', async ({ page }) => {
    await page.goto(`${BASE_URL}#/set/OP01`);
    await expect(page.locator('.range-btn')).toHaveCount(3);
    await expect(page.locator('.range-btn.active')).toContainText('All');
  });

  test('catalog page does not show set index chart', async ({ page }) => {
    await page.goto(BASE_URL);
    await expect(page.locator('.catalog-table')).toBeVisible();
    await expect(page.locator('#set-chart')).toHaveCount(0);
  });
});

// ════════════════════════════════════════════════════════════════
// Sorting
// ════════════════════════════════════════════════════════════════

test.describe('Sorting', () => {
  test.beforeEach(async ({ page }) => {
    await mockAPI(page);
    await page.goto(BASE_URL);
    await expect(page.locator('.catalog-table')).toBeVisible();
  });

  test('default sort is by SKU ascending', async ({ page }) => {
    const firstCell = page.locator('.catalog-table tbody tr').first().locator('td').first();
    await expect(firstCell).toContainText('EB01-001');
  });

  test('clicking Value header sorts by price', async ({ page }) => {
    await page.click('th[data-sort="price"]');
    const firstCell = page.locator('.catalog-table tbody tr').first().locator('td').first();
    // EN OP01-002 at £1.80 is cheapest
    await expect(firstCell).toContainText('OP01-002');
  });

  test('clicking Value header again reverses sort', async ({ page }) => {
    await page.click('th[data-sort="price"]');
    await page.click('th[data-sort="price"]');
    const firstCell = page.locator('.catalog-table tbody tr').first().locator('td').first();
    await expect(firstCell).toContainText('OP01-001');
  });

  test('sort indicator changes direction', async ({ page }) => {
    const priceHeader = page.locator('th[data-sort="price"]');
    await expect(priceHeader).toContainText('\u2195');
    await priceHeader.click();
    await expect(priceHeader).toContainText('\u2191');
    await priceHeader.click();
    await expect(priceHeader).toContainText('\u2193');
  });
});

// ════════════════════════════════════════════════════════════════
// SKU Detail Page
// ════════════════════════════════════════════════════════════════

test.describe('SKU Detail Page', () => {
  test.beforeEach(async ({ page }) => {
    await mockAPI(page);
  });

  test('navigating to SKU detail shows card info', async ({ page }) => {
    await page.goto(`${BASE_URL}#/sku/OP-OP01-001-JP`);
    await expect(page.locator('.sku-title')).toBeVisible();
    await expect(page.locator('.sku-title')).toContainText('OP01-001');
    await expect(page.locator('.sku-title')).toContainText('Romance Dawn');
  });

  test('shows SKU code in monospace', async ({ page }) => {
    await page.goto(`${BASE_URL}#/sku/OP-OP01-001-JP`);
    await expect(page.locator('.sku-code')).toContainText('OP-OP01-001-JP');
  });

  test('shows breadcrumb navigation', async ({ page }) => {
    await page.goto(`${BASE_URL}#/sku/OP-OP01-001-JP`);
    const breadcrumb = page.locator('.breadcrumb');
    await expect(breadcrumb).toBeVisible();
    await expect(breadcrumb).toContainText('Home');
    await expect(breadcrumb).toContainText('One Piece');
    await expect(breadcrumb).toContainText('OP01');
  });

  test('renders price chart', async ({ page }) => {
    await page.goto(`${BASE_URL}#/sku/OP-OP01-001-JP`);
    await expect(page.locator('#price-chart')).toBeVisible();
    const canvas = page.locator('#price-chart');
    const width = await canvas.evaluate((el) => el.width);
    expect(width).toBeGreaterThan(0);
  });

  test('shows range buttons with 30D active by default', async ({ page }) => {
    await page.goto(`${BASE_URL}#/sku/OP-OP01-001-JP`);
    await expect(page.locator('.range-btn')).toHaveCount(4);
    await expect(page.locator('.range-btn.active')).toContainText('30D');
  });

  test('clicking range button changes active state', async ({ page }) => {
    await page.goto(`${BASE_URL}#/sku/OP-OP01-001-JP`);
    await expect(page.locator('.range-btn.active')).toContainText('30D');
    await page.click('.range-btn >> text=90D');
    await expect(page.locator('.range-btn.active')).toContainText('90D');
  });

  test('range buttons trigger new API calls', async ({ page }) => {
    const apiCalls = [];
    await page.route(`${API_BASE}/prices**`, async (route) => {
      const url = new URL(route.request().url());
      apiCalls.push(url.searchParams.get('days'));
      const data = generatePriceHistory('OP-OP01-001-JP', 30);
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(data),
      });
    });
    await page.route(`${API_BASE}/catalog`, async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(MOCK_CATALOG),
      });
    });

    await page.goto(`${BASE_URL}#/sku/OP-OP01-001-JP`);
    await page.waitForTimeout(500);

    await page.click('.range-btn >> text=1Y');
    await page.waitForTimeout(500);

    await page.click('.range-btn >> text=All');
    await page.waitForTimeout(500);

    expect(apiCalls).toContain('30');
    expect(apiCalls).toContain('365');
    expect(apiCalls).toContain(null);
  });

  test('shows "Where to buy" section with platform price cards', async ({ page }) => {
    await page.goto(`${BASE_URL}#/sku/OP-OP01-001-JP`);
    await expect(page.locator('.price-section-title')).toContainText('Where to buy');
    const priceCards = page.locator('.price-card');
    await expect(priceCards).toHaveCount(3);
    await expect(priceCards.nth(0)).toContainText('Cambridge TCG');
    await expect(priceCards.nth(0)).toContainText('\u00a3142.80');
    await expect(priceCards.nth(1)).toContainText('eBay');
    await expect(priceCards.nth(1)).toContainText('\u00a3155.80');
    await expect(priceCards.nth(2)).toContainText('Cardmarket');
    await expect(priceCards.nth(2)).toContainText('\u00a3148.80');
  });

  test('Cambridge TCG price card has Buy link', async ({ page }) => {
    await page.goto(`${BASE_URL}#/sku/OP-OP01-001-JP`);
    const buyLink = page.locator('.price-card__buy');
    await expect(buyLink).toBeVisible();
    await expect(buyLink).toHaveAttribute('href', /cambridgetcg\.com\/search\?q=OP-OP01-001-JP/);
  });

  test('shows price stats with collection-oriented labels', async ({ page }) => {
    await page.goto(`${BASE_URL}#/sku/OP-OP01-001-JP`);
    await expect(page.locator('#price-stats')).toBeVisible({ timeout: 5000 });
    await expect(page.locator('#price-stats')).toContainText('Current Value');
    await expect(page.locator('#price-stats')).toContainText('Period High');
    await expect(page.locator('#price-stats')).toContainText('Period Low');
  });

  test('no stock badges on detail page', async ({ page }) => {
    await page.goto(`${BASE_URL}#/sku/OP-OP01-001-JP`);
    await expect(page.locator('.badge')).toHaveCount(0);
  });

  test('shows "Buy on Cambridge TCG" CTA button', async ({ page }) => {
    await page.goto(`${BASE_URL}#/sku/OP-OP01-001-JP`);
    const buyLink = page.locator('.buy-link');
    await expect(buyLink).toBeVisible();
    await expect(buyLink).toContainText('Buy on Cambridge TCG');
    await expect(buyLink).toHaveAttribute('href', /cambridgetcg\.com\/search\?q=OP-OP01-001-JP/);
    await expect(buyLink).toHaveAttribute('target', '_blank');
  });

  test('null eBay price shows dash', async ({ page }) => {
    await page.goto(`${BASE_URL}#/sku/PKMN-SV1a-002-JP`);
    const ebayCard = page.locator('.price-card').nth(1);
    await expect(ebayCard).toContainText('eBay');
    await expect(ebayCard).toContainText('\u2014');
  });
});

// ════════════════════════════════════════════════════════════════
// Navigation Flow
// ════════════════════════════════════════════════════════════════

test.describe('Navigation Flow', () => {
  test.beforeEach(async ({ page }) => {
    await mockAPI(page);
  });

  test('clicking card link in table navigates to SKU detail', async ({ page }) => {
    await page.goto(BASE_URL);
    await expect(page.locator('.catalog-table')).toBeVisible();
    await page.click('.catalog-table tbody tr:first-child td:first-child a');
    await expect(page.locator('.sku-title')).toBeVisible();
  });

  test('breadcrumb Home link returns to catalog', async ({ page }) => {
    await page.goto(`${BASE_URL}#/sku/OP-OP01-001-JP`);
    await expect(page.locator('.breadcrumb')).toBeVisible();
    await page.click('.breadcrumb a >> text=Home');
    await expect(page.locator('.catalog-table')).toBeVisible();
  });

  test('breadcrumb set link navigates to set view', async ({ page }) => {
    await page.goto(`${BASE_URL}#/sku/OP-OP01-001-JP`);
    await expect(page.locator('.breadcrumb')).toBeVisible();
    await page.click('.breadcrumb a >> text=OP01');
    await expect(page).toHaveURL(/.*#\/set\/OP01/);
    await expect(page.locator('.catalog-table tbody tr')).toHaveCount(6);
  });

  test('browser back/forward works with hash routing', async ({ page }) => {
    await page.goto(BASE_URL);
    await expect(page.locator('.catalog-table')).toBeVisible();

    await page.click('.pill >> text=OP01');
    await expect(page.locator('.count')).toContainText('6 cards');

    await page.click('.catalog-table tbody tr:first-child td:first-child a');
    await expect(page.locator('.sku-title')).toBeVisible();

    await page.goBack();
    await expect(page.locator('.count')).toContainText('6 cards');

    await page.goBack();
    // After going all the way back, should show full catalog
    await expect(page.locator('.catalog-table')).toBeVisible();
  });

  test('clicking set link in table row navigates to set view', async ({ page }) => {
    await page.goto(BASE_URL);
    await expect(page.locator('.catalog-table')).toBeVisible();
    await page.click('.catalog-table tbody tr:first-child .set-link');
    await expect(page).toHaveURL(/.*#\/set\//);
  });
});

// ════════════════════════════════════════════════════════════════
// Responsive / Mobile
// ════════════════════════════════════════════════════════════════

test.describe('Mobile Responsive', () => {
  test.use({ viewport: { width: 375, height: 812 } });

  test.beforeEach(async ({ page }) => {
    await mockAPI(page);
  });

  test('catalog loads on mobile viewport', async ({ page }) => {
    await page.goto(BASE_URL);
    await expect(page.locator('.catalog-table')).toBeVisible();
    await expect(page.locator('.catalog-table tbody tr')).toHaveCount(12);
  });

  test('name and set columns are hidden on mobile', async ({ page }) => {
    await page.goto(BASE_URL);
    await expect(page.locator('.catalog-table')).toBeVisible();
    const nameCell = page.locator('.catalog-table tbody tr:first-child td:nth-child(2)');
    const setCell = page.locator('.catalog-table tbody tr:first-child td:nth-child(3)');
    await expect(nameCell).toBeHidden();
    await expect(setCell).toBeHidden();
  });

  test('price grid stacks vertically on mobile', async ({ page }) => {
    await page.goto(`${BASE_URL}#/sku/OP-OP01-001-JP`);
    await expect(page.locator('.price-grid')).toBeVisible();
    const grid = page.locator('.price-grid');
    const style = await grid.evaluate((el) => getComputedStyle(el).gridTemplateColumns);
    expect(style).not.toContain('1fr 1fr 1fr');
  });

  test('chart container has reduced height on mobile', async ({ page }) => {
    await page.goto(`${BASE_URL}#/sku/OP-OP01-001-JP`);
    const container = page.locator('.chart-container');
    await expect(container).toBeVisible();
    const height = await container.evaluate((el) => parseInt(getComputedStyle(el).height));
    expect(height).toBeLessThanOrEqual(250);
  });

  test('game tabs scroll horizontally', async ({ page }) => {
    await page.goto(BASE_URL);
    const tabs = page.locator('.game-tabs');
    await expect(tabs).toBeVisible();
    const overflow = await tabs.evaluate((el) => getComputedStyle(el).overflowX);
    expect(overflow).toBe('auto');
  });
});

// ════════════════════════════════════════════════════════════════
// Session Storage Cache
// ════════════════════════════════════════════════════════════════

test.describe('API Caching', () => {
  test('catalog is cached in sessionStorage', async ({ page }) => {
    let catalogCallCount = 0;
    await page.route(`${API_BASE}/catalog`, async (route) => {
      catalogCallCount++;
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(MOCK_CATALOG),
      });
    });
    await page.route(`${API_BASE}/prices**`, async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(generatePriceHistory('OP-OP01-001-JP', 30)),
      });
    });

    await page.goto(BASE_URL);
    await expect(page.locator('.catalog-table')).toBeVisible();
    expect(catalogCallCount).toBe(1);

    const cached = await page.evaluate(() => sessionStorage.getItem('ctcg_catalog'));
    expect(cached).toBeTruthy();
    const parsed = JSON.parse(cached);
    expect(parsed.data.count).toBe(12);
  });
});

// ════════════════════════════════════════════════════════════════
// Market Index Page
// ════════════════════════════════════════════════════════════════

test.describe('Market Index Page', () => {
  test.beforeEach(async ({ page }) => {
    await mockAPI(page);
  });

  test('#/indices route renders the page', async ({ page }) => {
    await page.goto(`${BASE_URL}#/indices`);
    await expect(page.locator('.index-cards')).toBeVisible();
    await expect(page.locator('.breadcrumb')).toContainText('Market Index');
  });

  test('game index cards display with correct names (JP + EN per game)', async ({ page }) => {
    await page.goto(`${BASE_URL}#/indices`);
    await expect(page.locator('.index-card')).toHaveCount(4);
    await expect(page.locator('.index-card').nth(0)).toContainText('One Piece Japanese');
    await expect(page.locator('.index-card').nth(1)).toContainText('One Piece English');
    await expect(page.locator('.index-card').nth(2)).toContainText('Pokemon Japanese');
    await expect(page.locator('.index-card').nth(3)).toContainText('Pokemon English');
  });

  test('index cards show values and change percentages', async ({ page }) => {
    await page.goto(`${BASE_URL}#/indices`);
    const firstCard = page.locator('.index-card').nth(0);
    await expect(firstCard.locator('.index-card__value')).toBeVisible();
    await expect(firstCard.locator('.index-card__change')).toContainText('%');
    await expect(firstCard.locator('.index-card__meta')).toContainText('cards');
  });

  test('chart canvas rendered', async ({ page }) => {
    await page.goto(`${BASE_URL}#/indices`);
    const canvas = page.locator('#index-chart');
    await expect(canvas).toBeVisible();
    const width = await canvas.evaluate((el) => el.width);
    expect(width).toBeGreaterThan(0);
  });

  test('set breakdown table shows all sets (JP + EN per set)', async ({ page }) => {
    await page.goto(`${BASE_URL}#/indices`);
    await expect(page.locator('.set-breakdown')).toBeVisible();
    await expect(page.locator('.set-breakdown tbody tr')).toHaveCount(12);
  });

  test('clicking set code in breakdown navigates to set view', async ({ page }) => {
    await page.goto(`${BASE_URL}#/indices`);
    await expect(page.locator('.set-breakdown')).toBeVisible();
    await page.click('.set-breakdown tbody tr:first-child a');
    await expect(page).toHaveURL(/.*#\/set\//);
  });

  test('range buttons update chart', async ({ page }) => {
    const apiCalls = [];
    await page.route(`${API_BASE}/indices**`, async (route) => {
      const url = new URL(route.request().url());
      apiCalls.push(url.searchParams.get('days'));
      const data = generateMockIndices(14);
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(data),
      });
    });
    await page.route(`${API_BASE}/catalog`, async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(MOCK_CATALOG),
      });
    });

    await page.goto(`${BASE_URL}#/indices`);
    await expect(page.locator('.index-cards')).toBeVisible();

    await page.click('.range-btn >> text=7D');
    await page.waitForTimeout(500);

    await page.click('.range-btn >> text=30D');
    await page.waitForTimeout(500);

    expect(apiCalls).toContain('7');
    expect(apiCalls).toContain('30');
  });

  test('green/red change colors applied correctly', async ({ page }) => {
    await page.goto(`${BASE_URL}#/indices`);
    // OP-JP has positive change_1d, PKMN-JP has negative
    const opJpCard = page.locator('.index-card').nth(0);
    await expect(opJpCard.locator('.index-card__change')).toHaveClass(/index-up/);
    const pkmnJpCard = page.locator('.index-card').nth(2);
    await expect(pkmnJpCard.locator('.index-card__change')).toHaveClass(/index-down/);
  });

  test('header nav link navigates to indices', async ({ page }) => {
    await page.goto(BASE_URL);
    await expect(page.locator('.catalog-table')).toBeVisible();
    await page.click('.site-header__nav a >> text=Market Index');
    await expect(page).toHaveURL(/.*#\/indices/);
    await expect(page.locator('.index-cards')).toBeVisible();
  });

  test('set index chart is visible', async ({ page }) => {
    await page.goto(`${BASE_URL}#/indices`);
    const canvas = page.locator('#set-index-chart');
    await expect(canvas).toBeVisible();
    const width = await canvas.evaluate((el) => el.width);
    expect(width).toBeGreaterThan(0);
  });

  test('set index game tabs switch chart', async ({ page }) => {
    await page.goto(`${BASE_URL}#/indices`);
    await expect(page.locator('#set-index-chart')).toBeVisible();
    // Mock data has both OP and PKMN sets, so tabs should appear
    const tabs = page.locator('#set-index-tabs .tab');
    await expect(tabs).toHaveCount(2);
    await expect(tabs.nth(0)).toContainText('One Piece');
    await expect(tabs.nth(1)).toContainText('Pokemon');
    // Click Pokemon tab
    await tabs.nth(1).click();
    await expect(tabs.nth(1)).toHaveClass(/active/);
  });

  test('error state on API failure', async ({ page }) => {
    await page.route(`${API_BASE}/catalog`, async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(MOCK_CATALOG),
      });
    });
    await page.route(`${API_BASE}/indices**`, async (route) => {
      await route.fulfill({ status: 500, body: '{"error":"db down"}' });
    });
    await page.goto(`${BASE_URL}#/indices`);
    await expect(page.locator('.error-msg')).toBeVisible({ timeout: 5000 });
    await expect(page.locator('.error-msg')).toContainText('Failed to load market data');
  });

  test('language tabs appear on indices page', async ({ page }) => {
    await page.goto(`${BASE_URL}#/indices`);
    await expect(page.locator('.index-cards')).toBeVisible();
    const langTabs = page.locator('.lang-tabs .lang-tab');
    await expect(langTabs).toHaveCount(3);
    await expect(langTabs.nth(0)).toContainText('All');
    await expect(langTabs.nth(1)).toContainText('Japanese');
    await expect(langTabs.nth(2)).toContainText('English');
    await expect(langTabs.nth(0)).toHaveClass(/active/);
  });

  test('clicking Japanese passes lang=JP to API', async ({ page }) => {
    const apiCalls = [];
    await page.route(`${API_BASE}/indices**`, async (route) => {
      const url = new URL(route.request().url());
      apiCalls.push(url.searchParams.get('lang'));
      const data = generateMockIndices(null, url.searchParams.get('game'), url.searchParams.get('lang'));
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(data),
      });
    });
    await page.route(`${API_BASE}/catalog`, async (route) => {
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(MOCK_CATALOG) });
    });

    await page.goto(`${BASE_URL}#/indices`);
    await expect(page.locator('.index-cards')).toBeVisible();
    await page.click('.lang-tabs .lang-tab >> text=Japanese');
    await expect(page.locator('.index-cards')).toBeVisible();
    expect(apiCalls).toContain('JP');
  });

  test('clicking English passes lang=EN to API', async ({ page }) => {
    const apiCalls = [];
    await page.route(`${API_BASE}/indices**`, async (route) => {
      const url = new URL(route.request().url());
      apiCalls.push(url.searchParams.get('lang'));
      const data = generateMockIndices(null, url.searchParams.get('game'), url.searchParams.get('lang'));
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(data),
      });
    });
    await page.route(`${API_BASE}/catalog`, async (route) => {
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(MOCK_CATALOG) });
    });

    await page.goto(`${BASE_URL}#/indices`);
    await expect(page.locator('.index-cards')).toBeVisible();
    await page.click('.lang-tabs .lang-tab >> text=English');
    await expect(page.locator('.index-cards')).toBeVisible();
    expect(apiCalls).toContain('EN');
  });

  test('clicking All clears lang filter', async ({ page }) => {
    const apiCalls = [];
    await page.route(`${API_BASE}/indices**`, async (route) => {
      const url = new URL(route.request().url());
      apiCalls.push(url.searchParams.get('lang'));
      const data = generateMockIndices(null, url.searchParams.get('game'), url.searchParams.get('lang'));
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(data),
      });
    });
    await page.route(`${API_BASE}/catalog`, async (route) => {
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(MOCK_CATALOG) });
    });

    await page.goto(`${BASE_URL}#/indices`);
    await expect(page.locator('.index-cards')).toBeVisible();
    await page.click('.lang-tabs .lang-tab >> text=English');
    await expect(page.locator('.index-cards')).toBeVisible();
    await page.click('.lang-tabs .lang-tab >> text=All');
    await expect(page.locator('.index-cards')).toBeVisible();
    // Last call should have null lang (no lang param)
    expect(apiCalls[apiCalls.length - 1]).toBeNull();
  });

  test('language persists when changing range buttons', async ({ page }) => {
    const apiCalls = [];
    await page.route(`${API_BASE}/indices**`, async (route) => {
      const url = new URL(route.request().url());
      apiCalls.push({ lang: url.searchParams.get('lang'), days: url.searchParams.get('days') });
      const data = generateMockIndices(
        url.searchParams.get('days') ? parseInt(url.searchParams.get('days')) : null,
        url.searchParams.get('game'),
        url.searchParams.get('lang')
      );
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(data),
      });
    });
    await page.route(`${API_BASE}/catalog`, async (route) => {
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(MOCK_CATALOG) });
    });

    await page.goto(`${BASE_URL}#/indices`);
    await expect(page.locator('.index-cards')).toBeVisible();
    // Select Japanese
    await page.click('.lang-tabs .lang-tab >> text=Japanese');
    await expect(page.locator('.index-cards')).toBeVisible();
    // Change range to 7D
    await page.click('.range-btn >> text=7D');
    await page.waitForTimeout(500);
    // The 7D call should still have lang=JP
    const rangeCall = apiCalls.find(c => c.days === '7' && c.lang === 'JP');
    expect(rangeCall).toBeTruthy();
  });
});

// ════════════════════════════════════════════════════════════════
// Market Index — Mobile
// ════════════════════════════════════════════════════════════════

test.describe('Market Index Mobile', () => {
  test.use({ viewport: { width: 375, height: 812 } });

  test.beforeEach(async ({ page }) => {
    await mockAPI(page);
  });

  test('index cards stack on mobile', async ({ page }) => {
    await page.goto(`${BASE_URL}#/indices`);
    await expect(page.locator('.index-cards')).toBeVisible();
    const grid = page.locator('.index-cards');
    const style = await grid.evaluate((el) => getComputedStyle(el).gridTemplateColumns);
    expect(style).not.toContain('1fr 1fr 1fr');
  });
});

// ════════════════════════════════════════════════════════════════
// Language Filter
// ════════════════════════════════════════════════════════════════

test.describe('Language Filter', () => {
  test.beforeEach(async ({ page }) => {
    await mockAPI(page);
    await page.goto(BASE_URL);
    await expect(page.locator('.catalog-table')).toBeVisible();
  });

  test('language toggle shows when multiple languages exist', async ({ page }) => {
    const langTabs = page.locator('.lang-tabs .lang-tab');
    await expect(langTabs).toHaveCount(3); // All, Japanese, English
    await expect(langTabs.nth(0)).toContainText('All');
    await expect(langTabs.nth(1)).toContainText('English');
    await expect(langTabs.nth(2)).toContainText('Japanese');
  });

  test('clicking English filters to EN cards only', async ({ page }) => {
    await page.click('.lang-tab >> text=English');
    await expect(page.locator('.lang-tab >> text=English')).toHaveClass(/active/);
    await expect(page.locator('.catalog-table tbody tr')).toHaveCount(4);
    await expect(page.locator('.count')).toContainText('4 cards');
  });

  test('clicking Japanese filters to JP cards only', async ({ page }) => {
    await page.click('.lang-tab >> text=Japanese');
    await expect(page.locator('.lang-tab >> text=Japanese')).toHaveClass(/active/);
    await expect(page.locator('.catalog-table tbody tr')).toHaveCount(8);
    await expect(page.locator('.count')).toContainText('8 cards');
  });

  test('clicking All shows all cards', async ({ page }) => {
    await page.click('.lang-tab >> text=English');
    await expect(page.locator('.catalog-table tbody tr')).toHaveCount(4);
    await page.click('.lang-tabs .lang-tab >> text=All');
    await expect(page.locator('.catalog-table tbody tr')).toHaveCount(12);
  });

  test('card count updates on language filter', async ({ page }) => {
    await expect(page.locator('.count')).toContainText('12 cards');
    await page.click('.lang-tab >> text=English');
    await expect(page.locator('.count')).toContainText('4 cards');
    await page.click('.lang-tab >> text=Japanese');
    await expect(page.locator('.count')).toContainText('8 cards');
  });

  test('EN filter shows both base and parallel EN cards', async ({ page }) => {
    await page.click('.lang-tab >> text=English');
    await expect(page.locator('.catalog-table tbody tr')).toHaveCount(4);
    // Should include base EN and parallel P1/P2
    const text = await page.locator('.catalog-table tbody').textContent();
    expect(text).toContain('P1');
    expect(text).toContain('P2');
  });
});

// ════════════════════════════════════════════════════════════════
// Card Names, Rarity Badges, Variant Badges
// ════════════════════════════════════════════════════════════════

test.describe('Card Metadata', () => {
  test.beforeEach(async ({ page }) => {
    await mockAPI(page);
    await page.goto(BASE_URL);
    await expect(page.locator('.catalog-table')).toBeVisible();
  });

  test('card names appear in table for EN cards', async ({ page }) => {
    // EN cards have card_name set in mock data
    const nameCell = page.locator('.name-cell');
    await expect(nameCell.first()).toBeVisible();
    // At least one row should contain "Roronoa Zoro"
    const text = await page.locator('.catalog-table tbody').textContent();
    expect(text).toContain('Roronoa Zoro');
  });

  test('rarity badges rendered with correct class', async ({ page }) => {
    // The EN OP01-001 card has rarity SR
    const srBadges = page.locator('.rarity-badge.rarity-sr');
    const count = await srBadges.count();
    expect(count).toBeGreaterThan(0);
    await expect(srBadges.first()).toContainText('SR');
  });

  test('variant badges shown for parallel SKUs', async ({ page }) => {
    const variantBadges = page.locator('.variant-badge');
    // P1 and P2 variants in mock data
    const count = await variantBadges.count();
    expect(count).toBeGreaterThanOrEqual(2);
    await expect(variantBadges.first()).toContainText('P1');
  });

  test('JP cards show no card name', async ({ page }) => {
    // Filter to JP only
    await page.click('.lang-tab >> text=Japanese');
    await expect(page.locator('.catalog-table tbody tr')).toHaveCount(8);
    // JP cards have null card_name — name cells should be empty
    const text = await page.locator('.catalog-table tbody').textContent();
    expect(text).not.toContain('Roronoa Zoro');
  });

  test('Name column is sortable', async ({ page }) => {
    const nameHeader = page.locator('th[data-sort="name"]');
    await expect(nameHeader).toBeVisible();
    await nameHeader.click();
    // After sorting by name, verify it changed
    await expect(nameHeader).toContainText('\u2191');
  });
});

test.describe('Card Detail Metadata', () => {
  test.beforeEach(async ({ page }) => {
    await mockAPI(page);
  });

  test('EN card detail shows card name in title', async ({ page }) => {
    await page.goto(`${BASE_URL}#/sku/OP-OP01-001-EN`);
    await expect(page.locator('.sku-title')).toBeVisible();
    await expect(page.locator('.sku-title')).toContainText('Roronoa Zoro');
  });

  test('EN card detail shows rarity and type badges', async ({ page }) => {
    await page.goto(`${BASE_URL}#/sku/OP-OP01-001-EN`);
    await expect(page.locator('.card-detail-meta')).toBeVisible();
    await expect(page.locator('.rarity-badge')).toContainText('SR');
    await expect(page.locator('.meta-tag')).toHaveCount(2); // card_type + card_color
  });

  test('parallel variant detail shows variant badge', async ({ page }) => {
    await page.goto(`${BASE_URL}#/sku/OP-OP01-001-EN-P1`);
    await expect(page.locator('.card-detail-meta')).toBeVisible();
    await expect(page.locator('.variant-badge')).toContainText('P1');
  });

  test('JP card detail has no card metadata section', async ({ page }) => {
    await page.goto(`${BASE_URL}#/sku/OP-OP01-001-JP`);
    await expect(page.locator('.sku-title')).toBeVisible();
    await expect(page.locator('.card-detail-meta')).toHaveCount(0);
  });
});

// ════════════════════════════════════════════════════════════════
// Currency Conversion
// ════════════════════════════════════════════════════════════════

test.describe('Currency Conversion', () => {
  test.beforeEach(async ({ page }) => {
    await mockAPI(page);
  });

  test('currency buttons appear (GBP, USD, EUR, JPY)', async ({ page }) => {
    await page.goto(BASE_URL);
    await expect(page.locator('.catalog-table')).toBeVisible();
    const tabs = page.locator('#currency-selector .tab');
    await expect(tabs).toHaveCount(4);
    await expect(tabs.nth(0)).toContainText('GBP');
    await expect(tabs.nth(1)).toContainText('USD');
    await expect(tabs.nth(2)).toContainText('EUR');
    await expect(tabs.nth(3)).toContainText('JPY');
  });

  test('GBP is active by default', async ({ page }) => {
    await page.goto(BASE_URL);
    await expect(page.locator('.catalog-table')).toBeVisible();
    await expect(page.locator('#currency-selector .tab.active')).toContainText('GBP');
  });

  test('clicking USD updates catalog header and prices', async ({ page }) => {
    await page.goto(BASE_URL);
    await expect(page.locator('.catalog-table')).toBeVisible();
    await page.click('#currency-selector .tab >> text=USD');
    // Header should say Value (USD)
    await expect(page.locator('th[data-sort="price"]')).toContainText('Value (USD)');
    // Prices should show $ instead of £
    const firstPrice = page.locator('.catalog-table tbody tr').first().locator('td').nth(3);
    await expect(firstPrice).toContainText('$');
  });

  test('clicking JPY shows yen symbol with no decimals', async ({ page }) => {
    await page.goto(BASE_URL);
    await expect(page.locator('.catalog-table')).toBeVisible();
    await page.click('#currency-selector .tab >> text=JPY');
    const firstPrice = page.locator('.catalog-table tbody tr').first().locator('td').nth(3);
    const text = await firstPrice.textContent();
    expect(text).toContain('\u00a5');
    // JPY should have no decimal point
    expect(text).not.toContain('.');
  });

  test('switching back to GBP restores pound prices', async ({ page }) => {
    await page.goto(BASE_URL);
    await expect(page.locator('.catalog-table')).toBeVisible();
    await page.click('#currency-selector .tab >> text=USD');
    await expect(page.locator('th[data-sort="price"]')).toContainText('Value (USD)');
    await page.click('#currency-selector .tab >> text=GBP');
    await expect(page.locator('th[data-sort="price"]')).toContainText('Value (GBP)');
    const firstPrice = page.locator('.catalog-table tbody tr').first().locator('td').nth(3);
    await expect(firstPrice).toContainText('\u00a3');
  });

  test('preference persists across reload (localStorage)', async ({ page }) => {
    await page.goto(BASE_URL);
    await expect(page.locator('.catalog-table')).toBeVisible();
    await page.click('#currency-selector .tab >> text=EUR');
    await expect(page.locator('th[data-sort="price"]')).toContainText('Value (EUR)');
    // Reload
    await page.reload();
    await expect(page.locator('.catalog-table')).toBeVisible();
    await expect(page.locator('#currency-selector .tab.active')).toContainText('EUR');
    await expect(page.locator('th[data-sort="price"]')).toContainText('Value (EUR)');
  });

  test('price range converts on set page', async ({ page }) => {
    await page.goto(`${BASE_URL}#/set/OP01`);
    await expect(page.locator('.catalog-table')).toBeVisible();
    // Default GBP should show £
    await expect(page.locator('.range')).toContainText('\u00a3');
    await page.click('#currency-selector .tab >> text=USD');
    await expect(page.locator('.range')).toContainText('$');
  });

  test('detail page price cards convert', async ({ page }) => {
    await page.goto(`${BASE_URL}#/sku/OP-OP01-001-JP`);
    await expect(page.locator('.price-card')).toHaveCount(3);
    // Default GBP
    await expect(page.locator('.price-card').first()).toContainText('\u00a3142.80');
    // Switch to USD
    await page.click('#currency-selector .tab >> text=USD');
    const text = await page.locator('.price-card').first().locator('.price-card__value').textContent();
    expect(text).toContain('$');
    // 142.80 * 1.36 = 194.21
    expect(text).toContain('$194.21');
  });

  test('indices total values convert', async ({ page }) => {
    await page.goto(`${BASE_URL}#/indices`);
    await expect(page.locator('.index-cards')).toBeVisible();
    // Default GBP
    await expect(page.locator('.index-card__meta').first()).toContainText('\u00a3');
    await page.click('#currency-selector .tab >> text=USD');
    await expect(page.locator('.index-card__meta').first()).toContainText('$');
  });

  test('set breakdown table converts', async ({ page }) => {
    await page.goto(`${BASE_URL}#/indices`);
    await expect(page.locator('.set-breakdown')).toBeVisible();
    await page.click('#currency-selector .tab >> text=EUR');
    const avgCell = page.locator('.set-breakdown tbody tr').first().locator('td').nth(3);
    await expect(avgCell).toContainText('\u20ac');
  });

  test('FX API failure falls back gracefully', async ({ page }) => {
    // Override to fail the FX API
    await page.route('https://api.frankfurter.app/**', async (route) => {
      await route.fulfill({ status: 500, body: 'error' });
    });
    await page.route(`${API_BASE}/catalog`, async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(MOCK_CATALOG),
      });
    });
    await page.route(`${API_BASE}/indices**`, async (route) => {
      const data = generateMockIndices(14);
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(data) });
    });
    await page.goto(BASE_URL);
    await expect(page.locator('.catalog-table')).toBeVisible();
    // Should still work with fallback rates
    await page.click('#currency-selector .tab >> text=USD');
    const firstPrice = page.locator('.catalog-table tbody tr').first().locator('td').nth(3);
    await expect(firstPrice).toContainText('$');
  });

  test('footer currency label updates', async ({ page }) => {
    await page.goto(BASE_URL);
    await expect(page.locator('.catalog-table')).toBeVisible();
    await expect(page.locator('#footer-currency')).toContainText('GBP');
    await page.click('#currency-selector .tab >> text=USD');
    await expect(page.locator('#footer-currency')).toContainText('USD');
  });
});
