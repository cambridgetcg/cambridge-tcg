/**
 * Trade-In Buy List — Router + Page Controllers
 *
 * Hash routes:
 *   #/              → Full buy list
 *   #/set/{CODE}    → Set view (filtered)
 *   #/cart           → Cart review
 *   #/submit         → Submit form
 *   #/confirm/{ref}  → Confirmation page
 *   #/terms          → Trade-in terms
 *   #/status         → Check trade-in status
 */

// ── State ──────────────────────────────────────────────────────────
var buylistData = null;
var currentSort = { key: 'sku', asc: true };
var showBuyingOnly = false;
var lastConfirmation = null; // stores POST response for confirm page

// ── Router ─────────────────────────────────────────────────────────
function route() {
  var hash = location.hash || '#/';
  var app = document.getElementById('app');

  if (hash.startsWith('#/set/')) {
    var code = decodeURIComponent(hash.slice(6));
    renderBuyList(app, { setFilter: code });
  } else if (hash === '#/cart') {
    renderCart(app);
  } else if (hash === '#/submit') {
    renderSubmitForm(app);
  } else if (hash.startsWith('#/confirm/')) {
    var ref = decodeURIComponent(hash.slice(10));
    renderConfirmation(app, ref);
  } else if (hash === '#/terms') {
    renderTerms(app);
  } else if (hash === '#/status') {
    renderStatusCheck(app);
  } else {
    renderBuyList(app);
  }
}

window.addEventListener('hashchange', route);

// ── Init ───────────────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', async function() {
  try {
    buylistData = await BuyListAPI.getBuyList();
    Cart.updateBadge();
    route();
  } catch (err) {
    document.getElementById('app').innerHTML =
      '<div class="error-msg">Failed to load buy list. Please try again later.</div>';
    console.error(err);
  }
});

// ── Buy List Page ──────────────────────────────────────────────────
function renderBuyList(container, opts) {
  opts = opts || {};
  var items = buylistData.items;
  var activeSet = opts.setFilter || null;

  var allSets = [];
  var seen = {};
  for (var i = 0; i < items.length; i++) {
    var sc = items[i].set_code;
    if (sc && !seen[sc]) { allSets.push(sc); seen[sc] = true; }
  }
  allSets.sort(naturalSort);

  var pageTitle = 'Buy List';
  if (activeSet) pageTitle = activeSet + ' \u2014 ' + getSetName(activeSet);

  var filtered = items;
  if (activeSet) filtered = filtered.filter(function(s) { return s.set_code === activeSet; });

  var html = '';

  // Intro
  html += '<p class="intro">Cards we\'re currently looking to buy. All prices shown are for <strong>Near Mint (NM)</strong> condition. We offer cash or store credit.</p>';
  html += '<p class="intro mint-intro">\u2728 <strong>MINT Bonus:</strong> Cards assessed as Mint condition by Cambridge TCG may receive an additional +15% bonus on top of the NM price. This bonus is at our discretion and should be viewed as a reward for exceptional condition.</p>';

  // Summary stats
  html += '<div class="summary-cards">';
  html += '<div class="summary-card">';
  html += '<div class="summary-card__value">' + filtered.length + '</div>';
  html += '<div class="summary-card__label">Cards We Buy</div>';
  html += '</div>';
  html += '<div class="summary-card">';
  html += '<div class="summary-card__value">\u2728 +15%</div>';
  html += '<div class="summary-card__label">MINT Bonus</div>';
  html += '</div>';
  html += '</div>';

  // Search bar
  html += '<div class="search-bar">';
  html += '<input type="text" id="search-input" placeholder="Search by set or card number\u2026" autocomplete="off">';
  html += '</div>';

  // Set pills
  html += '<div class="set-pills">';
  if (!activeSet) {
    html += '<span class="pill active">All Sets</span>';
  } else {
    html += '<a class="pill" href="#/">All Sets</a>';
  }
  for (var i = 0; i < allSets.length; i++) {
    var sc = allSets[i];
    if (sc === activeSet) {
      html += '<span class="pill active">' + sc + '</span>';
    } else {
      html += '<a class="pill" href="#/set/' + sc + '">' + sc + '</a>';
    }
  }
  html += '</div>';

  // Filter controls
  html += '<div class="filter-row">';
  html += '<div class="catalog-stats">';
  html += '<h2 class="page-title">' + pageTitle + '</h2>';
  html += '<span class="count">' + filtered.length + ' cards</span>';
  html += '</div>';
  // All cards are buyable — no toggle needed
  html += '</div>';

  // Table
  html += renderBuyListTable(filtered);

  // Cart bar (sticky bottom)
  html += '<div id="cart-bar" class="cart-bar" style="display:none">';
  html += '<span class="cart-bar__text"></span>';
  html += '<a href="#/cart" class="btn btn-primary">View Cart</a>';
  html += '</div>';

  container.innerHTML = html;

  // Event: search (safe — new input element each render, so no duplicate)
  var searchInput = document.getElementById('search-input');
  searchInput.addEventListener('input', function() {
    var q = this.value.trim().toLowerCase();
    var rows = container.querySelectorAll('tbody tr');
    for (var i = 0; i < rows.length; i++) {
      var text = rows[i].textContent.toLowerCase();
      rows[i].style.display = (!q || text.indexOf(q) >= 0) ? '' : 'none';
    }
  });

  // Event: sortable headers (safe — new th elements each render)
  var headers = container.querySelectorAll('th[data-sort]');
  for (var i = 0; i < headers.length; i++) {
    headers[i].addEventListener('click', function() {
      var key = this.getAttribute('data-sort');
      if (currentSort.key === key) {
        currentSort.asc = !currentSort.asc;
      } else {
        currentSort.key = key;
        currentSort.asc = (key === 'sku');
      }
      renderBuyList(container, opts);
    });
  }

  // Event: add-to-cart buttons (use a named handler to prevent duplicate listeners)
  if (!container._cartClickHandler) {
    container._cartClickHandler = function(e) {
      var btn = e.target.closest('.btn-add-cart');
      if (!btn) return;
      var sku = btn.getAttribute('data-sku');
      // Find item from current buylist data
      var item = null;
      if (buylistData && buylistData.items) {
        for (var i = 0; i < buylistData.items.length; i++) {
          if (buylistData.items[i].sku === sku) { item = buylistData.items[i]; break; }
        }
      }
      if (item) {
        Cart.add(item);
        // Update button text
        var cart = Cart.get();
        var inCart = cart[sku] ? cart[sku].qty : 0;
        btn.textContent = inCart > 0 ? inCart + ' in cart' : '+';
        btn.classList.toggle('in-cart', inCart > 0);
      }
    };
    container.addEventListener('click', container._cartClickHandler);
  }

  // Apply initial visibility + cart badge
  applyVisibility(container);
  Cart.updateBadge();
}


function applyVisibility(container) {
  var q = '';
  var searchInput = document.getElementById('search-input');
  if (searchInput) q = searchInput.value.trim().toLowerCase();

  var rows = container.querySelectorAll('tbody tr');
  for (var i = 0; i < rows.length; i++) {
    var text = rows[i].textContent.toLowerCase();
    rows[i].style.display = (!q || text.indexOf(q) >= 0) ? '' : 'none';
  }
}


function renderBuyListTable(items) {
  var sorted = items.slice().sort(function(a, b) {
    var key = currentSort.key;
    var va, vb;
    if (key === 'cash') { va = a.cash_price || 0; vb = b.cash_price || 0; }
    else if (key === 'credit') { va = a.credit_price || 0; vb = b.credit_price || 0; }
    else if (key === 'cash_want') { va = a.cash_want || 0; vb = b.cash_want || 0; }
    else { va = a.sku || ''; vb = b.sku || ''; }
    if (va < vb) return currentSort.asc ? -1 : 1;
    if (va > vb) return currentSort.asc ? 1 : -1;
    return 0;
  });

  var sortIcon = function(key) {
    if (currentSort.key !== key) return ' \u2195';
    return currentSort.asc ? ' \u2191' : ' \u2193';
  };

  var cart = Cart.get();

  var html = '<table class="catalog-table">';
  html += '<thead><tr>';
  html += '<th class="col-thumb"></th>';
  html += '<th data-sort="sku" class="sortable">Card' + sortIcon('sku') + '</th>';
  html += '<th class="col-set">Set</th>';
  html += '<th data-sort="cash" class="sortable">Cash (NM)' + sortIcon('cash') + '</th>';
  html += '<th data-sort="credit" class="sortable">Credit (NM)' + sortIcon('credit') + '</th>';
  html += '<th class="col-mint">\u2728 MINT Bonus</th>';
  html += '<th class="col-cart">Add</th>';
  html += '</tr></thead>';
  html += '<tbody>';

  for (var i = 0; i < sorted.length; i++) {
    var s = sorted[i];
    var display = formatSkuShort(s.sku);
    var setName = getSetName(s.set_code);
    var cash = '\u00a3' + s.cash_price.toFixed(2);
    var credit = '\u00a3' + s.credit_price.toFixed(2);
    var inCart = cart[s.sku] ? cart[s.sku].qty : 0;
    var btnText = inCart > 0 ? inCart + ' in cart' : '+';
    var btnCls = 'btn-add-cart' + (inCart > 0 ? ' in-cart' : '');

    // MINT bonus: show the extra amount on top of NM price
    var mintBonusCash = (s.mint_cash_price && s.mint_cash_price > s.cash_price) ? (s.mint_cash_price - s.cash_price) : 0;
    var mintBonusCredit = (s.mint_credit_price && s.mint_credit_price > s.credit_price) ? (s.mint_credit_price - s.credit_price) : 0;
    var mintDisplay = mintBonusCash > 0 ? '+\u00a3' + mintBonusCash.toFixed(2) : '\u2014';

    // Card thumbnail: S3 primary, CardRush fallback, blank on final error
    var thumbHtml = '';
    if (s.image_url) {
      var fallback = s.image_fallback
        ? "this.src='" + s.image_fallback.replace(/'/g, "\\'") + "';this.onerror=function(){this.style.display='none'};"
        : "this.style.display='none';";
      var fbAttr = s.image_fallback ? ' data-fallback="' + s.image_fallback.replace(/"/g, '&quot;') + '"' : '';
      thumbHtml = '<img class="card-thumb" src="' + s.image_url + '" alt="' + display + '" loading="lazy"' + fbAttr + ' onerror="' + fallback + '">';
    }

    html += '<tr>';
    html += '<td class="col-thumb">' + thumbHtml + '</td>';
    html += '<td>' + display + '</td>';
    html += '<td class="col-set"><a href="#/set/' + s.set_code + '" class="set-link">' + s.set_code + '</a> <span class="set-name-hint">' + setName + '</span></td>';
    html += '<td>' + cash + '</td>';
    html += '<td class="credit-price">' + credit + '</td>';
    html += '<td class="col-mint"><span class="mint-badge">' + mintDisplay + '</span></td>';
    html += '<td class="col-cart"><button class="' + btnCls + '" data-sku="' + s.sku + '">' + btnText + '</button></td>';
    html += '</tr>';
  }

  html += '</tbody></table>';
  return html;
}


// ── Cart Page ──────────────────────────────────────────────────────
function renderCart(container) {
  var cartItems = Cart.items();
  var html = '';

  html += '<div class="page-header">';
  html += '<h2 class="page-title">Trade-In Cart</h2>';
  if (cartItems.length > 0) {
    html += '<button class="btn btn-muted" id="clear-cart">Clear Cart</button>';
  }
  html += '</div>';

  if (cartItems.length === 0) {
    html += '<div class="empty-state">';
    html += '<p>Your cart is empty.</p>';
    html += '<a href="#/" class="btn btn-primary">Browse Buy List</a>';
    html += '</div>';
    container.innerHTML = html;
    return;
  }

  // Cart table
  html += '<table class="catalog-table cart-table">';
  html += '<thead><tr>';
  html += '<th>Card</th>';
  html += '<th>Qty</th>';
  html += '<th>Cash (NM)</th>';
  html += '<th>Credit (NM)</th>';
  html += '<th>\u2728 MINT Bonus</th>';
  html += '<th></th>';
  html += '</tr></thead>';
  html += '<tbody>';

  var totals = { count: 0, cash: 0, credit: 0 };
  for (var i = 0; i < cartItems.length; i++) {
    var item = cartItems[i];
    var display = formatSkuShort(item.sku);
    // Always use NM prices as the base
    var cashUnit = item.cash_price;
    var creditUnit = item.credit_price;
    totals.count += item.qty;
    totals.cash += cashUnit * item.qty;
    totals.credit += creditUnit * item.qty;

    // MINT bonus is extra, shown separately
    var mintBonusCash = (item.mint_cash_price && item.mint_cash_price > item.cash_price) ? (item.mint_cash_price - item.cash_price) : 0;
    var mintBonusDisplay = mintBonusCash > 0 ? '+\u00a3' + mintBonusCash.toFixed(2) : '\u2014';

    html += '<tr>';
    html += '<td>' + display + '</td>';
    html += '<td><div class="qty-control">';
    html += '<button class="qty-btn" data-sku="' + item.sku + '" data-action="dec">\u2212</button>';
    html += '<span class="qty-value">' + item.qty + '</span>';
    html += '<button class="qty-btn" data-sku="' + item.sku + '" data-action="inc">+</button>';
    html += '</div></td>';
    html += '<td>\u00a3' + cashUnit.toFixed(2) + '</td>';
    html += '<td class="credit-price">\u00a3' + creditUnit.toFixed(2) + '</td>';
    html += '<td class="col-mint"><span class="mint-badge">' + mintBonusDisplay + '</span></td>';
    html += '<td><button class="btn-remove" data-sku="' + item.sku + '">\u2715</button></td>';
    html += '</tr>';
  }

  html += '</tbody>';
  html += '<tfoot><tr class="cart-totals">';
  html += '<td><strong>' + totals.count + ' card' + (totals.count === 1 ? '' : 's') + '</strong></td>';
  html += '<td></td>';
  html += '<td><strong>\u00a3' + totals.cash.toFixed(2) + '</strong></td>';
  html += '<td class="credit-price"><strong>\u00a3' + totals.credit.toFixed(2) + '</strong></td>';
  html += '<td></td>';
  html += '<td></td>';
  html += '</tr></tfoot>';
  html += '</table>';
  html += '<p class="form-note mint-intro">\u2728 <strong>MINT Bonus:</strong> If your cards are assessed as Mint condition by Cambridge TCG, you may receive up to +15% on top of the NM prices shown above. This bonus is at our discretion and should be viewed as a reward for exceptional condition.</p>';

  if (totals.credit < 5.0) {
    html += '<p class="form-note">\u26a0 Minimum trade-in value is \u00a35.00 (credit). Add more cards to proceed.</p>';
  }

  html += '<div class="cart-actions">';
  html += '<a href="#/" class="btn btn-muted">\u2190 Back to Buy List</a>';
  if (totals.credit >= 5.0) {
    html += '<a href="#/submit" class="btn btn-primary">Proceed to Submit \u2192</a>';
  }
  html += '</div>';

  container.innerHTML = html;

  // Events
  container.addEventListener('click', function(e) {
    var qtyBtn = e.target.closest('.qty-btn');
    if (qtyBtn) {
      var sku = qtyBtn.getAttribute('data-sku');
      var action = qtyBtn.getAttribute('data-action');
      var cart = Cart.get();
      if (cart[sku]) {
        var newQty = cart[sku].qty + (action === 'inc' ? 1 : -1);
        Cart.updateQty(sku, newQty);
        renderCart(container);
      }
      return;
    }

    // Condition toggle (NM vs A-)
    if (e.target.type === 'radio' && e.target.getAttribute('data-sku')) {
      Cart.setCondition(e.target.getAttribute('data-sku'), e.target.value);
      renderCart(container);
      return;
    }

    var removeBtn = e.target.closest('.btn-remove');
    if (removeBtn) {
      Cart.remove(removeBtn.getAttribute('data-sku'));
      renderCart(container);
      return;
    }

    if (e.target.id === 'clear-cart') {
      Cart.clear();
      renderCart(container);
    }
  });
}


// ── Submit Form ────────────────────────────────────────────────────
function renderSubmitForm(container) {
  var cartItems = Cart.items();
  if (cartItems.length === 0) {
    location.hash = '#/cart';
    return;
  }

  var totals = Cart.totals();
  var html = '';

  html += '<h2 class="page-title">Submit Trade-In</h2>';
  html += '<p class="intro">Review your details and submit your trade-in request. NM prices are locked for 7 days from submission. MINT bonus (if applicable) will be assessed when we receive your cards.</p>';

  html += '<form id="tradein-form" class="tradein-form">';

  // Your details
  html += '<fieldset>';
  html += '<legend>Your Details</legend>';
  html += '<div class="form-row">';
  html += '<label for="ti-name">Full Name *</label>';
  html += '<input type="text" id="ti-name" required autocomplete="name">';
  html += '</div>';
  html += '<div class="form-row">';
  html += '<label for="ti-email">Email *</label>';
  html += '<input type="email" id="ti-email" required autocomplete="email">';
  html += '</div>';
  html += '<div class="form-row">';
  html += '<label for="ti-phone">Phone (optional)</label>';
  html += '<input type="tel" id="ti-phone" autocomplete="tel">';
  html += '</div>';
  html += '</fieldset>';

  // Payment preference
  html += '<fieldset>';
  html += '<legend>Payment Preference</legend>';
  html += '<div class="radio-group">';
  html += '<label class="radio-label"><input type="radio" name="payment" value="credit" checked>';
  html += ' <strong>Store Credit</strong> \u2014 \u00a3' + totals.credit.toFixed(2) + ' (NM)</label>';
  html += '<label class="radio-label"><input type="radio" name="payment" value="cash">';
  html += ' <strong>Cash</strong> (bank transfer) \u2014 \u00a3' + totals.cash.toFixed(2) + ' (NM)</label>';
  html += '</div>';
  html += '<p class="form-note mint-note">\u2728 Cards assessed as Mint condition by Cambridge TCG may receive an additional +15% bonus on top of these NM prices.</p>';
  html += '</fieldset>';

  // Delivery method
  html += '<fieldset>';
  html += '<legend>Delivery Method</legend>';
  html += '<div class="radio-group">';
  html += '<label class="radio-label"><input type="radio" name="delivery" value="mail" checked>';
  html += ' <strong>Mail-in</strong> \u2014 post your cards to us</label>';
  html += '<label class="radio-label"><input type="radio" name="delivery" value="instore">';
  html += ' <strong>In-store</strong> \u2014 bring to our Cambridge shop</label>';
  html += '</div>';
  html += '</fieldset>';

  // Declarations
  html += '<fieldset>';
  html += '<legend>Declarations</legend>';
  html += '<label class="checkbox-label"><input type="checkbox" id="ti-condition" required>';
  html += ' I confirm all cards are at least Near Mint (NM) condition</label>';
  html += '<label class="checkbox-label"><input type="checkbox" id="ti-age" required>';
  html += ' I am 18 or over, or a parent/guardian is submitting on my behalf</label>';
  html += '</fieldset>';

  // Notes
  html += '<fieldset>';
  html += '<legend>Notes (optional)</legend>';
  html += '<textarea id="ti-notes" rows="3" placeholder="Any additional information\u2026"></textarea>';
  html += '</fieldset>';

  // Honeypot
  html += '<div style="display:none" aria-hidden="true">';
  html += '<input type="text" id="ti-website" name="website" tabindex="-1" autocomplete="off">';
  html += '</div>';

  // Submit
  html += '<div class="form-actions">';
  html += '<a href="#/cart" class="btn btn-muted">\u2190 Back to Cart</a>';
  html += '<button type="submit" class="btn btn-primary btn-lg" id="submit-btn">Submit Trade-In</button>';
  html += '</div>';

  html += '<p class="form-note">By submitting, you agree to our <a href="#/terms">trade-in terms</a>.</p>';

  html += '<div id="form-error" class="error-msg" style="display:none"></div>';

  html += '</form>';

  container.innerHTML = html;

  // Form submit handler
  document.getElementById('tradein-form').addEventListener('submit', async function(e) {
    e.preventDefault();

    var submitBtn = document.getElementById('submit-btn');
    var errorDiv = document.getElementById('form-error');
    errorDiv.style.display = 'none';
    submitBtn.disabled = true;
    submitBtn.textContent = 'Submitting\u2026';

    var cartItems = Cart.items();
    var data = {
      customer_name: document.getElementById('ti-name').value.trim(),
      customer_email: document.getElementById('ti-email').value.trim(),
      customer_phone: document.getElementById('ti-phone').value.trim(),
      payment_method: document.querySelector('input[name="payment"]:checked').value,
      delivery_method: document.querySelector('input[name="delivery"]:checked').value,
      is_over_18: document.getElementById('ti-age').checked,
      notes: document.getElementById('ti-notes').value.trim(),
      website: document.getElementById('ti-website').value,
      items: cartItems.map(function(item) {
        return { sku: item.sku, quantity: item.qty, condition: item.condition || 'nm' };
      }),
    };

    try {
      var result = await BuyListAPI.submitTradeIn(data);
      lastConfirmation = result;
      lastConfirmation._customerName = data.customer_name;
      lastConfirmation._customerEmail = data.customer_email;
      lastConfirmation._items = cartItems;
      try { sessionStorage.setItem('ctcg_last_confirm', JSON.stringify(lastConfirmation)); } catch(e) {}
      Cart.clear();
      location.hash = '#/confirm/' + result.reference;
    } catch (err) {
      errorDiv.textContent = err.message;
      errorDiv.style.display = '';
      submitBtn.disabled = false;
      submitBtn.textContent = 'Submit Trade-In';
    }
  });
}


// ── Confirmation Page ──────────────────────────────────────────────
function renderConfirmation(container, reference) {
  var html = '';

  html += '<div class="confirm-page">';
  html += '<div class="confirm-icon">\u2713</div>';
  html += '<h2 class="page-title">Trade-In Submitted</h2>';

  if (!lastConfirmation || lastConfirmation.reference !== reference) {
    try {
      var saved = sessionStorage.getItem('ctcg_last_confirm');
      if (saved) {
        var parsed = JSON.parse(saved);
        if (parsed.reference === reference) lastConfirmation = parsed;
      }
    } catch(e) {}
  }

  if (lastConfirmation && lastConfirmation.reference === reference) {
    var c = lastConfirmation;
    var chosenTotal = c.payment_method === 'credit' ? c.quoted_credit_total : c.quoted_cash_total;
    var paymentLabel = c.payment_method === 'credit' ? 'Store Credit' : 'Cash (bank transfer)';
    var deliveryLabel = c.delivery_method === 'mail' ? 'Mail-in' : 'In-store drop-off';

    html += '<div class="confirm-details">';
    html += '<div class="confirm-row"><span class="confirm-label">Reference</span><span class="confirm-value">' + reference + '</span></div>';
    html += '<div class="confirm-row"><span class="confirm-label">Items</span><span class="confirm-value">' + c.item_count + ' cards</span></div>';
    html += '<div class="confirm-row"><span class="confirm-label">Payment</span><span class="confirm-value">' + paymentLabel + '</span></div>';
    html += '<div class="confirm-row"><span class="confirm-label">Total (NM)</span><span class="confirm-value confirm-total">\u00a3' + chosenTotal.toFixed(2) + '</span></div>';
    html += '<div class="confirm-row"><span class="confirm-label">Delivery</span><span class="confirm-value">' + deliveryLabel + '</span></div>';
    html += '<div class="confirm-row"><span class="confirm-label">Quote Valid Until</span><span class="confirm-value">' + formatDate(c.expires_at) + '</span></div>';
    html += '</div>';

    // Items list
    if (c._items && c._items.length > 0) {
      html += '<h3>Items</h3>';
      html += '<table class="catalog-table">';
      html += '<thead><tr><th>Card</th><th>Qty</th><th>Cash (NM)</th><th>Credit (NM)</th></tr></thead>';
      html += '<tbody>';
      for (var i = 0; i < c._items.length; i++) {
        var item = c._items[i];
        html += '<tr>';
        html += '<td>' + formatSkuShort(item.sku) + '</td>';
        html += '<td>' + item.qty + '</td>';
        html += '<td>\u00a3' + item.cash_price.toFixed(2) + '</td>';
        html += '<td class="credit-price">\u00a3' + item.credit_price.toFixed(2) + '</td>';
        html += '</tr>';
      }
      html += '</tbody></table>';
      html += '<p class="form-note mint-note">\u2728 Prices shown are NM (Near Mint). Cards assessed as Mint condition by Cambridge TCG may receive an additional +15% bonus.</p>';
    }

    // Instructions
    html += '<div class="confirm-instructions">';
    if (c.delivery_method === 'mail') {
      html += '<h3>What\u2019s Next</h3>';
      html += '<ol>';
      html += '<li>Pack your cards carefully (sleeved, in toploaders, rigid mailer)</li>';
      html += '<li>Ship to: <strong>Cambridge TCG, Cambridge, UK</strong></li>';
      html += '<li>Use tracked delivery (Royal Mail Tracked 48 recommended)</li>';
      html += '<li>Email us your tracking number at <a href="mailto:contact@cambridgetcg.com">contact@cambridgetcg.com</a> with reference <strong>' + reference + '</strong></li>';
      html += '<li>Prices are locked for 7 days \u2014 cards must arrive by <strong>' + formatDate(c.expires_at) + '</strong></li>';
      html += '</ol>';
    } else {
      html += '<h3>What\u2019s Next</h3>';
      html += '<ol>';
      html += '<li>Bring your cards to our shop in Cambridge</li>';
      html += '<li>Quote your reference: <strong>' + reference + '</strong></li>';
      html += '<li>We\u2019ll verify and process on the spot</li>';
      html += '</ol>';
    }
    html += '</div>';
  } else {
    // No confirmation data (page refreshed)
    html += '<div class="confirm-details">';
    html += '<div class="confirm-row"><span class="confirm-label">Reference</span><span class="confirm-value">' + reference + '</span></div>';
    html += '<p class="intro">Your trade-in request has been submitted. Check your email for full details and instructions.</p>';
    html += '</div>';
  }

  html += '<div class="cart-actions">';
  html += '<a href="#/" class="btn btn-primary">Back to Buy List</a>';
  html += '</div>';
  html += '</div>';

  container.innerHTML = html;
}


// ── Terms Page ─────────────────────────────────────────────────────
function renderTerms(container) {
  var html = '';
  html += '<h2 class="page-title">Trade-In Terms</h2>';

  html += '<div class="terms-content">';

  html += '<h3>Price Lock</h3>';
  html += '<p>Prices are locked for <strong>7 calendar days</strong> from submission. Cards must be received within this period. After expiry, we\u2019ll offer a new quote at current prices.</p>';

  html += '<h3>Condition Requirements</h3>';
  html += '<ul>';
  html += '<li><strong>Near Mint (NM)</strong>: Listed price. No visible wear, clean edges, no bends, no scratches.</li>';
  html += '<li><strong>Lightly Played (LP)</strong>: 75% of listed NM price. Minor edge wear, light scratches.</li>';
  html += '<li><strong>Below LP</strong>: Not accepted for mail-in. In-store: at staff discretion.</li>';
  html += '</ul>';

  html += '<h3>\u2728 MINT Bonus</h3>';
  html += '<p>Cards assessed as <strong>Mint condition</strong> by Cambridge TCG may receive an additional <strong>+15%</strong> on top of the NM price. This bonus is determined entirely at our discretion based on our assessment of the card\u2019s condition. It should be viewed as a reward for exceptional condition, not a guaranteed rate.</p>';
  html += '<p>Mint means: factory-fresh appearance, no handling marks, perfect centering, no whitening, no surface imperfections. We assess this in person when your cards arrive.</p>';

  html += '<h3>Minimum Values</h3>';
  html += '<p>Mail-in: minimum \u00a35.00 total credit value. In-store: no minimum.</p>';

  html += '<h3>Payment</h3>';
  html += '<ul>';
  html += '<li><strong>Store credit</strong>: Issued within 1 business day of acceptance as a Shopify discount code.</li>';
  html += '<li><strong>Cash</strong>: Paid via bank transfer within 2 business days of acceptance.</li>';
  html += '</ul>';

  html += '<h3>Shipping</h3>';
  html += '<ul>';
  html += '<li>Customer pays outgoing shipping. We recommend Royal Mail Tracked 48.</li>';
  html += '<li>Cards should be sleeved, in toploaders, and shipped in a rigid mailer.</li>';
  html += '<li>We are not responsible for cards lost or damaged in transit.</li>';
  html += '</ul>';

  html += '<h3>Grading & Discrepancies</h3>';
  html += '<p>If we find condition or quantity issues, we\u2019ll email you with an adjusted offer. You\u2019ll have 7 days to accept or request your cards back (return postage: \u00a32.50). No response after 7 days means acceptance of the adjusted price.</p>';

  html += '<h3>Returns</h3>';
  html += '<p>Rejected cards are returned via Royal Mail 2nd Class. Return postage of \u00a32.50 is deducted from payment or invoiced separately.</p>';

  html += '<h3>Age Policy</h3>';
  html += '<p>You must be 18 or over to submit a trade-in. Under-18s require a parent or guardian to submit on their behalf.</p>';

  html += '<h3>Cancellation</h3>';
  html += '<p>You may cancel at any time before payment is issued by emailing <a href="mailto:contact@cambridgetcg.com">contact@cambridgetcg.com</a> with your reference number.</p>';

  html += '</div>';

  html += '<div class="cart-actions">';
  html += '<a href="#/" class="btn btn-muted">\u2190 Back to Buy List</a>';
  html += '</div>';

  container.innerHTML = html;
}


// ── Status Check Page ───────────────────────────────────────────
function renderStatusCheck(container) {
  var html = '';

  html += '<h2 class="page-title">Check Trade-In Status</h2>';
  html += '<p class="intro">Enter your trade-in reference and email to check the status of your submission.</p>';

  html += '<form id="status-form" class="tradein-form">';
  html += '<fieldset>';
  html += '<div class="form-row">';
  html += '<label for="status-ref">Reference Number</label>';
  html += '<input type="text" id="status-ref" required placeholder="TI-XXXXXXXX-XXXX">';
  html += '</div>';
  html += '<div class="form-row">';
  html += '<label for="status-email">Email Address</label>';
  html += '<input type="email" id="status-email" required placeholder="your@email.com">';
  html += '</div>';
  html += '</fieldset>';
  html += '<div class="form-actions">';
  html += '<a href="#/" class="btn btn-muted">\u2190 Back</a>';
  html += '<button type="submit" class="btn btn-primary" id="status-btn">Check Status</button>';
  html += '</div>';
  html += '<div id="status-error" class="error-msg" style="display:none"></div>';
  html += '</form>';

  html += '<div id="status-result" style="display:none"></div>';

  container.innerHTML = html;

  document.getElementById('status-form').addEventListener('submit', async function(e) {
    e.preventDefault();
    var ref = document.getElementById('status-ref').value.trim().toUpperCase();
    var email = document.getElementById('status-email').value.trim();
    var btn = document.getElementById('status-btn');
    var errorDiv = document.getElementById('status-error');
    var resultDiv = document.getElementById('status-result');

    errorDiv.style.display = 'none';
    resultDiv.style.display = 'none';
    btn.disabled = true;
    btn.textContent = 'Checking\u2026';

    try {
      var data = await BuyListAPI.getTradeInStatus(ref, email);
      renderStatusResult(resultDiv, data);
      resultDiv.style.display = '';
    } catch (err) {
      errorDiv.textContent = err.message === 'Trade-in not found'
        ? 'No trade-in found with that reference and email combination.'
        : err.message;
      errorDiv.style.display = '';
    }

    btn.disabled = false;
    btn.textContent = 'Check Status';
  });
}


function renderStatusResult(container, data) {
  var statusLabels = {
    'submitted': 'Submitted',
    'received': 'Cards Received',
    'paid': 'Payment Issued',
    'cancelled': 'Cancelled',
  };
  var statusClass = 'status-' + data.status;
  var statusLabel = statusLabels[data.status] || data.status;
  var chosenTotal = data.chosen_total;
  var paymentLabel = data.payment_method === 'credit' ? 'Store Credit' : 'Cash (bank transfer)';
  var deliveryLabel = data.delivery_method === 'mail' ? 'Mail-in' : 'In-store drop-off';

  var html = '';
  html += '<div class="status-result">';

  html += '<div class="status-header">';
  html += '<span class="status-badge ' + statusClass + '">' + statusLabel + '</span>';
  html += '</div>';

  html += '<div class="confirm-details">';
  html += '<div class="confirm-row"><span class="confirm-label">Reference</span><span class="confirm-value">' + data.reference + '</span></div>';
  html += '<div class="confirm-row"><span class="confirm-label">Status</span><span class="confirm-value"><span class="status-badge ' + statusClass + '">' + statusLabel + '</span></span></div>';
  html += '<div class="confirm-row"><span class="confirm-label">Submitted</span><span class="confirm-value">' + formatDate(data.submitted_at) + '</span></div>';
  html += '<div class="confirm-row"><span class="confirm-label">Payment</span><span class="confirm-value">' + paymentLabel + '</span></div>';
  html += '<div class="confirm-row"><span class="confirm-label">Total</span><span class="confirm-value confirm-total">\u00a3' + chosenTotal.toFixed(2) + '</span></div>';
  html += '<div class="confirm-row"><span class="confirm-label">Delivery</span><span class="confirm-value">' + deliveryLabel + '</span></div>';
  html += '<div class="confirm-row"><span class="confirm-label">Quote Valid Until</span><span class="confirm-value">' + formatDate(data.expires_at) + '</span></div>';

  if (data.tracking_number) {
    html += '<div class="confirm-row"><span class="confirm-label">Tracking</span><span class="confirm-value">' + data.tracking_number + '</span></div>';
  }
  if (data.payment_reference) {
    html += '<div class="confirm-row"><span class="confirm-label">Payment Ref</span><span class="confirm-value">' + data.payment_reference + '</span></div>';
  }

  html += '</div>';

  // Items table
  if (data.items && data.items.length > 0) {
    html += '<h3 style="text-align:center; margin-top:24px; margin-bottom:8px; font-size:16px;">Items</h3>';
    html += '<table class="catalog-table" style="max-width:500px; margin:0 auto;">';
    html += '<thead><tr><th>Card</th><th>Qty</th><th>Cash</th><th>Credit</th></tr></thead>';
    html += '<tbody>';
    for (var i = 0; i < data.items.length; i++) {
      var item = data.items[i];
      html += '<tr>';
      html += '<td>' + formatSkuShort(item.sku) + '</td>';
      html += '<td>' + item.quantity + '</td>';
      html += '<td>\u00a3' + item.cash_price.toFixed(2) + '</td>';
      html += '<td class="credit-price">\u00a3' + item.credit_price.toFixed(2) + '</td>';
      html += '</tr>';
    }
    html += '</tbody></table>';
  }

  html += '</div>';
  container.innerHTML = html;
}


// ── Helpers ────────────────────────────────────────────────────────
function naturalSort(a, b) {
  return a.localeCompare(b, undefined, { numeric: true, sensitivity: 'base' });
}

function formatDate(dateStr) {
  if (!dateStr) return '';
  var d = new Date(dateStr);
  if (isNaN(d)) return dateStr;
  var months = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
  return d.getDate() + ' ' + months[d.getMonth()] + ' ' + d.getFullYear();
}


// ── Card Image Lightbox ────────────────────────────────────────────
(function() {
  var overlay, lbImg, lbLabel;

  function createLightbox() {
    overlay = document.createElement('div');
    overlay.className = 'card-lightbox';
    overlay.setAttribute('role', 'dialog');
    overlay.setAttribute('aria-modal', 'true');
    overlay.innerHTML =
      '<div class="card-lightbox__inner">' +
        '<button class="card-lightbox__close" aria-label="Close">\u00d7</button>' +
        '<img class="card-lightbox__img" src="" alt="">' +
        '<div class="card-lightbox__label"></div>' +
      '</div>';
    document.body.appendChild(overlay);

    lbImg   = overlay.querySelector('.card-lightbox__img');
    lbLabel = overlay.querySelector('.card-lightbox__label');

    // Close on overlay background click
    overlay.addEventListener('click', function(e) {
      if (e.target === overlay) closeLightbox();
    });

    // Close button
    overlay.querySelector('.card-lightbox__close').addEventListener('click', closeLightbox);

    // Esc key
    document.addEventListener('keydown', function(e) {
      if (e.key === 'Escape') closeLightbox();
    });
  }

  function openLightbox(src, fallback, label) {
    if (!overlay) createLightbox();
    lbImg.src = src;
    lbImg.alt = label || '';
    lbLabel.textContent = label || '';
    lbImg.onerror = fallback
      ? function() { lbImg.src = fallback; lbImg.onerror = null; }
      : null;
    overlay.classList.add('open');
    document.body.style.overflow = 'hidden';
  }

  function closeLightbox() {
    if (!overlay) return;
    overlay.classList.remove('open');
    document.body.style.overflow = '';
    lbImg.src = '';
  }

  // Delegate clicks on .card-thumb anywhere in the document
  document.addEventListener('click', function(e) {
    var thumb = e.target.closest ? e.target.closest('.card-thumb') : null;
    if (!thumb) return;
    var src      = thumb.getAttribute('src');
    var fallback = thumb.dataset.fallback || '';
    var label    = thumb.getAttribute('alt') || '';
    if (src) openLightbox(src, fallback, label);
  });
})();
