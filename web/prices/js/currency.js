/**
 * Currency — client-side multi-currency converter for Price Explorer.
 *
 * Supports GBP (default), USD, EUR, JPY.
 * FX rates fetched from Frankfurter API, cached in sessionStorage (5min TTL).
 * User preference persisted in localStorage.
 */
var Currency = (function() {
  var CURRENCIES = {
    GBP: { symbol: '\u00a3', decimals: 2 },
    USD: { symbol: '$', decimals: 2 },
    EUR: { symbol: '\u20ac', decimals: 2 },
    JPY: { symbol: '\u00a5', decimals: 0 },
  };

  var FALLBACK_RATES = { USD: 1.26, EUR: 1.17, JPY: 190 };
  var FX_CACHE_KEY = 'ctcg_fx_rates';
  var PREF_KEY = 'ctcg_currency';
  var CACHE_TTL = 5 * 60 * 1000; // 5 minutes

  var _current = 'GBP';
  var _rates = {}; // GBP -> X
  var _listeners = [];

  function init() {
    // Load preference
    try {
      var saved = localStorage.getItem(PREF_KEY);
      if (saved && CURRENCIES[saved]) _current = saved;
    } catch (e) { /* localStorage unavailable */ }

    // Try cached rates from sessionStorage
    try {
      var cached = sessionStorage.getItem(FX_CACHE_KEY);
      if (cached) {
        var parsed = JSON.parse(cached);
        if (parsed.ts && Date.now() - parsed.ts < CACHE_TTL) {
          _rates = parsed.rates;
          return Promise.resolve();
        }
        // Expired but usable as fallback
        _rates = parsed.rates || FALLBACK_RATES;
      }
    } catch (e) { /* sessionStorage unavailable */ }

    // Fetch fresh rates
    return fetch('https://api.frankfurter.app/latest?from=GBP&to=USD,EUR,JPY')
      .then(function(res) { return res.json(); })
      .then(function(data) {
        if (data && data.rates) {
          _rates = data.rates;
          try {
            sessionStorage.setItem(FX_CACHE_KEY, JSON.stringify({ ts: Date.now(), rates: _rates }));
          } catch (e) { /* ignore */ }
        }
      })
      .catch(function() {
        // Use fallback if no cached rates loaded
        if (!_rates || !_rates.USD) _rates = FALLBACK_RATES;
      });
  }

  function convert(gbpAmount) {
    if (gbpAmount == null) return null;
    if (_current === 'GBP') return gbpAmount;
    var rate = _rates[_current] || FALLBACK_RATES[_current] || 1;
    return gbpAmount * rate;
  }

  function format(gbpAmount) {
    if (gbpAmount == null) return '\u2014';
    if (_current === 'GBP') {
      return '\u00a3' + gbpAmount.toFixed(2);
    }
    var converted = convert(gbpAmount);
    var cur = CURRENCIES[_current];
    return cur.symbol + converted.toFixed(cur.decimals);
  }

  function formatValue(gbpTotal) {
    if (gbpTotal == null) return '\u2014';
    var converted = convert(gbpTotal);
    var cur = CURRENCIES[_current];
    if (converted >= 1000) {
      return cur.symbol + (converted / 1000).toFixed(1) + 'K';
    }
    return cur.symbol + converted.toFixed(0);
  }

  function label() {
    return 'Value (' + _current + ')';
  }

  function chartLabel() {
    return 'Price (' + CURRENCIES[_current].symbol + ')';
  }

  function current() {
    return _current;
  }

  function setCurrency(code) {
    if (!CURRENCIES[code] || code === _current) return;
    _current = code;
    try { localStorage.setItem(PREF_KEY, code); } catch (e) { /* ignore */ }
    for (var i = 0; i < _listeners.length; i++) _listeners[i](code);
  }

  function onChange(cb) {
    _listeners.push(cb);
  }

  function renderSelector(el) {
    if (!el) return;
    var html = '<div class="currency-tabs">';
    var codes = ['GBP', 'USD', 'EUR', 'JPY'];
    for (var i = 0; i < codes.length; i++) {
      var c = codes[i];
      html += '<button class="tab' + (c === _current ? ' active' : '') + '" data-currency="' + c + '">' + c + '</button>';
    }
    html += '</div>';
    el.innerHTML = html;

    var btns = el.querySelectorAll('.tab');
    for (var j = 0; j < btns.length; j++) {
      btns[j].addEventListener('click', function() {
        var code = this.getAttribute('data-currency');
        setCurrency(code);
        // Update active state
        for (var k = 0; k < btns.length; k++) btns[k].classList.remove('active');
        this.classList.add('active');
      });
    }
  }

  return {
    init: init,
    convert: convert,
    format: format,
    formatValue: formatValue,
    label: label,
    chartLabel: chartLabel,
    current: current,
    onChange: onChange,
    renderSelector: renderSelector,
  };
})();
