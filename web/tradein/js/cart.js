/**
 * Trade-In Cart — localStorage persistence + helpers
 */
var CART_KEY = 'ctcg_tradein_cart';

var Cart = {
  get: function() {
    try { return JSON.parse(localStorage.getItem(CART_KEY)) || {}; }
    catch(e) { return {}; }
  },

  set: function(cart) {
    localStorage.setItem(CART_KEY, JSON.stringify(cart));
    Cart.updateBadge();
  },

  /**
   * Validate cart items — remove any with missing/invalid price data.
   * Protects against stale localStorage from previous code versions.
   * Returns true if cart was clean, false if items were purged.
   */
  validate: function() {
    var cart = Cart.get();
    var purged = 0;
    for (var key in cart) {
      var item = cart[key];
      if (typeof item.cash_price !== 'number' || isNaN(item.cash_price) ||
          typeof item.credit_price !== 'number' || isNaN(item.credit_price) ||
          typeof item.qty !== 'number' || item.qty < 1 ||
          !item.sku) {
        delete cart[key];
        purged++;
      }
    }
    if (purged > 0) {
      Cart.set(cart);
      console.warn('[Cart] Purged ' + purged + ' invalid item(s) from localStorage');
    }
    return purged === 0;
  },

  add: function(item) {
    var cart = Cart.get();
    var key = item.sku;
    if (cart[key]) {
      cart[key].qty += 1;
    } else {
      cart[key] = {
        qty: 1,
        sku: item.sku,
        set_code: item.set_code,
        card_number: item.card_number,
        name: item.name || '',
        cash_price: item.cash_price,
        credit_price: item.credit_price,
        mint_cash_price: item.mint_cash_price || null,
        mint_credit_price: item.mint_credit_price || null,
        condition: 'nm', // default to NM (MINT bonus)
      };
    }
    Cart.set(cart);
  },

  remove: function(sku) {
    var cart = Cart.get();
    delete cart[sku];
    Cart.set(cart);
  },

  updateQty: function(sku, qty) {
    var cart = Cart.get();
    if (qty <= 0) {
      delete cart[sku];
    } else if (cart[sku]) {
      cart[sku].qty = qty;
    }
    Cart.set(cart);
  },

  setCondition: function(sku, condition) {
    var cart = Cart.get();
    if (cart[sku]) {
      cart[sku].condition = condition; // 'nm' or 'a-'
    }
    Cart.set(cart);
  },

  clear: function() {
    localStorage.removeItem(CART_KEY);
    Cart.updateBadge();
  },

  count: function() {
    var cart = Cart.get();
    var total = 0;
    for (var key in cart) total += cart[key].qty;
    return total;
  },

  totals: function() {
    var cart = Cart.get();
    var count = 0, cash = 0, credit = 0;
    for (var key in cart) {
      var item = cart[key];
      count += item.qty;
      // Always use NM prices. MINT bonus is discretionary, added by Cambridge TCG after assessment.
      cash += item.cash_price * item.qty;
      credit += item.credit_price * item.qty;
    }
    return { count: count, cash: cash, credit: credit };
  },

  items: function() {
    var cart = Cart.get();
    var arr = [];
    for (var key in cart) arr.push(cart[key]);
    return arr;
  },

  updateBadge: function() {
    var badge = document.getElementById('cart-badge');
    var count = Cart.count();
    if (badge) {
      badge.textContent = count;
      badge.style.display = count > 0 ? '' : 'none';
    }
    // Update cart bar
    var bar = document.getElementById('cart-bar');
    if (bar) {
      if (count > 0) {
        var totals = Cart.totals();
        bar.querySelector('.cart-bar__text').textContent =
          totals.count + ' card' + (totals.count === 1 ? '' : 's') +
          ' — Cash: £' + totals.cash.toFixed(2) +
          ' / Credit: £' + totals.credit.toFixed(2);
        bar.style.display = '';
      } else {
        bar.style.display = 'none';
      }
    }
  },
};
