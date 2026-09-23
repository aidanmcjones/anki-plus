/* Deck list reorder driven by a REAL mouse drag in Chromium (page.mouse:
   press, move past the drag threshold, move, release), on the home page
   markup homedeck.js builds. The older decklist_reorder.cjs dispatches
   synthetic drag events, so it never saw that Chromium (and QtWebEngine)
   aborts a drag whose dragstart inserts the "top level" drop zone above
   the rows: every nested deck (e.g. the sub-decks of "Exam 1") fired
   dragend immediately and could not be dropped anywhere.
   Run with NODE_PATH pointing to a Playwright installation. */
const { chromium } = require('playwright');
const assert = require('node:assert/strict');
const path = require('node:path');
const web = path.resolve(__dirname, '..', 'web');

const DECKS = [
  { did: 10, name: 'Fundamentals of Biochemistry', depth: 0, new: 107, review: 19 },
  { did: 11, name: 'Amino Acids', depth: 1 },
  { did: 12, name: 'Exam 1', depth: 1, new: 107 },
  { did: 13, name: '00 Learning Goals', depth: 2 },
  { did: 14, name: '01 pKa & Drug Absorption', depth: 2 },
  { did: 15, name: '02 Peptides', depth: 2 },
  { did: 20, name: 'MCAT', depth: 0 },
  { did: 30, name: 'Microbiology', depth: 0 },
];

function withTimeout(p, ms, what) {
  return Promise.race([p, new Promise((_, rej) =>
    setTimeout(() => rej(new Error('timed out: ' + what)), ms))]);
}

(async () => {
  const browser = await chromium.launch({ headless: true });
  let failed = false;
  try {
    const page = await browser.newPage({ viewport: { width: 1100, height: 800 } });
    const errors = []; page.on('pageerror', e => errors.push(e.message));
    await page.setContent('<html><head><script>'
      + 'window.sent=[];window.events=[];window.pycmd=function(c){window.sent.push(c)};'
      + 'window.__baOpts={deckList:{dragMove:true}};'
      + 'window.__baDeckTree=' + JSON.stringify(DECKS) + ';'
      + '</script></head><body><center class="ba-home ba-multi"></center></body></html>');
    for (const f of ['tokens.css', 'theme.css', 'decklist.css']) {
      await page.addStyleTag({ path: path.join(web, f) });
    }
    for (const f of ['decklist.js', 'homedeck.js']) {
      await page.addScriptTag({ path: path.join(web, f) });
    }
    await page.evaluate(() => ['dragstart', 'dragend', 'drop'].forEach(t =>
      document.addEventListener(t, () => window.events.push(t), true)));
    assert.equal(await page.locator('.ad-list--home .ad-list-row').count(), DECKS.length);

    const row = did => `.ad-list--home .ad-list-row[data-did="${did}"]`;

    async function drag(src, target, frac) {
      await page.evaluate(() => { window.sent = []; window.events = []; });
      const s = await page.locator(row(src) + ' .ad-list-name').boundingBox();
      await page.mouse.move(s.x + 12, s.y + s.height / 2);
      await page.mouse.down();
      await withTimeout(page.mouse.move(s.x + 24, s.y + s.height / 2 + 6, { steps: 4 }),
        5000, 'start drag of ' + src);
      await page.waitForTimeout(50);
      const alive = await page.evaluate(() => window.events.slice());
      // The target is measured AFTER the drag started: the top-level zone
      // may have pushed the rows down by then.
      const t = await page.locator(row(target)).boundingBox();
      await withTimeout(page.mouse.move(t.x + t.width / 2, t.y + t.height * frac, { steps: 8 }),
        5000, 'move to ' + target);
      await withTimeout(page.mouse.up(), 5000, 'release');
      await page.waitForTimeout(50);
      return { alive, events: await page.evaluate(() => window.events.slice()),
               sent: await page.evaluate(() => window.sent.slice()) };
    }

    // A nested deck (the user's case: sub-decks of "Exam 1").
    let r = await drag(15, 13, 0.1);
    assert.deepEqual(r.alive, ['dragstart'],
      'dragging a nested deck must not end the drag straight away: ' + r.alive);
    assert.deepEqual(r.sent, ['ba:deck:reorder:15:13:before']);
    console.log('ok  nested deck, real mouse drag -> ' + r.sent[0]);

    r = await drag(11, 12, 0.9);
    assert.deepEqual(r.alive, ['dragstart']);
    assert.deepEqual(r.sent, ['ba:deck:reorder:11:12:after']);
    console.log('ok  depth-1 deck below its sibling -> ' + r.sent[0]);

    r = await drag(30, 20, 0.1);
    assert.deepEqual(r.sent, ['ba:deck:reorder:30:20:before']);
    console.log('ok  top-level deck -> ' + r.sent[0]);

    // The middle of a row still nests.
    r = await drag(20, 30, 0.5);
    assert.deepEqual(r.sent, ['ba:deck:reparent:20:30']);
    console.log('ok  middle drop still nests -> ' + r.sent[0]);

    // A nested deck can still reach the top-level zone once it shows.
    r = await (async () => {
      await page.evaluate(() => { window.sent = []; });
      const s = await page.locator(row(14) + ' .ad-list-name').boundingBox();
      await page.mouse.move(s.x + 12, s.y + s.height / 2);
      await page.mouse.down();
      await withTimeout(page.mouse.move(s.x + 24, s.y + s.height / 2 + 6, { steps: 4 }), 5000, 'start');
      await page.waitForTimeout(50);
      const z = await page.locator('.ad-list-dropzone--show').boundingBox();
      assert.ok(z, 'top-level drop zone should show during a nested drag');
      await withTimeout(page.mouse.move(z.x + z.width / 2, z.y + z.height / 2, { steps: 8 }), 5000, 'zone');
      await withTimeout(page.mouse.up(), 5000, 'release');
      await page.waitForTimeout(50);
      return { sent: await page.evaluate(() => window.sent.slice()) };
    })();
    assert.deepEqual(r.sent, ['ba:deck:reparent:14:0']);
    console.log('ok  nested deck to the top-level zone -> ' + r.sent[0]);

    assert.deepEqual(errors, []);
    console.log('PASS decklist_mouse_drag');
  } catch (e) {
    failed = true;
    console.error('FAIL decklist_mouse_drag:', e.message);
  } finally {
    await browser.close();
  }
  process.exit(failed ? 1 : 0);
})();
