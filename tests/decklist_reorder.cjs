/* Drag-to-reorder on the deck list: dropping a deck on the top or bottom
   edge of a SIBLING resorts the group (ba:deck:reorder), the middle of a row
   still nests (ba:deck:reparent), and edges on non-siblings fall back to
   nesting. Run with NODE_PATH pointing to a Playwright installation. */
const { chromium } = require('playwright');
const assert = require('node:assert/strict');
const path = require('node:path');
const root = path.resolve(__dirname, '..');

const DECKS = [
  { did: 1, name: 'Amino Acids', depth: 0 },
  { did: 2, name: 'Exam 1', depth: 0 },
  { did: 3, name: 'MCAT', depth: 0 },
  { did: 5, name: 'Bio', depth: 1 },
  { did: 6, name: 'Chem', depth: 1 },
  { did: 4, name: 'Microbiology', depth: 0 },
];

(async () => {
  const browser = await chromium.launch({ headless: true });
  try {
    const page = await browser.newPage({ viewport: { width: 1000, height: 700 } });
    const errors = []; page.on('pageerror', e => errors.push(e.message));
    await page.setContent('<html><body><div class="ad-list" id="list"></div></body></html>');
    await page.addStyleTag({ path: path.join(root, 'web/decklist.css') });
    await page.addScriptTag({ path: path.join(root, 'web/decklist.js') });
    await page.evaluate(decks => {
      window.sent = [];
      window.pycmd = cmd => { window.sent.push(cmd); };
      window.__adDeckList.render(document.getElementById('list'), decks, {});
    }, DECKS);

    const sent = () => page.evaluate(() => window.sent.splice(0));
    const row = did => `.ad-list-row[data-did="${did}"]`;

    // Drive the HTML5 drag events by hand so the pointer position is exact:
    // `frac` is where in the target row the pointer sits (0 = top edge).
    async function drop(srcDid, targetDid, frac) {
      const dt = await page.evaluateHandle(() => new DataTransfer());
      await page.dispatchEvent(row(srcDid), 'dragstart', { dataTransfer: dt });
      const box = await page.locator(row(targetDid)).boundingBox();
      const clientY = box.y + box.height * frac;
      const clientX = box.x + box.width / 2;
      await page.dispatchEvent(row(targetDid), 'dragover', { dataTransfer: dt, clientX, clientY });
      const marks = await page.evaluate(d => {
        const r = document.querySelector(`.ad-list-row[data-did="${d}"]`);
        return ['drop', 'before', 'after'].filter(k => r.classList.contains('ad-list-row--' + k));
      }, targetDid);
      await page.dispatchEvent(row(targetDid), 'drop', { dataTransfer: dt, clientX, clientY });
      await page.dispatchEvent(row(srcDid), 'dragend', { dataTransfer: dt });
      return { marks, sent: await sent() };
    }

    // Top edge of the first sibling: the deck goes to the top.
    let r = await drop(4, 1, 0.1);
    assert.deepEqual(r.marks, ['before'], 'top edge should draw the insertion line above');
    assert.deepEqual(r.sent, ['ba:deck:reorder:4:1:before']);

    // Bottom edge of the last sibling: the deck goes to the bottom.
    r = await drop(1, 4, 0.9);
    assert.deepEqual(r.marks, ['after'], 'bottom edge should draw the insertion line below');
    assert.deepEqual(r.sent, ['ba:deck:reorder:1:4:after']);

    // Middle of a row still nests, exactly as before.
    r = await drop(1, 2, 0.5);
    assert.deepEqual(r.marks, ['drop']);
    assert.deepEqual(r.sent, ['ba:deck:reparent:1:2']);

    // Nested siblings reorder among themselves.
    r = await drop(6, 5, 0.1);
    assert.deepEqual(r.sent, ['ba:deck:reorder:6:5:before']);

    // An edge on a non-sibling (different parent) is a plain nest.
    r = await drop(5, 1, 0.1);
    assert.deepEqual(r.marks, ['drop']);
    assert.deepEqual(r.sent, ['ba:deck:reparent:5:1']);

    // Edges of the deck's own parent stay blocked (it is already there).
    r = await drop(5, 3, 0.1);
    assert.deepEqual(r.marks, []);
    assert.deepEqual(r.sent, []);

    // Nothing lingers after a drop.
    const leftovers = await page.evaluate(() =>
      document.querySelectorAll('.ad-list-row--before, .ad-list-row--after, .ad-list-row--drop, .ad-list-row--dragging').length);
    assert.equal(leftovers, 0);

    assert.deepEqual(errors, []);
    console.log('PASS decklist: edge drops reorder siblings, middle drops still nest');
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
