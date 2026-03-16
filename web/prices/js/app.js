/**
 * Card Price Checker — Router + Page Controllers
 *
 * Hash routes:
 *   #/              → Price list (all cards)
 *   #/set/{CODE}    → Set view (filtered)
 *   #/sku/{SKU}     → Card detail with price history chart
 *   #/indices       → Market indices (S&P 500 style)
 */

// ── State ──────────────────────────────────────────────────────────
let catalogData = null;
let currentChart = null;
let currentSort = { key: 'sku', asc: true };
let indexChart = null;
let setIndexChart = null;
let currentIndicesData = null;
let currentIndexDays = null;
let currentIndexLang = null;  // null = All, 'JP', 'EN'

const SHOP_URL = 'https://cambridgetcg.com';

// ── Router ─────────────────────────────────────────────────────────
function route() {
  const hash = location.hash || '#/';
  const app = document.getElementById('app');

  // Clean up previous charts
  if (currentChart) { currentChart.destroy(); currentChart = null; }
  if (indexChart) { indexChart.destroy(); indexChart = null; }
  if (setIndexChart) { setIndexChart.destroy(); setIndexChart = null; }

  // Update active nav link
  var navLinks = document.querySelectorAll('.site-header__nav a');
  for (var i = 0; i < navLinks.length; i++) {
    var href = navLinks[i].getAttribute('href');
    if (href && href.charAt(0) === '#') {
      navLinks[i].classList.toggle('active', hash === href || (href === '#/' && !hash.startsWith('#/indices') && hash === '#/'));
    }
  }

  if (hash === '#/indices' || hash.startsWith('#/indices?')) {
    renderIndices(app);
  } else if (hash.startsWith('#/sku/')) {
    const sku = decodeURIComponent(hash.slice(6));
    renderSkuDetail(app, sku);
  } else if (hash.startsWith('#/set/')) {
    const code = decodeURIComponent(hash.slice(6));
    renderCatalog(app, { setFilter: code });
  } else {
    renderCatalog(app);
  }
}

window.addEventListener('hashchange', route);

// ── Init ───────────────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', async function() {
  try {
    await Currency.init();
    Currency.renderSelector(document.getElementById('currency-selector'));
    Currency.onChange(function() {
      route();
      var fc = document.getElementById('footer-currency');
      if (fc) fc.textContent = Currency.current();
    });
    catalogData = await PriceAPI.getCatalog();
    route();
  } catch (err) {
    document.getElementById('app').innerHTML =
      '<div class="error-msg">Failed to load prices. Please try again later.</div>';
    console.error(err);
  }
});

// ── Catalog Page ───────────────────────────────────────────────────
function renderCatalog(container, opts) {
  opts = opts || {};
  const skus = catalogData.skus;

  // Derive unique games, languages, and sets
  const games = [...new Set(skus.map(s => s.game).filter(Boolean))].sort();
  const langs = [...new Set(skus.map(s => s.lang).filter(Boolean))].sort();
  const allSets = [...new Set(skus.map(s => s.set_code).filter(Boolean))].sort(naturalSort);

  // Active filters
  const activeGame = opts.gameFilter || null;
  const activeSet = opts.setFilter || null;
  const activeLang = opts.langFilter || null;

  // If we have a set filter, infer the game
  let inferredGame = activeGame;
  if (activeSet && !inferredGame) {
    const sample = skus.find(s => s.set_code === activeSet);
    if (sample) inferredGame = sample.game;
  }

  // Filter by language first (affects set pills and counts)
  let langFiltered = skus;
  if (activeLang) langFiltered = langFiltered.filter(s => s.lang === activeLang);

  // Filter sets by game (within language-filtered data)
  const visibleSets = inferredGame
    ? allSets.filter(sc => langFiltered.some(s => s.set_code === sc && s.game === inferredGame))
    : allSets.filter(sc => langFiltered.some(s => s.set_code === sc));

  // Page title
  let pageTitle = 'All Cards';
  if (activeSet) {
    pageTitle = activeSet + ' \u2014 ' + getSetName(activeSet);
  } else if (inferredGame) {
    pageTitle = getGameName(inferredGame);
  }

  // Filter data
  let filtered = langFiltered;
  if (inferredGame) filtered = filtered.filter(s => s.game === inferredGame);
  if (activeSet) filtered = filtered.filter(s => s.set_code === activeSet);

  // SEO: dynamic title + meta per page
  var langLabel = activeLang === 'EN' ? 'English' : activeLang === 'JP' ? 'Japanese' : 'Japanese & English';
  if (activeSet) {
    var setFullName = getSetName(activeSet);
    var gameLabel = inferredGame === 'OP' ? 'One Piece' : inferredGame === 'PKMN' ? 'Pokemon' : '';
    setPageMeta(
      activeSet + ' ' + setFullName + ' Card Prices | ' + gameLabel + ' ' + langLabel + ' TCG Price Guide',
      'Check current prices for ' + langLabel + ' ' + gameLabel + ' ' + activeSet + ' ' + setFullName + ' cards. ' + filtered.length + ' cards tracked with daily price updates and history charts.'
    );
  } else if (inferredGame) {
    var gName = getGameName(inferredGame);
    setPageMeta(
      langLabel + ' ' + gName + ' Card Prices | Cambridge TCG Price Guide',
      'Free daily price guide for ' + langLabel + ' ' + gName + ' trading cards. Browse all sets, check current market values, and track price history.'
    );
  } else {
    setPageMeta(
      langLabel + ' One Piece & Pokemon Card Prices | Cambridge TCG Price Guide',
      'Free daily price guide for ' + langLabel + ' One Piece TCG and Pokemon cards. Check current market values for ' + filtered.length + '+ cards across all sets. Track price history charts and market indices.'
    );
  }

  // Build HTML
  let html = '';

  // Intro
  html += '<p class="intro">Look up the latest market value for your trading cards. Prices updated daily.</p>';

  // Index ticker placeholder (loaded async)
  html += '<div id="index-ticker"></div>';

  // Search bar
  html += '<div class="search-bar">';
  html += '<input type="text" id="search-input" placeholder="Search by set or card number\u2026" autocomplete="off">';
  html += '</div>';

  // Game tabs
  html += '<div class="game-tabs">';
  html += '<button class="tab' + (!inferredGame ? ' active' : '') + '" data-game="">All</button>';
  for (const g of games) {
    html += '<button class="tab' + (inferredGame === g ? ' active' : '') + '" data-game="' + g + '">' + getGameName(g) + '</button>';
  }
  html += '</div>';

  // Language toggle (only show when multiple languages exist)
  if (langs.length > 1) {
    html += '<div class="lang-tabs">';
    html += '<button class="lang-tab' + (!activeLang ? ' active' : '') + '" data-lang="">All</button>';
    for (var li = 0; li < langs.length; li++) {
      var langCode = langs[li];
      var langName = langCode === 'JP' ? 'Japanese' : langCode === 'EN' ? 'English' : langCode;
      html += '<button class="lang-tab' + (activeLang === langCode ? ' active' : '') + '" data-lang="' + langCode + '">' + langName + '</button>';
    }
    html += '</div>';
  }

  // Set pills
  html += '<div class="set-pills">';
  if (!activeSet) {
    html += '<span class="pill active">All Sets</span>';
  } else {
    html += '<a class="pill" href="#/' + (inferredGame ? '?game=' + inferredGame : '') + '">All Sets</a>';
  }
  for (const sc of visibleSets) {
    if (sc === activeSet) {
      html += '<span class="pill active">' + sc + '</span>';
    } else {
      html += '<a class="pill" href="#/set/' + sc + '">' + sc + '</a>';
    }
  }
  html += '</div>';

  // Stats
  html += '<div class="catalog-stats">';
  html += '<h2 class="page-title">' + pageTitle + '</h2>';
  html += '<span class="count">' + filtered.length + ' cards</span>';
  if (activeSet && filtered.length > 0) {
    const prices = filtered.map(s => s.shopify_price).filter(Boolean);
    if (prices.length) {
      html += '<span class="range">' + Currency.format(Math.min(...prices)) + ' \u2013 ' + Currency.format(Math.max(...prices)) + '</span>';
    }
  }
  html += '</div>';

  // Set index chart (only on set pages)
  if (activeSet) {
    html += '<div class="range-buttons">';
    html += '<button class="range-btn" data-days="30">30D</button>';
    html += '<button class="range-btn" data-days="90">90D</button>';
    html += '<button class="range-btn active" data-days="">All</button>';
    html += '</div>';
    html += '<div class="chart-container"><canvas id="set-chart"></canvas></div>';
  }

  // Table
  html += renderCatalogTable(filtered, !activeLang && langs.length > 1);

  container.innerHTML = html;

  // Load set index chart (non-blocking)
  if (activeSet) {
    loadSetIndexOnPage(activeSet, null);
    var setBtns = container.querySelectorAll('.range-btn');
    for (var b = 0; b < setBtns.length; b++) {
      setBtns[b].addEventListener('click', function() {
        for (var j = 0; j < setBtns.length; j++) setBtns[j].classList.remove('active');
        this.classList.add('active');
        var days = this.getAttribute('data-days');
        loadSetIndexOnPage(activeSet, days ? parseInt(days) : null);
      });
    }
  }

  // Event: search
  var searchInput = document.getElementById('search-input');
  searchInput.addEventListener('input', function() {
    var q = this.value.trim().toLowerCase();
    var rows = container.querySelectorAll('tbody tr');
    for (var i = 0; i < rows.length; i++) {
      var text = rows[i].textContent.toLowerCase();
      rows[i].style.display = text.indexOf(q) >= 0 ? '' : 'none';
    }
  });

  // Event: game tabs
  var tabs = container.querySelectorAll('.game-tabs .tab');
  for (var i = 0; i < tabs.length; i++) {
    tabs[i].addEventListener('click', function() {
      var game = this.getAttribute('data-game');
      if (game) {
        renderCatalog(container, { gameFilter: game, langFilter: activeLang });
      } else {
        renderCatalog(container, { langFilter: activeLang });
      }
    });
  }

  // Event: language tabs
  var langTabs = container.querySelectorAll('.lang-tab');
  for (var i = 0; i < langTabs.length; i++) {
    langTabs[i].addEventListener('click', function() {
      var lang = this.getAttribute('data-lang');
      var newOpts = { gameFilter: inferredGame };
      if (lang) newOpts.langFilter = lang;
      renderCatalog(container, newOpts);
    });
  }

  // Event: sortable headers
  var headers = container.querySelectorAll('th[data-sort]');
  for (var i = 0; i < headers.length; i++) {
    headers[i].addEventListener('click', function() {
      var key = this.getAttribute('data-sort');
      if (currentSort.key === key) {
        currentSort.asc = !currentSort.asc;
      } else {
        currentSort.key = key;
        currentSort.asc = true;
      }
      renderCatalog(container, { gameFilter: inferredGame, setFilter: activeSet, langFilter: activeLang });
    });
  }

  // Load index ticker (non-blocking)
  loadIndexTicker();
}


function renderCatalogTable(items, showLang) {
  // Sort
  var sorted = items.slice().sort(function(a, b) {
    var key = currentSort.key;
    var va, vb;
    if (key === 'price') {
      va = a.shopify_price || 0;
      vb = b.shopify_price || 0;
    } else if (key === 'name') {
      va = (a.card_name || '').toLowerCase();
      vb = (b.card_name || '').toLowerCase();
    } else {
      va = a.sku || '';
      vb = b.sku || '';
    }
    if (va < vb) return currentSort.asc ? -1 : 1;
    if (va > vb) return currentSort.asc ? 1 : -1;
    return 0;
  });

  var sortIcon = function(key) {
    if (currentSort.key !== key) return ' \u2195';
    return currentSort.asc ? ' \u2191' : ' \u2193';
  };

  var html = '<table class="catalog-table">';
  html += '<thead><tr>';
  html += '<th data-sort="sku" class="sortable">Card' + sortIcon('sku') + '</th>';
  html += '<th data-sort="name" class="sortable">Name' + sortIcon('name') + '</th>';
  html += '<th>Set</th>';
  html += '<th data-sort="price" class="sortable">' + Currency.label() + sortIcon('price') + '</th>';
  html += '<th></th>';
  html += '</tr></thead>';
  html += '<tbody>';

  for (var i = 0; i < sorted.length; i++) {
    var s = sorted[i];
    var display = formatSkuShort(s.sku);
    if (showLang && s.lang) {
      display += ' <span class="lang-badge lang-' + s.lang.toLowerCase() + '">' + s.lang + '</span>';
    }
    if (s.variant) {
      display += ' <span class="variant-badge">' + s.variant + '</span>';
    }
    var setName = getSetName(s.set_code);
    var price = s.shopify_price ? Currency.format(s.shopify_price) : '\u2014';
    var buyUrl = SHOP_URL + '/search?q=' + encodeURIComponent(s.sku);

    // Card name with rarity badge
    var nameCell = '';
    if (s.card_name) {
      nameCell = s.card_name;
      if (s.rarity) {
        nameCell += ' <span class="rarity-badge rarity-' + s.rarity.toLowerCase() + '">' + s.rarity + '</span>';
      }
    }

    html += '<tr>';
    html += '<td><a href="#/sku/' + encodeURIComponent(s.sku) + '">' + display + '</a></td>';
    html += '<td class="name-cell">' + nameCell + '</td>';
    html += '<td><a href="#/set/' + s.set_code + '" class="set-link">' + s.set_code + '</a> <span class="set-name-hint">' + setName + '</span></td>';
    html += '<td>' + price + '</td>';
    html += '<td><a href="' + buyUrl + '" target="_blank" rel="noopener" class="table-buy-link">Buy</a></td>';
    html += '</tr>';
  }

  html += '</tbody></table>';
  return html;
}


// ── SKU Detail Page ────────────────────────────────────────────────
async function renderSkuDetail(container, sku) {
  var parsed = parseSku(sku);
  var cardInfo = catalogData.skus.find(function(s) { return s.sku === sku; });
  var buyUrl = SHOP_URL + '/search?q=' + encodeURIComponent(sku);

  // SEO
  var skuDisplay = formatSkuShort(sku);
  var setName = parsed.setCode ? getSetName(parsed.setCode) : '';
  var gameFull = parsed.game === 'OP' ? 'One Piece' : parsed.game === 'PKMN' ? 'Pokemon' : '';
  var langFull = parsed.lang === 'EN' ? 'English' : 'Japanese';
  var priceStr = cardInfo && cardInfo.shopify_price ? Currency.format(cardInfo.shopify_price) : '';
  var cardTitle = cardInfo && cardInfo.card_name ? cardInfo.card_name + ' (' + skuDisplay + ')' : skuDisplay;
  setPageMeta(
    cardTitle + ' Price' + (setName ? ' | ' + setName : '') + ' | ' + langFull + ' ' + gameFull + ' TCG',
    'Current price' + (priceStr ? ' ' + priceStr : '') + ' for ' + langFull + ' ' + gameFull + ' card ' + cardTitle + (setName ? ' from ' + setName : '') + '. View price history chart, compare prices across platforms, and buy online.'
  );

  // Breadcrumb
  var html = '<nav class="breadcrumb">';
  html += '<a href="#/">Home</a>';
  if (parsed.game) {
    html += ' <span class="sep">\u203a</span> <a href="#/" data-game="' + parsed.game + '">' + getGameName(parsed.game) + '</a>';
  }
  if (parsed.setCode) {
    html += ' <span class="sep">\u203a</span> <a href="#/set/' + parsed.setCode + '">' + parsed.setCode + '</a>';
  }
  html += ' <span class="sep">\u203a</span> <span>' + formatSkuShort(sku) + '</span>';
  html += '</nav>';

  // Title — use card name if available
  var title = formatSkuShort(sku);
  if (cardInfo && cardInfo.card_name) {
    title = cardInfo.card_name;
    if (parsed.setCode) title += ' \u2014 ' + getSetName(parsed.setCode);
  } else if (parsed.setCode) {
    title += ' \u2014 ' + getSetName(parsed.setCode);
  }
  html += '<h2 class="sku-title">' + title + '</h2>';
  html += '<p class="sku-code">' + sku + '</p>';

  // Card metadata badges
  if (cardInfo && (cardInfo.rarity || cardInfo.card_type || cardInfo.card_color || cardInfo.variant)) {
    html += '<div class="card-detail-meta">';
    if (cardInfo.rarity) {
      html += '<span class="rarity-badge rarity-' + cardInfo.rarity.toLowerCase() + '">' + cardInfo.rarity + '</span>';
    }
    if (cardInfo.card_type) {
      html += '<span class="meta-tag">' + cardInfo.card_type + '</span>';
    }
    if (cardInfo.card_color) {
      html += '<span class="meta-tag">' + cardInfo.card_color + '</span>';
    }
    if (cardInfo.variant) {
      html += '<span class="variant-badge">' + cardInfo.variant + '</span>';
    }
    html += '</div>';
  }

  // Range buttons
  html += '<div class="range-buttons">';
  html += '<button class="range-btn active" data-days="30">30D</button>';
  html += '<button class="range-btn" data-days="90">90D</button>';
  html += '<button class="range-btn" data-days="365">1Y</button>';
  html += '<button class="range-btn" data-days="">All</button>';
  html += '</div>';

  // Chart container
  html += '<div class="chart-container"><canvas id="price-chart"></canvas></div>';

  // Stats (filled after chart loads)
  html += '<div id="price-stats" class="price-stats"></div>';

  // Price comparison
  html += '<div class="price-section-title">Where to buy</div>';
  html += '<div class="price-grid">';
  if (cardInfo) {
    html += priceCard('Cambridge TCG', cardInfo.shopify_price, buyUrl);
    html += priceCard('eBay', cardInfo.ebay_price, null);
    html += priceCard('Cardmarket', cardInfo.cardmarket_price, null);
  }
  html += '</div>';

  // Buy CTA
  html += '<div class="sku-actions">';
  html += '<a href="' + buyUrl + '" target="_blank" rel="noopener" class="buy-link">Buy on Cambridge TCG \u2192</a>';
  html += '</div>';

  container.innerHTML = html;

  // Load chart
  await loadChart(sku, 30);

  // Range button events
  var btns = container.querySelectorAll('.range-btn');
  for (var i = 0; i < btns.length; i++) {
    btns[i].addEventListener('click', function() {
      for (var j = 0; j < btns.length; j++) btns[j].classList.remove('active');
      this.classList.add('active');
      var days = this.getAttribute('data-days');
      loadChart(sku, days ? parseInt(days) : null);
    });
  }
}


function priceCard(platform, price, link) {
  var display = price ? Currency.format(price) : '\u2014';
  var html = '<div class="price-card">';
  html += '<div class="price-card__label">' + platform + '</div>';
  html += '<div class="price-card__value">' + display + '</div>';
  if (link && price) {
    html += '<a href="' + link + '" target="_blank" rel="noopener" class="price-card__buy">Buy</a>';
  }
  html += '</div>';
  return html;
}


async function loadChart(sku, days) {
  try {
    var data = await PriceAPI.getPrices(sku, days);
    var labels = data.prices.map(function(p) { return p.date; });
    var prices = data.prices.map(function(p) { return p.selling_price_gbp; });

    if (currentChart) {
      currentChart.destroy();
      currentChart = null;
    }

    var canvas = document.getElementById('price-chart');
    if (!canvas) return;

    currentChart = new Chart(canvas, createPriceChartConfig(labels, prices));

    // Stats
    renderPriceStats(prices);
  } catch (err) {
    console.error('Chart load failed:', err);
    var statsEl = document.getElementById('price-stats');
    if (statsEl) statsEl.innerHTML = '<p class="error-msg">Failed to load price history.</p>';
  }
}


function renderPriceStats(prices) {
  var statsEl = document.getElementById('price-stats');
  if (!statsEl || !prices.length) return;

  var valid = prices.filter(function(p) { return p != null; });
  if (!valid.length) return;

  var current = valid[valid.length - 1];
  var high = Math.max.apply(null, valid);
  var low = Math.min.apply(null, valid);

  statsEl.innerHTML =
    '<div class="stat"><span class="stat__label">Current Value</span><span class="stat__value">' + Currency.format(current) + '</span></div>' +
    '<div class="stat"><span class="stat__label">Period High</span><span class="stat__value">' + Currency.format(high) + '</span></div>' +
    '<div class="stat"><span class="stat__label">Period Low</span><span class="stat__value">' + Currency.format(low) + '</span></div>';
}


// ── Set Index Chart (set page) ────────────────────────────────────
async function loadSetIndexOnPage(setCode, days) {
  try {
    // Infer game from catalog data to enable lazy-load optimization
    var game = 'OP';
    if (catalogData && catalogData.skus) {
      var sample = catalogData.skus.find(function(s) { return s.set_code === setCode; });
      if (sample) game = sample.game;
    }
    var data = await PriceAPI.getIndices(days, game);
    if (!data.set_series || !data.set_series[setCode]) return;
    var hist = data.set_series[setCode].history;
    var labels = hist.map(function(h) { return h.date; });
    var values = hist.map(function(h) { return h.index; });
    var datasets = [{ key: setCode, name: setCode + ' \u2014 ' + getSetName(setCode), data: values }];

    if (currentChart) { currentChart.destroy(); currentChart = null; }
    var canvas = document.getElementById('set-chart');
    if (!canvas) return;
    currentChart = new Chart(canvas, createIndexChartConfig(labels, datasets));
  } catch (err) {
    console.error('Set index chart load failed:', err);
  }
}


// ── Index Ticker (catalog page) ───────────────────────────────────
async function loadIndexTicker() {
  var el = document.getElementById('index-ticker');
  if (!el) return;
  try {
    var data = await PriceAPI.getIndices(7);
    if (!data.series || !Object.keys(data.series).length) return;
    var order = ['OP', 'PKMN'];
    var html = '<a href="#/indices" class="index-ticker">';
    for (var i = 0; i < order.length; i++) {
      var s = data.series[order[i]];
      if (!s) continue;
      var change = s.change_1d || 0;
      var arrow = change > 0 ? '\u25b2' : change < 0 ? '\u25bc' : '';
      var cls = change > 0 ? 'index-up' : change < 0 ? 'index-down' : 'index-flat';
      html += '<span class="ticker-item">';
      html += '<span class="ticker-item__label">' + s.name + '</span>';
      html += '<span class="ticker-item__value">' + (s.current_index || 100).toFixed(2) + '</span>';
      html += '<span class="ticker-item__change ' + cls + '">' + arrow + ' ' + (change >= 0 ? '+' : '') + change.toFixed(2) + '%</span>';
      html += '</span>';
    }
    html += '</a>';
    el.innerHTML = html;
  } catch (err) {
    console.error('Ticker load failed:', err);
  }
}


// ── Indices Page ──────────────────────────────────────────────────
async function renderIndices(container) {
  setPageMeta(
    'Japanese Card Market Index | One Piece & Pokemon TCG Price Trends',
    'Track the overall Japanese trading card market with daily indices for One Piece TCG and Pokemon cards. See price trends across all sets with our S&P 500-style market tracker.'
  );
  container.innerHTML = '<div class="loading">Loading market data...</div>';

  try {
    var data = await PriceAPI.getIndices(null, null, currentIndexLang);
    if (!data.series || !Object.keys(data.series).length) {
      container.innerHTML = '<div class="error-msg">No index data yet. Check back after the pricing pipeline has run.</div>';
      return;
    }

    var html = '';

    // Breadcrumb
    html += '<nav class="breadcrumb">';
    html += '<a href="#/">Home</a> <span class="sep">\u203a</span> <span>Market Index</span>';
    html += '</nav>';

    // Language tabs
    html += '<div class="lang-tabs">';
    html += '<button class="lang-tab' + (!currentIndexLang ? ' active' : '') + '" data-lang="">All</button>';
    html += '<button class="lang-tab' + (currentIndexLang === 'JP' ? ' active' : '') + '" data-lang="JP">Japanese</button>';
    html += '<button class="lang-tab' + (currentIndexLang === 'EN' ? ' active' : '') + '" data-lang="EN">English</button>';
    html += '</div>';

    // Headline cards
    var order = ['OP-JP', 'OP-EN', 'PKMN-JP', 'PKMN-EN'];
    html += '<div class="index-cards">';
    for (var i = 0; i < order.length; i++) {
      var s = data.series[order[i]];
      if (!s) continue;
      var change = s.change_1d || 0;
      var arrow = change > 0 ? '\u25b2' : change < 0 ? '\u25bc' : '';
      var cls = change > 0 ? 'index-up' : change < 0 ? 'index-down' : 'index-flat';
      var total = s.total_value || 0;
      var totalStr = Currency.formatValue(total);

      html += '<div class="index-card">';
      html += '<div class="index-card__label">' + s.name + '</div>';
      html += '<div class="index-card__value">' + (s.current_index || 100).toFixed(2) + '</div>';
      html += '<div class="index-card__change ' + cls + '">' + arrow + ' ' + (change >= 0 ? '+' : '') + change.toFixed(2) + '%</div>';
      html += '<div class="index-card__meta">' + (s.sku_count || 0) + ' cards \u00b7 ' + totalStr + '</div>';
      html += '</div>';
    }
    html += '</div>';

    // Range buttons
    html += '<div class="range-buttons">';
    html += '<button class="range-btn" data-days="7">7D</button>';
    html += '<button class="range-btn" data-days="30">30D</button>';
    html += '<button class="range-btn" data-days="90">90D</button>';
    html += '<button class="range-btn active" data-days="">All</button>';
    html += '</div>';

    // Chart
    html += '<div class="chart-container"><canvas id="index-chart"></canvas></div>';

    // Set Breakdown
    if (data.sets && data.sets.length) {
      html += '<div class="price-section-title">Set Breakdown</div>';
      html += '<table class="set-breakdown">';
      html += '<thead><tr>';
      html += '<th>Set</th><th>Game</th><th>Cards</th><th>Avg Price</th><th>Total Value</th><th>1D Change</th>';
      html += '</tr></thead><tbody>';
      for (var j = 0; j < data.sets.length; j++) {
        var row = data.sets[j];
        var pctChange = row.pct_change || 0;
        var changeCls = pctChange > 0 ? 'change-positive' : pctChange < 0 ? 'change-negative' : 'change-zero';
        var gameName = row.game === 'OP' ? 'One Piece' : row.game === 'PKMN' ? 'Pokemon' : row.game;
        html += '<tr>';
        var langLabel = row.lang === 'EN' ? 'English' : 'Japanese';
        html += '<td><a href="#/set/' + row.set_code + '">' + row.set_code + ' \u2014 ' + getSetName(row.set_code) + ' ' + langLabel + '</a></td>';
        html += '<td>' + gameName + '</td>';
        html += '<td>' + row.card_count + '</td>';
        html += '<td>' + Currency.format(row.avg_price) + '</td>';
        html += '<td>' + Currency.format(row.total_value) + '</td>';
        html += '<td class="' + changeCls + '">' + (pctChange >= 0 ? '+' : '') + pctChange.toFixed(2) + '%</td>';
        html += '</tr>';
      }
      html += '</tbody></table>';
    }

    // Set Indices chart (loaded lazily via separate API call)
    if (data.sets && data.sets.length) {
      html += '<div class="price-section-title" style="margin-top:24px">Set Indices</div>';
      var setGames = {};
      for (var si = 0; si < data.sets.length; si++) { setGames[data.sets[si].game] = true; }
      var gameList = Object.keys(setGames).sort();
      if (gameList.length > 1) {
        html += '<div class="game-tabs" id="set-index-tabs">';
        for (var g = 0; g < gameList.length; g++) {
          html += '<button class="tab' + (g === 0 ? ' active' : '') + '" data-game="' + gameList[g] + '">' + getGameName(gameList[g]) + '</button>';
        }
        html += '</div>';
      }
      html += '<div class="chart-container"><canvas id="set-index-chart"></canvas></div>';
    }

    container.innerHTML = html;
    currentIndicesData = data;

    // Build game-level chart
    buildIndexChart(data);

    // Lazy-load set-level chart (separate API call with game param)
    if (data.sets && data.sets.length) {
      var setGames2 = {};
      for (var si2 = 0; si2 < data.sets.length; si2++) { setGames2[data.sets[si2].game] = true; }
      var gameList2 = Object.keys(setGames2).sort();
      var defaultSetGame = gameList2[0] || 'OP';
      loadSetIndices(defaultSetGame, null);

      var setTabs = container.querySelectorAll('#set-index-tabs .tab');
      for (var t = 0; t < setTabs.length; t++) {
        setTabs[t].addEventListener('click', function() {
          for (var m = 0; m < setTabs.length; m++) setTabs[m].classList.remove('active');
          this.classList.add('active');
          loadSetIndices(this.getAttribute('data-game'), currentIndexDays);
        });
      }
    }

    // Range button events
    var btns = container.querySelectorAll('.range-btn');
    for (var k = 0; k < btns.length; k++) {
      btns[k].addEventListener('click', function() {
        for (var m = 0; m < btns.length; m++) btns[m].classList.remove('active');
        this.classList.add('active');
        var days = this.getAttribute('data-days');
        loadIndexChart(days ? parseInt(days) : null);
      });
    }

    // Language tab events
    var langTabs = container.querySelectorAll('.lang-tabs .lang-tab');
    for (var lt = 0; lt < langTabs.length; lt++) {
      langTabs[lt].addEventListener('click', function() {
        var lang = this.getAttribute('data-lang');
        currentIndexLang = lang || null;
        renderIndices(container);
      });
    }

  } catch (err) {
    container.innerHTML = '<div class="error-msg">Failed to load market data. Please try again later.</div>';
    console.error(err);
  }
}


function buildIndexChart(data) {
  var order = ['OP-JP', 'OP-EN', 'PKMN-JP', 'PKMN-EN'];
  var labels = [];
  var datasets = [];

  // Collect all unique dates from ALL series history
  var dateSet = {};
  for (var i = 0; i < order.length; i++) {
    var s = data.series[order[i]];
    if (!s || !s.history) continue;
    for (var j = 0; j < s.history.length; j++) {
      dateSet[s.history[j].date] = true;
    }
  }
  labels = Object.keys(dateSet).sort();

  for (var i = 0; i < order.length; i++) {
    var s = data.series[order[i]];
    if (!s || !s.history) continue;
    var dateMap = {};
    for (var j = 0; j < s.history.length; j++) {
      dateMap[s.history[j].date] = s.history[j].index;
    }
    var values = labels.map(function(d) { return dateMap[d] != null ? dateMap[d] : null; });
    datasets.push({ key: order[i], name: s.name, data: values });
  }

  var canvas = document.getElementById('index-chart');
  if (!canvas) return;

  if (indexChart) { indexChart.destroy(); indexChart = null; }
  indexChart = new Chart(canvas, createIndexChartConfig(labels, datasets));
}


function buildSetIndexChart(data, game) {
  if (!data.set_series) return;
  var sets = [];
  for (var key in data.set_series) {
    if (data.set_series[key].game === game) sets.push(key);
  }
  sets.sort(naturalSort);

  var dateSet = {};
  for (var i = 0; i < sets.length; i++) {
    var hist = data.set_series[sets[i]].history;
    for (var j = 0; j < hist.length; j++) { dateSet[hist[j].date] = true; }
  }
  var labels = Object.keys(dateSet).sort();

  var datasets = [];
  for (var i = 0; i < sets.length; i++) {
    var sc = sets[i];
    var entry = data.set_series[sc];
    var hist = entry.history;
    var dateMap = {};
    for (var j = 0; j < hist.length; j++) { dateMap[hist[j].date] = hist[j].index; }
    var values = labels.map(function(d) { return dateMap[d] != null ? dateMap[d] : null; });
    var setCode = entry.set_code || sc;
    var langSuffix = entry.lang ? ' (' + entry.lang + ')' : '';
    datasets.push({ key: sc, name: setCode + ' \u2014 ' + getSetName(setCode) + langSuffix, data: values });
  }

  var canvas = document.getElementById('set-index-chart');
  if (!canvas) return;

  if (setIndexChart) { setIndexChart.destroy(); setIndexChart = null; }
  setIndexChart = new Chart(canvas, createIndexChartConfig(labels, datasets));
}


async function loadSetIndices(game, days) {
  try {
    var data = await PriceAPI.getIndices(days, game, currentIndexLang);
    if (!data.set_series) return;
    buildSetIndexChart(data, game);
  } catch (err) {
    console.error('Set indices load failed:', err);
  }
}


async function loadIndexChart(days) {
  try {
    var data = await PriceAPI.getIndices(days, null, currentIndexLang);
    if (!data.series) return;
    currentIndicesData = data;
    currentIndexDays = days;
    buildIndexChart(data);
    // Reload set chart with current game selection (separate API call)
    var activeTab = document.querySelector('#set-index-tabs .tab.active');
    var game = activeTab ? activeTab.getAttribute('data-game') : 'OP';
    loadSetIndices(game, days);
  } catch (err) {
    console.error('Index chart load failed:', err);
  }
}


// ── SEO Helpers ───────────────────────────────────────────────────
function setPageMeta(title, description) {
  document.title = title;
  var meta = document.querySelector('meta[name="description"]');
  if (meta) meta.setAttribute('content', description);
  var ogTitle = document.querySelector('meta[property="og:title"]');
  if (ogTitle) ogTitle.setAttribute('content', title);
  var ogDesc = document.querySelector('meta[property="og:description"]');
  if (ogDesc) ogDesc.setAttribute('content', description);
  var canonical = document.querySelector('link[rel="canonical"]');
  if (canonical) canonical.setAttribute('href', 'https://prices.cambridgetcg.com/' + location.hash);
}

// ── Helpers ────────────────────────────────────────────────────────
function naturalSort(a, b) {
  return a.localeCompare(b, undefined, { numeric: true, sensitivity: 'base' });
}
