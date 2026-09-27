/* Click-to-reveal must not fire for clicks that operate a control inside the
   card, e.g. opening the collapsible "Scenario" <details> on the question
   side. Run with NODE_PATH pointing to a Playwright installation. */
const { chromium } = require('playwright');
const assert = require('node:assert/strict');
const path = require('node:path');
const root = path.resolve(__dirname, '..');

(async () => {
  const browser = await chromium.launch({ headless: true });
  try {
    const page = await browser.newPage({ viewport: { width: 1000, height: 700 } });
    const errors = []; page.on('pageerror', e => errors.push(e.message));
    await page.setContent(`<html><body>
      <div id="qa"><div class="card">
        <details class="scenario"><summary>Scenario</summary>
          <p id="scenario-text">HSPB5 is a small heat shock protein that keeps lens proteins folded.</p>
        </details>
        <p id="question">What type of amino acids do you predict HSPB5 uses to bind to unfolded proteins? Why?</p>
      </div></div></body></html>`);
    await page.evaluate(() => {
      window.__baOpts = { reviewer: { clickToReveal: true } };
      window.answers = 0;
      window.pycmd = cmd => { if (cmd === 'ans') window.answers++; };
    });
    await page.addScriptTag({ path: path.join(root, 'web/reviewer.js') });

    await page.locator('summary').click();
    assert.equal(await page.evaluate(() => document.querySelector('details').open), true, 'details did not open');
    assert.equal(await page.evaluate(() => window.answers), 0, 'opening the Scenario revealed the answer');

    await page.locator('#scenario-text').click();
    assert.equal(await page.evaluate(() => window.answers), 0, 'clicking inside the open Scenario revealed the answer');

    await page.locator('#question').click();
    assert.equal(await page.evaluate(() => window.answers), 1, 'clicking the question text should still reveal');

    assert.deepEqual(errors, []);
    console.log('PASS reviewer: Scenario toggle does not reveal the answer; plain card click still does');
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
