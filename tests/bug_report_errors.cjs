/* Playwright check for bugreport.py's JS error ring buffer
   (window.__baErrors), installed at the top of web/reviewer.js and
   web/editor-tools.js — see bugreport.py's `create_ticket` / SCHEMA.md's
   capture.console_errors.

   Run with NODE_PATH pointing to a Playwright installation, e.g.:
     NODE_PATH=/Users/aidanjones/.npm/_npx/e41f203b7505f1fb/node_modules \
       node tests/bug_report_errors.cjs
*/
const { chromium } = require('playwright');
const assert = require('node:assert/strict');
const path = require('node:path');
const root = path.resolve(__dirname, '..');

async function checkRingBuffer(browser, scriptPath, label) {
  const page = await browser.newPage();
  const pageErrors = [];
  page.on('pageerror', e => pageErrors.push(e.message));
  await page.setContent('<html><body></body></html>');
  await page.addScriptTag({ path: path.join(root, scriptPath) });

  // Buffer exists and starts empty; nothing broke just loading the script.
  const initial = await page.evaluate(() => window.__baErrors);
  assert.ok(Array.isArray(initial), `${label}: window.__baErrors is not an array`);
  assert.equal(initial.length, 0, `${label}: ring buffer should start empty`);
  assert.deepEqual(pageErrors, [], `${label}: script threw while loading: ${pageErrors}`);

  // A genuinely uncaught synchronous error is recorded via window.onerror.
  await page.evaluate(() => {
    setTimeout(() => { throw new Error('ba-test-thrown-error'); }, 0);
  });
  await page.waitForFunction(() => window.__baErrors.length >= 1);
  let errs = await page.evaluate(() => window.__baErrors);
  assert.equal(errs.length, 1, `${label}: thrown error was not recorded`);
  assert.ok(errs[0].message.includes('ba-test-thrown-error'),
    `${label}: recorded message missing the thrown text: ${errs[0].message}`);
  assert.ok('ts' in errs[0] && 'source' in errs[0] && 'line' in errs[0],
    `${label}: recorded entry missing {ts, message, source, line} shape: ${JSON.stringify(errs[0])}`);

  // An unhandled promise rejection is recorded too.
  await page.evaluate(() => {
    Promise.reject(new Error('ba-test-unhandled-rejection'));
  });
  await page.waitForFunction(() => window.__baErrors.length >= 2);
  errs = await page.evaluate(() => window.__baErrors);
  assert.equal(errs.length, 2, `${label}: unhandled rejection was not recorded`);
  assert.ok(errs[1].message.includes('ba-test-unhandled-rejection'),
    `${label}: recorded rejection message missing the thrown text: ${errs[1].message}`);

  // The buffer is capped at 50 — push 60 more and confirm it never grows
  // past that, keeping the most recent entries (FIFO eviction).
  await page.evaluate(() => {
    for (let i = 0; i < 60; i++) {
      window.dispatchEvent(new ErrorEvent('error', {
        message: 'ba-flood-' + i, filename: 'test.js', lineno: i,
      }));
    }
  });
  await page.waitForFunction(() => window.__baErrors.length === 50);
  const flooded = await page.evaluate(() => window.__baErrors);
  assert.equal(flooded.length, 50, `${label}: ring buffer did not cap at 50`);
  assert.equal(flooded[flooded.length - 1].message, 'ba-flood-59',
    `${label}: buffer should keep the most recent entries`);

  console.log(`PASS ${label}: __baErrors records thrown errors + unhandled rejections, caps at 50`);
  await page.close();
}

(async () => {
  const browser = await chromium.launch({ headless: true });
  try {
    await checkRingBuffer(browser, 'web/reviewer.js', 'reviewer.js');
    await checkRingBuffer(browser, 'web/editor-tools.js', 'editor-tools.js');
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
