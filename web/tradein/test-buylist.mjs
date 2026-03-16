/**
 * Playwright smoke test for tradein.cambridgetcg.com
 *
 * Run: npx playwright test web/tradein/test-buylist.mjs --headed
 *  or: node web/tradein/test-buylist.mjs  (standalone)
 */
import { chromium } from 'playwright';

const URL = 'https://tradein.cambridgetcg.com';
const results = [];
let page, browser;
let submittedRef = null; // captured after successful submission

function ok(name) { results.push({ name, pass: true }); console.log(`  ✓ ${name}`); }
function fail(name, err) { results.push({ name, pass: false, err }); console.error(`  ✗ ${name}: ${err}`); }

async function run() {
  browser = await chromium.launch({ headless: true });
  const ctx = await browser.newContext();
  page = await ctx.newPage();

  // Collect console errors
  const consoleErrors = [];
  page.on('console', msg => { if (msg.type() === 'error') consoleErrors.push(msg.text()); });
  page.on('pageerror', err => consoleErrors.push(err.message));

  // Intercept API call
  let apiResponse = null;
  page.on('response', resp => {
    if (resp.url().includes('/buylist')) apiResponse = resp;
  });

  console.log(`\nLoading ${URL} ...\n`);
  console.log('── Page Load ──');

  try {
    const resp = await page.goto(URL, { waitUntil: 'networkidle', timeout: 15000 });
    resp.status() === 200 ? ok('Page loads (HTTP 200)') : fail('Page loads', `HTTP ${resp.status()}`);
  } catch (e) {
    fail('Page loads', e.message);
    await cleanup(); return;
  }

  // Check API call succeeded
  if (apiResponse) {
    apiResponse.status() === 200 ? ok('API /buylist returns 200') : fail('API /buylist', `HTTP ${apiResponse.status()}`);
  } else {
    fail('API /buylist', 'No API call intercepted');
  }

  // Check no JS errors (ignore Cloudflare beacon + network resource errors from proxy-injected scripts)
  const realErrors = consoleErrors.filter(e =>
    !e.includes('cloudflareinsights') && !e.includes('net::ERR_NAME_NOT_RESOLVED')
  );
  if (realErrors.length === 0) {
    ok('No console errors');
  } else {
    fail('Console errors', realErrors.join('; '));
  }

  // Check error message is NOT shown
  const errorMsg = await page.$('.error-msg');
  errorMsg === null ? ok('No error message displayed') : fail('Error message shown', await errorMsg.textContent());

  console.log('\n── Content ──');

  // Check table exists with rows
  const rows = await page.$$('tbody tr');
  rows.length > 0 ? ok(`Table has ${rows.length} rows`) : fail('Table rows', '0 rows');

  // Check column headers
  const headers = await page.$$eval('thead th', ths => ths.map(t => t.textContent.trim()));
  headers.some(h => h.includes('Cash') && !h.includes('Want')) ? ok('Cash price column header present') : fail('Cash column', `Headers: ${headers}`);
  headers.some(h => h.includes('Credit') && !h.includes('Want')) ? ok('Credit price column header present') : fail('Credit column', `Headers: ${headers}`);
  headers.some(h => h.includes('Cash Want')) ? ok('Cash Want column header present') : fail('Cash Want column', `Headers: ${headers}`);
  headers.some(h => h.includes('Credit Want')) ? ok('Credit Want column header present') : fail('Credit Want column', `Headers: ${headers}`);
  headers.some(h => h.includes('Add')) ? ok('Add column header present') : fail('Add column', `Headers: ${headers}`);

  // Check price values render correctly (£X.XX format)
  const firstRowCells = await page.$$eval('tbody tr:first-child td', tds => tds.map(t => t.textContent.trim()));
  console.log(`  First row: ${JSON.stringify(firstRowCells)}`);
  const hasCashPrice = firstRowCells.some(c => /^£\d+\.\d{2}$/.test(c));
  const hasCreditPrice = firstRowCells.some(c => /^£\d+\.\d{2}$/.test(c));
  hasCashPrice ? ok('Cash price renders (£X.XX)') : fail('Cash price format', firstRowCells);
  hasCreditPrice ? ok('Credit price renders (£X.XX)') : fail('Credit price format', firstRowCells);

  // Check credit price > cash price for first row
  const prices = firstRowCells.filter(c => /^£\d+\.\d{2}$/.test(c)).map(c => parseFloat(c.slice(1)));
  if (prices.length >= 2 && prices[1] > prices[0]) {
    ok(`Credit (£${prices[1]}) > Cash (£${prices[0]})`);
  } else {
    fail('Credit > Cash', `Prices: ${prices}`);
  }

  // Check summary cards (Total Cards + Buying for Cash)
  const summaryValues = await page.$$eval('.summary-card__value', els => els.map(e => e.textContent));
  summaryValues.length >= 2 ? ok(`Summary cards present (${summaryValues.join(', ')})`) : fail('Summary cards', `Found ${summaryValues.length}`);

  console.log('\n── Set Pills ──');

  // Check set pills exist
  const pills = await page.$$('.pill');
  pills.length > 1 ? ok(`${pills.length} set pills`) : fail('Set pills', `Found ${pills.length}`);

  // Click a set pill and verify filter
  const op01Pill = await page.$('a.pill:text("OP01")');
  if (op01Pill) {
    await op01Pill.click();
    await page.waitForTimeout(300);
    const filteredRows = await page.$$('tbody tr');
    const hash = page.url().split('#')[1] || '';
    hash.includes('set/OP01') ? ok('Set pill navigates to #/set/OP01') : fail('Set pill nav', hash);
    filteredRows.length < rows.length ? ok(`Filter reduces rows (${filteredRows.length} < ${rows.length})`) : fail('Filter', `${filteredRows.length} vs ${rows.length}`);

    // Go back
    await page.goto(URL, { waitUntil: 'networkidle', timeout: 10000 });
  } else {
    fail('OP01 pill', 'Not found');
  }

  console.log('\n── Search ──');

  // Test search
  const searchInput = await page.$('#search-input');
  if (searchInput) {
    await searchInput.fill('OP09');
    await page.waitForTimeout(300);
    const visibleRows = await page.$$eval('tbody tr', trs => trs.filter(tr => tr.style.display !== 'none').length);
    visibleRows < rows.length ? ok(`Search filters (${visibleRows} visible)`) : fail('Search filter', `${visibleRows} still visible`);
    await searchInput.fill('');
    await page.waitForTimeout(100);
  } else {
    fail('Search input', 'Not found');
  }

  console.log('\n── Sort ──');

  // Test sort by credit price
  const creditHeader = await page.$('th[data-sort="credit"]');
  if (creditHeader) {
    await creditHeader.click();
    await page.waitForTimeout(300);
    const sortedPrices = await page.$$eval('tbody tr td:nth-child(4)', tds =>
      tds.slice(0, 5).map(t => parseFloat(t.textContent.replace('£', '')))
    );
    console.log(`  First 5 credit prices after sort: ${sortedPrices}`);
    const isDescending = sortedPrices.every((v, i, a) => i === 0 || v <= a[i - 1]);
    isDescending ? ok('Sort by credit descending (first click)') : fail('Sort credit desc', sortedPrices);
  } else {
    fail('Credit sort header', 'Not found');
  }

  // Test sort by cash want
  const cashWantHeader = await page.$('th[data-sort="cash_want"]');
  if (cashWantHeader) {
    await cashWantHeader.click();
    await page.waitForTimeout(300);
    const sortedWants = await page.$$eval('tbody tr', trs =>
      trs.slice(0, 5).map(tr => parseInt(tr.getAttribute('data-cash-want')))
    );
    console.log(`  First 5 cash wants after sort: ${sortedWants}`);
    const isDesc = sortedWants.every((v, i, a) => i === 0 || v <= a[i - 1]);
    isDesc ? ok('Sort by cash want descending (first click)') : fail('Sort cash want desc', sortedWants);
  } else {
    fail('Cash want sort header', 'Not found');
  }

  console.log('\n── Buying Only Toggle ──');

  // Test "Show buying only" toggle (filters on cash want)
  const toggle = await page.$('#buying-only');
  if (toggle) {
    await toggle.click();
    await page.waitForTimeout(300);
    const cashWantZeroVisible = await page.$$eval('tbody tr[data-cash-want="0"]', trs =>
      trs.filter(tr => tr.style.display !== 'none').length
    );
    cashWantZeroVisible === 0 ? ok('Toggle hides cash-want=0 rows') : fail('Toggle', `${cashWantZeroVisible} cash-want=0 rows still visible`);
    await toggle.click();
    await page.waitForTimeout(100);
  } else {
    fail('Buying-only toggle', 'Not found');
  }

  console.log('\n── Want Badges ──');

  // Check cash want badge colors
  const wantHigh = await page.$('.want-high');
  const wantMid = await page.$('.want-mid');
  const wantNone = await page.$('.want-none');
  wantHigh ? ok('want-high badge exists (green)') : fail('want-high', 'Not found');
  wantMid ? ok('want-mid badge exists (amber)') : fail('want-mid', 'Not found');
  wantNone ? ok('want-none badge exists (gray)') : fail('want-none', 'Not found');

  // Check credit want unlimited badges
  const wantUnlimited = await page.$('.want-unlimited');
  wantUnlimited ? ok('want-unlimited badge exists (blue ∞)') : fail('want-unlimited', 'Not found');
  const unlimitedText = await page.$eval('.want-unlimited', el => el.textContent);
  unlimitedText === '∞' ? ok('Credit want shows ∞ symbol') : fail('Credit want text', unlimitedText);

  console.log('\n── Add to Cart ──');

  // Clear any existing cart
  await page.evaluate(() => localStorage.removeItem('ctcg_tradein_cart'));

  // Reload to clear cart badge state
  await page.goto(URL, { waitUntil: 'networkidle', timeout: 10000 });

  // Find an add button and click it
  const addBtn = await page.$('.btn-add-cart');
  if (addBtn) {
    const sku = await addBtn.getAttribute('data-sku');
    await addBtn.click();
    await page.waitForTimeout(200);

    // Button should update to show "1 in cart"
    const btnText = await addBtn.textContent();
    btnText.includes('1 in cart') ? ok(`Add button updates ("${btnText}")`) : fail('Add button text', btnText);

    // Button should have 'in-cart' class
    const hasClass = await addBtn.evaluate(el => el.classList.contains('in-cart'));
    hasClass ? ok('Add button has in-cart class') : fail('Add button in-cart class', 'Missing');

    // Cart badge should show 1
    const badge = await page.$('#cart-badge');
    if (badge) {
      const badgeText = await badge.textContent();
      const badgeVisible = await badge.evaluate(el => el.style.display !== 'none');
      badgeText === '1' && badgeVisible ? ok('Cart badge shows 1') : fail('Cart badge', `text="${badgeText}" visible=${badgeVisible}`);
    } else {
      fail('Cart badge', 'Not found');
    }

    // Cart bar should appear
    const cartBar = await page.$('#cart-bar');
    if (cartBar) {
      const barVisible = await cartBar.evaluate(el => el.style.display !== 'none');
      barVisible ? ok('Cart bar appears after add') : fail('Cart bar', 'Not visible');
    } else {
      fail('Cart bar', 'Not found');
    }

    // Click add again to increment
    await addBtn.click();
    await page.waitForTimeout(200);
    const btnText2 = await addBtn.textContent();
    btnText2.includes('2 in cart') ? ok(`Second click increments ("${btnText2}")`) : fail('Second add click', btnText2);

    console.log('\n── Cart Page ──');

    // Navigate to cart
    await page.click('a[href="#/cart"]');
    await page.waitForTimeout(500);

    const cartTitle = await page.$('.page-title');
    const titleText = cartTitle ? await cartTitle.textContent() : '';
    titleText.includes('Cart') ? ok('Cart page loads') : fail('Cart page title', titleText);

    // Check cart table has items
    const cartRows = await page.$$('.cart-table tbody tr');
    cartRows.length > 0 ? ok(`Cart table has ${cartRows.length} item(s)`) : fail('Cart table', '0 rows');

    // Check qty controls
    const qtyControl = await page.$('.qty-control');
    qtyControl ? ok('Qty +/- controls present') : fail('Qty controls', 'Not found');

    // Check totals row
    const totalsRow = await page.$('.cart-totals');
    totalsRow ? ok('Cart totals row present') : fail('Cart totals', 'Not found');

    // Check "Proceed to Submit" button
    const submitLink = await page.$('a[href="#/submit"]');
    submitLink ? ok('Proceed to Submit button present') : fail('Submit button', 'Not found');

    // Test remove button
    const removeBtn = await page.$('.btn-remove');
    if (removeBtn) {
      await removeBtn.click();
      await page.waitForTimeout(300);
      // Cart should be empty now
      const emptyState = await page.$('.empty-state');
      emptyState ? ok('Remove button empties cart') : fail('Remove button', 'Cart not empty');
    } else {
      fail('Remove button', 'Not found');
    }

    console.log('\n── Submit Form ──');

    // Re-add items to cart for form test (add 3+ cards to reach £5 minimum)
    await page.goto(URL, { waitUntil: 'networkidle', timeout: 10000 });

    // Sort by credit descending to get expensive cards first
    await page.click('th[data-sort="credit"]');
    await page.waitForTimeout(300);

    // Add first 5 cards to cart
    const addBtns = await page.$$('.btn-add-cart');
    for (let i = 0; i < Math.min(5, addBtns.length); i++) {
      await addBtns[i].click();
      await page.waitForTimeout(100);
    }

    // Navigate to cart, then submit form
    await page.click('a[href="#/cart"]');
    await page.waitForTimeout(500);

    // Click proceed
    const proceedBtn = await page.$('a[href="#/submit"]');
    if (proceedBtn) {
      await proceedBtn.click();
      await page.waitForTimeout(500);

      // Check form elements exist
      const nameInput = await page.$('#ti-name');
      const emailInput = await page.$('#ti-email');
      const phoneInput = await page.$('#ti-phone');
      const paymentRadios = await page.$$('input[name="payment"]');
      const deliveryRadios = await page.$$('input[name="delivery"]');
      const conditionCheck = await page.$('#ti-condition');
      const ageCheck = await page.$('#ti-age');
      const notesArea = await page.$('#ti-notes');
      const submitBtn = await page.$('#submit-btn');

      nameInput ? ok('Name input present') : fail('Name input', 'Not found');
      emailInput ? ok('Email input present') : fail('Email input', 'Not found');
      paymentRadios.length === 2 ? ok('Payment radio buttons (credit/cash)') : fail('Payment radios', `Found ${paymentRadios.length}`);
      deliveryRadios.length === 2 ? ok('Delivery radio buttons (mail/instore)') : fail('Delivery radios', `Found ${deliveryRadios.length}`);
      conditionCheck ? ok('Condition checkbox present') : fail('Condition checkbox', 'Not found');
      ageCheck ? ok('Age declaration checkbox present') : fail('Age checkbox', 'Not found');
      submitBtn ? ok('Submit button present') : fail('Submit button', 'Not found');

      // Check credit is pre-selected
      const creditChecked = await page.$eval('input[name="payment"][value="credit"]', el => el.checked);
      creditChecked ? ok('Credit payment pre-selected') : fail('Credit pre-selected', 'Not checked');

      // Check honeypot exists but hidden
      const honeypot = await page.$('#ti-website');
      if (honeypot) {
        const hpParent = await honeypot.evaluate(el => getComputedStyle(el.closest('div')).display);
        hpParent === 'none' ? ok('Honeypot field hidden') : fail('Honeypot', `display=${hpParent}`);
      } else {
        fail('Honeypot', 'Not found');
      }

      // Fill and submit the form (real e2e test)
      await nameInput.fill('Playwright Test');
      await emailInput.fill('test@playwright.dev');
      await conditionCheck.check();
      await ageCheck.check();

      // Intercept the POST
      let postResponse = null;
      page.on('response', resp => {
        if (resp.url().includes('/tradein') && resp.request().method() === 'POST') {
          postResponse = resp;
        }
      });

      await submitBtn.click();
      await page.waitForTimeout(3000);

      console.log('\n── Submission + Confirmation ──');

      if (postResponse) {
        const status = postResponse.status();
        status === 200 ? ok(`POST /tradein returns ${status}`) : fail('POST /tradein', `HTTP ${status}`);
      } else {
        fail('POST /tradein', 'No response intercepted');
      }

      // Check confirmation page loaded
      const currentHash = page.url().split('#')[1] || '';
      currentHash.startsWith('/confirm/TI-') ? ok(`Redirected to confirmation (${currentHash})`) : fail('Confirm redirect', currentHash);

      // Capture ref for status page test
      if (currentHash.startsWith('/confirm/TI-')) {
        submittedRef = currentHash.replace('/confirm/', '');
      }

      // Check confirmation elements
      const confirmIcon = await page.$('.confirm-icon');
      confirmIcon ? ok('Confirmation icon shown') : fail('Confirm icon', 'Not found');

      const confirmRef = await page.$('.confirm-value');
      if (confirmRef) {
        const refText = await confirmRef.textContent();
        refText.startsWith('TI-') ? ok(`Reference shown (${refText})`) : fail('Reference format', refText);
      } else {
        fail('Confirm reference', 'Not found');
      }

      // Check items table on confirmation
      const confirmItems = await page.$$('.confirm-page .catalog-table tbody tr');
      confirmItems.length > 0 ? ok(`Confirmation shows ${confirmItems.length} items`) : fail('Confirm items', '0 items');

      // Check instructions shown
      const instructions = await page.$('.confirm-instructions');
      instructions ? ok('Delivery instructions shown') : fail('Delivery instructions', 'Not found');

    } else {
      fail('Proceed button', 'Not found — cart total may be under £5');
    }

  } else {
    fail('Add to cart button', 'Not found');
  }

  console.log('\n── Status Page ──');

  await page.goto(URL + '#/status', { waitUntil: 'networkidle', timeout: 10000 });
  await page.waitForTimeout(500);

  // Check status form renders
  const statusRefInput = await page.$('#status-ref');
  const statusEmailInput = await page.$('#status-email');
  const statusBtn = await page.$('#status-btn');
  statusRefInput ? ok('Status reference input present') : fail('Status ref input', 'Not found');
  statusEmailInput ? ok('Status email input present') : fail('Status email input', 'Not found');
  statusBtn ? ok('Status check button present') : fail('Status button', 'Not found');

  // Check page title
  const statusTitle = await page.$('.page-title');
  const statusTitleText = statusTitle ? await statusTitle.textContent() : '';
  statusTitleText.includes('Status') ? ok('Status page title correct') : fail('Status title', statusTitleText);

  // Test with invalid ref — should show error
  if (statusRefInput && statusEmailInput && statusBtn) {
    await statusRefInput.fill('TI-00000000-ZZZZ');
    await statusEmailInput.fill('nobody@example.com');
    await statusBtn.click();
    await page.waitForTimeout(2000);
    const statusErrorMsg = await page.$('#status-error');
    if (statusErrorMsg) {
      const errorVisible = await statusErrorMsg.evaluate(el => el.style.display !== 'none');
      const errorText = await statusErrorMsg.textContent();
      errorVisible ? ok('Invalid ref shows error: "' + errorText.slice(0, 50) + '"') : fail('Status error', 'Not visible');
    } else {
      fail('Status error div', 'Not found');
    }

    // Test with real ref from earlier submission
    if (submittedRef) {
      // Clear previous error
      await statusRefInput.fill(submittedRef);
      await statusEmailInput.fill('test@playwright.dev');
      await statusBtn.click();
      await page.waitForTimeout(3000);
      const statusResult = await page.$('#status-result');
      if (statusResult) {
        const resultVisible = await statusResult.evaluate(el => el.style.display !== 'none');
        resultVisible ? ok('Real ref status lookup succeeds') : fail('Real ref lookup', 'Result not visible');

        // Check status badge
        const badge = await page.$('.status-badge');
        badge ? ok('Status badge displayed') : fail('Status badge', 'Not found');

        // Check items table
        const statusItems = await page.$$('.status-result .catalog-table tbody tr');
        statusItems.length > 0 ? ok(`Status shows ${statusItems.length} items`) : fail('Status items', '0 items');
      } else {
        fail('Status result div', 'Not found');
      }
    } else {
      console.log('  (skipping real ref lookup — no submission ref captured)');
    }
  }

  console.log('\n── Terms Page ──');

  await page.goto(URL + '#/terms', { waitUntil: 'networkidle', timeout: 10000 });
  await page.waitForTimeout(500);

  const termsContent = await page.$('.terms-content');
  termsContent ? ok('Terms page loads') : fail('Terms page', 'Not found');

  const termsSections = await page.$$('.terms-content h3');
  termsSections.length >= 5 ? ok(`Terms has ${termsSections.length} sections`) : fail('Terms sections', `Found ${termsSections.length}`);

  console.log('\n── Mobile ──');

  // Reset to buy list
  await page.goto(URL, { waitUntil: 'networkidle', timeout: 10000 });
  await page.waitForTimeout(300);

  // Test mobile viewport
  await page.setViewportSize({ width: 375, height: 667 });
  await page.waitForTimeout(300);
  const setColVisible = await page.$eval('.col-set', el => getComputedStyle(el).display !== 'none');
  !setColVisible ? ok('Set column hidden on mobile') : fail('Mobile set column', 'Still visible');
  const creditWantColVisible = await page.$eval('.col-credit-want', el => getComputedStyle(el).display !== 'none');
  !creditWantColVisible ? ok('Credit want column hidden on mobile') : fail('Mobile credit want column', 'Still visible');

  // Take screenshot
  await page.screenshot({ path: '/tmp/tradein-mobile.png', fullPage: true });
  ok('Mobile screenshot saved to /tmp/tradein-mobile.png');

  // Reset
  await page.setViewportSize({ width: 1280, height: 720 });
  await page.screenshot({ path: '/tmp/tradein-desktop.png', fullPage: true });
  ok('Desktop screenshot saved to /tmp/tradein-desktop.png');

  await cleanup();
}

async function cleanup() {
  if (browser) await browser.close();

  console.log('\n══════════════════════════════════════');
  const passed = results.filter(r => r.pass).length;
  const failed = results.filter(r => !r.pass).length;
  console.log(`  ${passed} passed, ${failed} failed (${results.length} total)`);
  if (failed > 0) {
    console.log('\n  Failures:');
    results.filter(r => !r.pass).forEach(r => console.log(`    ✗ ${r.name}: ${r.err}`));
  }
  console.log('══════════════════════════════════════\n');
  process.exit(failed > 0 ? 1 : 0);
}

run().catch(err => { console.error('Fatal:', err); process.exit(1); });
