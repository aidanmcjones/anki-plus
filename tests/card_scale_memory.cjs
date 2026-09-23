/* card-scale.js over a long review session: the reviewer page must not grow.
   Ticket 20260923-153047 (window went grey after ~2.5 h of reviewing; the
   main webview's renderer died out of memory). card-scale.js rewrites the
   card's own <style> font sizes on every card, so it was the prime suspect:
   a rewrite that is not idempotent nests calc() one level deeper per card.
   This loads the reviewer stack (theme.css, reviewer.css, card-scale.js,
   reviewer.js), renders 300 cards the way Anki's _updateQA does (#qa
   innerHTML replaced, the note type <style> included), and measures after
   1, 10, 100 and 300 renders: total <style> text, CSSOM rule count and text,
   the deepest calc() nesting, the JS heap after a forced GC, and how many
   CSSOM writes each render cost. Everything must stay flat. It also checks
   that a note type whose CSS already carries the scale is not wrapped again,
   that loading the script twice installs one observer, that a new document
   in the same window (document.open) is still scaled, and that mutations
   that bring no <style> (progress bar, ease labels) cost no CSSOM work.
   Run with NODE_PATH pointing to a Playwright installation. ANKI_DESIGN_WEB
   overrides the web/ dir under test (default: this checkout's web/). */
const { chromium } = require('playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const root = path.resolve(__dirname, '..');
const web = process.env.ANKI_DESIGN_WEB || path.join(root, 'web');
const RENDERS = +(process.env.RENDERS || 300);

// FunBiochem-E1's note type CSS (the ticket deck), from the text-size fixture.
const ticketQa = fs.readFileSync(path.join(__dirname, 'fixtures/answer_size_qa.html'), 'utf8');
const deckCss = ticketQa.match(/<style>([\s\S]*?)<\/style>/)[1];

function card(i, css) {
  const front = `<div class="fb-front"><details class="scenario"><summary>Scenario</summary>Climbers ${i}</details>`
    + `What is K<sub>m</sub> when V<sub>0</sub> = V<sup>max</sup>/2? <span class=cloze-inactive>x<sub>2</sub></span></div>`;
  const back = `<hr id=answer><div class=answer>[S] = K<sub>m</sub> (${i})</div>`
    + `<table class=seq><tr><th>[S]</th><th>V<sub>0</sub></th></tr><tr><td>K<sub>m</sub></td><td>V<sup>max</sup>/2</td></tr></table>`
    + `<div class=src>slide ${i}</div>`;
  return `<style>${css}</style>` + front + (i % 2 ? back : '');
}

async function session(browser, { css = deckCss, loadTwice = false } = {}) {
  const page = await browser.newPage({ viewport: { width: 1400, height: 900 } });
  const errors = []; page.on('pageerror', e => errors.push(e.message));
  await page.setContent('<html><head></head><body class="card card1"><div id="qa"></div></body></html>');
  for (const f of ['theme.css', 'reviewer.css']) await page.addStyleTag({ path: path.join(web, f) });
  // Count every CSSOM font-size write and custom-property write the page makes.
  await page.evaluate(() => {
    window.__writes = 0;
    const orig = CSSStyleDeclaration.prototype.setProperty;
    CSSStyleDeclaration.prototype.setProperty = function (name) {
      if (name === 'font-size' || name === '--rf-deck-scale') window.__writes++;
      return orig.apply(this, arguments);
    };
  });
  const js = fs.readFileSync(path.join(web, 'card-scale.js'), 'utf8');
  await page.addScriptTag({ content: js });
  if (loadTwice) await page.addScriptTag({ content: js });
  await page.addScriptTag({ path: path.join(web, 'reviewer.js') });
  const cdp = await page.context().newCDPSession(page);
  return { page, cdp, errors };
}

async function render(s, i, css) {
  await s.page.evaluate(html => { document.getElementById('qa').innerHTML = html; }, card(i, css));
  await s.page.evaluate(() => new Promise(r => requestAnimationFrame(() => r())));
}

async function metrics(s) {
  await s.cdp.send('HeapProfiler.collectGarbage');
  const heap = (await s.cdp.send('Runtime.getHeapUsage')).usedSize;
  const m = await s.page.evaluate(() => {
    let styleText = 0, rules = 0, cssomText = 0, depth = 0;
    document.querySelectorAll('style').forEach(st => { styleText += st.textContent.length; });
    const walk = (list, card) => { for (const r of list) {
      rules++; cssomText += r.cssText.length;
      // calc() nesting of the card's own font sizes
      const fs = card && r.style ? r.style.getPropertyValue('font-size') : '';
      const d = (fs.match(/calc\(/g) || []).length; if (d > depth) depth = d;
      if (r.cssRules) walk(r.cssRules, card);
    } };
    for (const sh of document.styleSheets) {
      const card = !!(sh.ownerNode && sh.ownerNode.closest && sh.ownerNode.closest('#qa'));
      try { walk(sh.cssRules, card); } catch (_) {}
    }
    let answer = null;
    for (const r of document.querySelector('#qa style').sheet.cssRules) if (r.selectorText === '.answer') answer = r.style.fontSize;
    return { styleText, rules, cssomText, calcDepth: depth, answer, writes: window.__writes };
  });
  return { ...m, heap };
}

(async () => {
  const browser = await chromium.launch({ headless: true });
  let failed = 0;
  const check = (name, fn) => {
    try { fn(); console.log(`PASS ${name}`); } catch (e) { failed++; console.log(`FAIL ${name}\n     ${e.message}`); }
  };
  try {
    // 1. A long session on the ticket's note type.
    const s = await session(browser);
    const at = {};
    let prevWrites = 0; const perRender = [];
    for (let i = 1; i <= RENDERS; i++) {
      await render(s, i, deckCss);
      const w = await s.page.evaluate(() => window.__writes);
      perRender.push(w - prevWrites); prevWrites = w;
      if ([1, 10, 100, 300, RENDERS].includes(i)) at[i] = await metrics(s);
    }
    for (const k of Object.keys(at)) console.log(`  after ${k} renders: ${JSON.stringify(at[k])}`);
    const a = at[10], b = at[RENDERS];
    check('<style> text is flat from 10 to ' + RENDERS + ' renders', () =>
      assert.ok(Math.abs(b.styleText - a.styleText) <= a.styleText * 0.05, `${a.styleText} -> ${b.styleText}`));
    check('CSSOM rule count and text are flat', () => {
      assert.ok(Math.abs(b.rules - a.rules) <= a.rules * 0.05, `rules ${a.rules} -> ${b.rules}`);
      assert.ok(Math.abs(b.cssomText - a.cssomText) <= a.cssomText * 0.05, `cssom ${a.cssomText} -> ${b.cssomText}`);
    });
    check('font sizes are wrapped once, never nested', () => {
      assert.equal(b.calcDepth, 1, `deepest calc() nesting ${b.calcDepth}`);
      assert.equal(b.answer, 'calc(17px * var(--rf-deck-scale, 1))');
    });
    check('JS heap after GC grows less than 2 MB', () =>
      assert.ok(b.heap - a.heap < 2e6, `${a.heap} -> ${b.heap} bytes`));
    const maxWrites = Math.max(...perRender.slice(1));
    // px/pt/rem font-size rules in the note type CSS, counted by the CSS parser.
    const sizeRuleCount = await s.page.evaluate(css => {
      const sh = new CSSStyleSheet(); sh.replaceSync(css); let n = 0;
      const walk = l => { for (const r of l) { if (r.style && /^\s*[\d.]+(px|pt|rem)\s*$/i.test(r.style.getPropertyValue('font-size'))) n++; if (r.cssRules) walk(r.cssRules); } };
      walk(sh.cssRules); return n;
    }, deckCss);
    check('CSSOM writes per render are bounded by the note type\'s font-size rules', () => {
      const sizeRules = sizeRuleCount;
      assert.ok(maxWrites <= sizeRules + 1, `max ${maxWrites} writes in one render, ${sizeRules} px/pt/rem font-size rules`);
    });

    // 2. Mutations that bring no <style> cost no CSSOM work.
    const before = await s.page.evaluate(() => window.__writes);
    await s.page.evaluate(() => {
      for (let k = 0; k < 50; k++) {
        const d = document.createElement('div'); d.textContent = 'ease ' + k; document.body.appendChild(d); d.remove();
        const l = document.getElementById('reforge-progress-label'); if (l) l.textContent = k + ' / 50';
      }
    });
    await s.page.evaluate(() => new Promise(r => requestAnimationFrame(() => r())));
    const after = await s.page.evaluate(() => window.__writes);
    check('non-card mutations trigger no CSSOM writes', () => assert.equal(after - before, 0, `${after - before} writes`));
    assert.deepEqual(s.errors, [], 'page errors');
    await s.page.close();

    // 3. A note type whose CSS already carries the scale is left alone.
    const pre = '.card { font-size: calc(20px * var(--rf-deck-scale, 1)); } .answer { font-size: calc(17px * var(--rf-deck-scale, 1)); }';
    const s2 = await session(browser, { css: pre });
    for (let i = 1; i <= 20; i++) await render(s2, i, pre);
    const m2 = await metrics(s2);
    check('pre-scaled note type CSS is not wrapped again', () => {
      assert.equal(m2.calcDepth, 1, `calc depth ${m2.calcDepth}`);
      assert.equal(m2.answer, 'calc(17px * var(--rf-deck-scale, 1))');
    });
    await s2.page.close();

    // 4. Loading the script twice does the work once.
    const s3 = await session(browser, { loadTwice: true });
    const s4 = await session(browser);
    for (let i = 1; i <= 20; i++) { await render(s3, i, deckCss); await render(s4, i, deckCss); }
    const w3 = await s3.page.evaluate(() => window.__writes), w4 = await s4.page.evaluate(() => window.__writes);
    check('a second copy of the script adds no work', () => assert.equal(w3, w4, `${w3} writes loaded twice vs ${w4} once`));
    await s3.page.close(); await s4.page.close();

    // 5. document.open() keeps the window but brings a new document and
    //    <body> (Playwright's setContent; the deck preflight renders every
    //    card this way and loads the script again for each). The new body
    //    must be scaled too: the one observer moves there.
    const s5 = await session(browser);
    await render(s5, 1, deckCss);
    await s5.page.setContent('<html><head></head><body class="card card1"><div id="qa"></div></body></html>');
    for (const f of ['theme.css', 'reviewer.css']) await s5.page.addStyleTag({ path: path.join(web, f) });
    await s5.page.addScriptTag({ content: fs.readFileSync(path.join(web, 'card-scale.js'), 'utf8') });
    await render(s5, 2, deckCss);
    const m5 = await metrics(s5);
    check('a new document in the same window is scaled (document.open)', () =>
      assert.equal(m5.answer, 'calc(17px * var(--rf-deck-scale, 1))'));
    await render(s5, 3, deckCss);
    const m5b = await metrics(s5);
    check('and later cards in it are scaled by the moved observer', () =>
      assert.equal(m5b.answer, 'calc(17px * var(--rf-deck-scale, 1))'));
    await s5.page.close();
  } finally {
    await browser.close();
  }
  if (failed) { console.log(`${failed} check(s) failed`); process.exit(1); }
  console.log('all passed');
})().catch(e => { console.error(e); process.exit(1); });
