#!/usr/bin/env node
/**
 * E2E test for the trade-in flow via curl/fetch simulation.
 * Tests the live site data flow without needing a browser.
 */
const https = require('https');

const SITE = 'https://tradein.cambridgetcg.com';
const API  = 'https://tradein-api.cambridgetcg.com';

let passed = 0, failed = 0;

function ok(name)       { passed++; console.log(`  ✓ ${name}`); }
function fail(name, err){ failed++; console.log(`  ✗ ${name}: ${err}`); }

function get(url) {
  return new Promise((resolve, reject) => {
    https.get(url, { timeout: 10000 }, (res) => {
      let body = '';
      res.on('data', d => body += d);
      res.on('end', () => resolve({ status: res.statusCode, body, ok: res.statusCode >= 200 && res.statusCode < 300 }));
    }).on('error', reject).on('timeout', function() { this.destroy(); reject(new Error('timeout')); });
  });
}

function post(url, data) {
  const u = new URL(url);
  const body = JSON.stringify(data);
  return new Promise((resolve, reject) => {
    const req = https.request({
      hostname: u.hostname, path: u.pathname,
      method: 'POST', timeout: 15000,
      headers: { 'Content-Type': 'application/json', 'Content-Length': Buffer.byteLength(body) },
    }, (res) => {
      let d = '';
      res.on('data', chunk => d += chunk);
      res.on('end', () => resolve({ status: res.statusCode, body: d, ok: res.statusCode >= 200 && res.statusCode < 300 }));
    });
    req.on('error', reject);
    req.on('timeout', () => { req.destroy(); reject(new Error('timeout')); });
    req.write(body);
    req.end();
  });
}

async function run() {
  console.log('\n═══ E2E Trade-In Flow Test ═══\n');

  // ── 1. Site loads ──
  console.log('── Site ──');
  const page = await get(SITE + '/');
  page.status === 200 ? ok('Site loads (200)') : fail('Site loads', page.status);

  // Check for our fixed files (cache hash should differ from old)
  const hash = (page.body.match(/v=([a-f0-9]+)/) || [])[1];
  hash ? ok(`Cache hash: ${hash}`) : fail('Cache hash', 'not found in HTML');

  // Check the fixed JS files are being served
  const appJs = await get(SITE + '/js/app.js?v=' + hash);
  appJs.ok ? ok('app.js loads') : fail('app.js', appJs.status);

  // Verify our fixes are deployed
  const hasCartPageHandler = appJs.body.includes('_cartPageHandler');
  hasCartPageHandler ? ok('Fix deployed: _cartPageHandler (no duplicate listeners)') : fail('Fix missing', '_cartPageHandler not in app.js');

  const hasFormError = appJs.body.includes('form-error');
  hasFormError ? ok('Fix deployed: form-error class (visible errors)') : fail('Fix missing', 'form-error not in app.js');

  const hasValidation = appJs.body.includes('Please confirm all cards are Near Mint');
  hasValidation ? ok('Fix deployed: JS-level form validation') : fail('Fix missing', 'JS validation not in app.js');

  const hasCartValidate = appJs.body.includes('Cart.validate');
  hasCartValidate ? ok('Fix deployed: Cart.validate() on init') : fail('Fix missing', 'Cart.validate not in app.js');

  const cartJs = await get(SITE + '/js/cart.js?v=' + hash);
  const hasValidateMethod = cartJs.body.includes('validate: function');
  hasValidateMethod ? ok('Fix deployed: Cart.validate method in cart.js') : fail('Fix missing', 'validate method not in cart.js');

  const skuJs = await get(SITE + '/js/sku-parser.js?v=' + hash);
  const hasNewRegex = skuJs.body.includes('OP|ST|EB|PRB|P|PKMN');
  hasNewRegex ? ok('Fix deployed: Updated SKU regex with all prefixes') : fail('Fix missing', 'new SKU regex not in sku-parser.js');

  const css = await get(SITE + '/css/styles.css?v=' + hash);
  const hasFormErrorCss = css.body.includes('.form-error');
  hasFormErrorCss ? ok('Fix deployed: .form-error CSS (red, visible)') : fail('Fix missing', '.form-error not in styles.css');

  // ── 2. Buylist data ──
  console.log('\n── Buylist ──');
  const bl = await get(SITE + '/data/buylist.json');
  bl.ok ? ok('buylist.json loads') : fail('buylist.json', bl.status);

  const data = JSON.parse(bl.body);
  data.items?.length > 0 ? ok(`${data.items.length} items in buylist`) : fail('Items', 'empty');

  // Check transform compatibility
  const item = data.items[0];
  typeof item.cashPrice === 'number' ? ok('cashPrice is number') : fail('cashPrice type', typeof item.cashPrice);
  typeof item.creditPrice === 'number' ? ok('creditPrice is number') : fail('creditPrice type', typeof item.creditPrice);
  item.sku ? ok(`First SKU: ${item.sku}`) : fail('SKU', 'missing');

  // ── 3. API health ──
  console.log('\n── API ──');
  const health = await get(API + '/health');
  health.ok ? ok('API healthy') : fail('API health', health.status);

  // ── 4. Submit a trade-in ──
  console.log('\n── Submit Flow ──');

  // Pick 3 items worth enough to pass the £5 minimum
  const testItems = data.items.slice(0, 3);
  const cartCredit = testItems.reduce((s, i) => s + i.creditPrice, 0);
  console.log(`  Cart: ${testItems.length} items, credit total: £${cartCredit.toFixed(2)}`);
  cartCredit >= 5.0 ? ok('Cart meets £5 minimum') : fail('Cart minimum', `£${cartCredit.toFixed(2)} < £5`);

  const submitData = {
    customerName: 'E2E Test ' + Date.now(),
    customerEmail: 'e2e-test@cambridgetcg.com',
    customerPhone: '',
    paymentMethod: 'credit',
    deliveryMethod: 'mail',
    isOver18: true,
    notes: 'Automated E2E test — safe to ignore',
    website: '',  // honeypot empty
    items: testItems.map(i => ({ sku: i.sku, quantity: 1, condition: 'nm' })),
  };

  const resp = await post(API + '/tradein', submitData);
  resp.ok ? ok(`POST /tradein → ${resp.status}`) : fail('POST /tradein', `${resp.status}: ${resp.body.slice(0, 100)}`);

  const result = JSON.parse(resp.body);
  result.reference?.startsWith('TI-') ? ok(`Reference: ${result.reference}`) : fail('Reference', result.reference);
  typeof result.quotedCreditTotal === 'number' ? ok(`Quoted credit: £${result.quotedCreditTotal}`) : fail('Credit total', result.quotedCreditTotal);
  typeof result.quotedCashTotal === 'number' ? ok(`Quoted cash: £${result.quotedCashTotal}`) : fail('Cash total', result.quotedCashTotal);
  result.quoteExpiresAt ? ok(`Expires: ${result.quoteExpiresAt.slice(0, 10)}`) : fail('Expiry', 'missing');
  result.items?.length === testItems.length ? ok(`${result.items.length} items confirmed`) : fail('Item count', result.items?.length);

  // ── 5. Status lookup ──
  console.log('\n── Status Lookup ──');
  const statusResp = await get(API + '/tradein/' + result.reference + '?email=e2e-test%40cambridgetcg.com');
  statusResp.ok ? ok('Status lookup OK') : fail('Status lookup', statusResp.status);

  const statusData = JSON.parse(statusResp.body);
  statusData.status === 'submitted' ? ok('Status: submitted') : fail('Status', statusData.status);
  statusData.items?.length > 0 ? ok(`Status shows ${statusData.items.length} items`) : fail('Status items', 'empty');

  // Wrong email should 404 (not leak data)
  const wrongEmail = await get(API + '/tradein/' + result.reference + '?email=wrong%40email.com');
  wrongEmail.status === 404 ? ok('Wrong email returns 404 (no data leak)') : fail('Wrong email', wrongEmail.status);

  // ── 6. Honeypot ──
  console.log('\n── Honeypot ──');
  const botData = { ...submitData, website: 'spam-bot-url.com', customerName: 'Bot Test' };
  const botResp = await post(API + '/tradein', botData);
  botResp.status === 400 ? ok('Honeypot rejects bot submission') : fail('Honeypot', botResp.status);

  // ── Summary ──
  console.log('\n══════════════════════════════════════');
  console.log(`  ${passed} passed, ${failed} failed`);
  if (failed > 0) {
    console.log('  ⚠️  Some tests failed!');
  } else {
    console.log('  ✅ All tests passed!');
  }
  console.log('══════════════════════════════════════\n');
  process.exit(failed > 0 ? 1 : 0);
}

run().catch(e => { console.error('Fatal:', e.message); process.exit(1); });
