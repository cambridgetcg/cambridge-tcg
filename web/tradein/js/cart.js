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
        cash_price: item.cash_price,
        credit_price: item.credit_price,
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
          ' \u2014 Cash: \u00a3' + totals.cash.toFixed(2) +
          ' / Credit: \u00a3' + totals.credit.toFixed(2);
        bar.style.display = '';
      } else {
        bar.style.display = 'none';
      }
    }
  },
};
