/* Answer text must render at the same size as the question text when the
   reviewer styles card content (Settings > Reviewer > "Style card content").
   The app sets .card/#qa to the reviewer font size (28px), but a note type's
   own pixel sizes (FunBiochem-E1: `.answer { font-size: 17px }`) used to pin
   the answer at 17px under a 28px question. Covers the ticket's card, a stock
   Basic card, a stock Cloze card and the FunBiochem fill-in-the-blank front,
   and checks that native mode ("Style card content" off) is unchanged.
   Run with NODE_PATH pointing to a Playwright installation. ANKI_DESIGN_WEB
   overrides the web/ dir under test (default: this checkout's web/). */
const { chromium } = require('playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const root = path.resolve(__dirname, '..');
const web = process.env.ANKI_DESIGN_WEB || path.join(root, 'web');

// The ticket's rendered #qa (FunBiochem-E1 back side, its note type CSS embedded).
const ticketQa = fs.readFileSync(path.join(__dirname, 'fixtures/answer_size_qa.html'), 'utf8');
const deckCss = ticketQa.match(/<style>([\s\S]*?)<\/style>/)[1];
const stockCss = '.card { font-family: arial; font-size: 20px; text-align: center; color: black; background-color: white; }';
const stockClozeCss = stockCss + ' .cloze { font-weight: bold; color: blue; } .nightMode .cloze { color: lightblue; }';

const cases = [
  // split: 'hr' = text before hr#answer vs after; 'cloze' = .cloze/[...] text vs the rest.
  { name: 'ticket FunBiochem-E1 back', split: 'hr', qa: ticketQa },
  { name: 'stock Basic back', split: 'hr',
    qa: `<div id="qa"><style>${stockCss}</style>What is the powerhouse of the cell?<hr id=answer>The mitochondrion, which makes ATP.</div>` },
  { name: 'stock Cloze front', split: 'cloze',
    qa: `<div id="qa"><style>${stockClozeCss}</style>The <span class=cloze>[...]</span> is the powerhouse of the cell and makes most ATP.</div>` },
  { name: 'stock Cloze back', split: 'cloze',
    qa: `<div id="qa"><style>${stockClozeCss}</style>The <span class=cloze>mitochondrion</span> is the powerhouse of the cell and makes most ATP.<br>Extra detail</div>` },
  { name: 'FunBiochem cloze front', split: 'cloze',
    qa: `<div id="qa"><style>${deckCss}</style><div class="fb-front">Fill in the blanks about enzymes.<hr>Enzymes lower the <span class=cloze-inactive>activation energy</span> and leave <span class=cloze-inactive>Keq</span> and <span class=cloze-inactive>deltaG</span> unchanged.</div></div>` },
];

// Dominant (most characters) computed font-size of the visible text on each
// side of the split, counting ::after content such as the cloze "[...]".
function measure(split) {
  const qa = document.getElementById('qa');
  function hidden(el, self) {
    for (let e = el; e && e.nodeType === 1; e = e.parentElement) {
      const cs = getComputedStyle(e);
      if (cs.display === 'none' || cs.visibility === 'hidden' || parseFloat(cs.opacity) === 0) return true;
      if ((e !== el || self) && parseFloat(cs.fontSize) === 0) return true;
      if (e.tagName === 'DETAILS' && !e.open && !(e.querySelector('summary') && e.querySelector('summary').contains(el))) return true;
    }
    return false;
  }
  const hr = qa.querySelector('hr#answer');
  // node: the text node itself (or the element for ::after); its position, not its parent's.
  const side = node => split === 'hr'
    ? (hr.compareDocumentPosition(node) & Node.DOCUMENT_POSITION_FOLLOWING ? 'a' : 'q')
    : ((node.nodeType === 1 ? node : node.parentElement).closest('.cloze, .cloze-inactive') ? 'a' : 'q');
  const tally = { q: {}, a: {} };
  const add = (s, size, n) => { tally[s][size] = (tally[s][size] || 0) + n; };
  const tw = document.createTreeWalker(qa, NodeFilter.SHOW_TEXT);
  let n;
  while ((n = tw.nextNode())) {
    const p = n.parentElement;
    if (!p || ['STYLE', 'SCRIPT'].includes(p.tagName) || hidden(p, true)) continue;
    const t = n.textContent.replace(/\s+/g, '');
    if (t) add(side(n), parseFloat(getComputedStyle(p).fontSize), t.length);
  }
  for (const el of qa.querySelectorAll('*')) {
    const ps = getComputedStyle(el, '::after');
    const m = /^"(.*)"$/.exec(ps.content || '');
    if (!m || !m[1].trim() || parseFloat(ps.fontSize) === 0 || hidden(el, false)) continue;
    add(side(el), parseFloat(ps.fontSize), m[1].trim().length);
  }
  const dom = t => { const e = Object.entries(t).sort((x, y) => y[1] - x[1])[0]; return e ? +e[0] : null; };
  return { q: dom(tally.q), a: dom(tally.a) };
}

async function render(browser, c, mode) {
  const page = await browser.newPage({ viewport: { width: 1600, height: 900 }, reducedMotion: 'reduce' });
  const errors = []; page.on('pageerror', e => errors.push(e.message));
  const attr = mode === 'native' ? ' data-rf-cardstyle="native"' : '';
  await page.setContent(`<html${attr}><head></head><body class="card card1 nightMode"><div id="qa"></div></body></html>`);
  if (mode !== 'bare') {
    for (const f of ['theme.css', 'reviewer.css']) await page.addStyleTag({ path: path.join(web, f) });
    const js = path.join(web, 'card-scale.js');
    if (fs.existsSync(js)) await page.addScriptTag({ path: js });
  }
  // Like Anki's _updateQA: the card (its <style> included) lands in #qa after the page scripts load.
  await page.evaluate(html => {
    const t = document.createElement('div'); t.innerHTML = html;
    document.getElementById('qa').innerHTML = t.firstElementChild.innerHTML;
  }, c.qa);
  await page.evaluate(() => new Promise(r => requestAnimationFrame(() => r())));
  const r = await page.evaluate(measure, c.split);
  await page.close();
  assert.deepEqual(errors, [], `${c.name} ${mode}: page errors`);
  return r;
}

(async () => {
  const browser = await chromium.launch({ headless: true });
  let failed = 0;
  try {
    for (const c of cases) {
      const styled = await render(browser, c, 'styled');
      const native = await render(browser, c, 'native');
      const bare = await render(browser, c, 'bare');
      const ratio = styled.a / styled.q;
      const line = `${c.name}: styled q=${styled.q}px a=${styled.a}px (a/q ${ratio.toFixed(2)}); native q=${native.q}px a=${native.a}px`;
      try {
        assert.ok(styled.q && styled.a, 'both sides have visible text');
        assert.ok(Math.abs(ratio - 1) <= 0.05, `answer ${styled.a}px vs question ${styled.q}px differ by more than 5%`);
        // Native mode hands typography back to the note type: same sizes as the note type CSS alone.
        assert.deepEqual(native, bare, `native mode changed sizes: ${JSON.stringify(native)} vs note type alone ${JSON.stringify(bare)}`);
        console.log(`PASS ${line}`);
      } catch (e) {
        failed++;
        console.log(`FAIL ${line}\n     ${e.message}`);
      }
    }
  } finally {
    await browser.close();
  }
  if (failed) { console.log(`${failed} case(s) failed`); process.exit(1); }
  console.log('all passed');
})().catch(e => { console.error(e); process.exit(1); });
