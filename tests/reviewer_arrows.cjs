/* Arrow-key grading (config arrow_key_grading): Up/Right/Left/Down grade
   ease 4/3/2/1 on the answer side, reveal on the question side, and stay out
   of the way while typing or when the option is off. Run with NODE_PATH
   pointing to a Playwright installation. */
const { chromium } = require('playwright');
const assert = require('node:assert/strict');
const path = require('node:path');
const root = path.resolve(__dirname, '..');

async function load(browser, opts) {
  const page = await browser.newPage({ viewport: { width: 1000, height: 700 } });
  const errors = []; page.on('pageerror', e => errors.push(e.message));
  await page.setContent(`<html><body>
    <div id="qa"><div class="card"><p id="question">Front text</p></div></div>
    <textarea id="notes"></textarea>
    <div class="ba-rv-ease" hidden>
      <button class="ba-rv-ease-key" data-ease="1"><span class="ba-rv-ease-int"></span></button>
      <button class="ba-rv-ease-key" data-ease="2"><span class="ba-rv-ease-int"></span></button>
      <button class="ba-rv-ease-key" data-ease="3"><span class="ba-rv-ease-int"></span></button>
      <button class="ba-rv-ease-key" data-ease="4"><span class="ba-rv-ease-int"></span></button>
    </div></body></html>`);
  await page.evaluate(o => {
    window.__baOpts = { reviewer: o };
    window.sent = [];
    window.pycmd = cmd => { window.sent.push(cmd); };
  }, opts);
  await page.addScriptTag({ path: path.join(root, 'web/reviewer.js') });
  return { page, errors };
}

const sent = page => page.evaluate(() => window.sent.splice(0));
const showAnswer = (page, intervals) => page.evaluate(iv => {
  document.activeElement && document.activeElement.blur && document.activeElement.blur();
  window.__baSetEase(iv, 3, true);
}, intervals || { 1: '1m', 2: '6m', 3: '10m', 4: '4d' });
const showQuestion = page => page.evaluate(() => window.__baSetEase({}, 3, false));

(async () => {
  const browser = await chromium.launch({ headless: true });
  try {
    // Answer side: each arrow sends its ease.
    let { page, errors } = await load(browser, { arrowKeyGrading: true });
    await showAnswer(page);
    for (const [key, cmd] of [['ArrowUp', 'ease4'], ['ArrowRight', 'ease3'],
                              ['ArrowDown', 'ease1'], ['ArrowLeft', 'ease2']]) {
      await page.keyboard.press(key);
      assert.deepEqual(await sent(page), [cmd], `${key} on answer side`);
    }
    console.log('PASS answer side: Up ease4, Right ease3, Down ease1, Left ease2');

    // Handled keys suppress the default scroll.
    const prevented = await page.evaluate(() => {
      const ev = new KeyboardEvent('keydown', { key: 'ArrowDown', bubbles: true, cancelable: true });
      document.body.dispatchEvent(ev);
      return ev.defaultPrevented;
    });
    assert.equal(prevented, true, 'handled arrow should preventDefault');
    await sent(page);

    // Question side: any arrow reveals, never grades.
    await showQuestion(page);
    for (const key of ['ArrowUp', 'ArrowRight', 'ArrowDown', 'ArrowLeft']) {
      await page.keyboard.press(key);
      assert.deepEqual(await sent(page), ['ans'], `${key} on question side`);
    }
    console.log('PASS question side: every arrow sends ans and no ease');

    // Focused textarea: nothing is sent and the caret keeps the key.
    await showAnswer(page);
    await page.focus('#notes');
    for (const key of ['ArrowUp', 'ArrowRight', 'ArrowDown', 'ArrowLeft']) {
      await page.keyboard.press(key);
    }
    assert.deepEqual(await sent(page), [], 'arrows in a textarea must not grade');
    // Quick editor active (body.ba-editing): nothing either.
    await page.evaluate(() => { document.activeElement.blur(); document.body.classList.add('ba-editing'); });
    await page.keyboard.press('ArrowRight');
    assert.deepEqual(await sent(page), [], 'arrows while editing must not grade');
    await page.evaluate(() => document.body.classList.remove('ba-editing'));
    console.log('PASS typing: focused textarea and edit mode send nothing');

    // Fewer buttons: Easy clamps to the top offered ease, Again stays 1.
    await showAnswer(page, { 1: '1m', 2: '10m', 3: '1d' });
    await page.keyboard.press('ArrowUp');
    await page.keyboard.press('ArrowDown');
    assert.deepEqual(await sent(page), ['ease3', 'ease1'], '3-button mapping');
    console.log('PASS 3-button card: Up clamps to ease3, Down stays ease1');
    assert.deepEqual(errors, []);
    await page.close();

    // Option off: nothing sent, default (scroll) untouched.
    ({ page, errors } = await load(browser, { arrowKeyGrading: false }));
    await showAnswer(page);
    await page.keyboard.press('ArrowUp');
    await showQuestion(page);
    await page.keyboard.press('ArrowRight');
    assert.deepEqual(await sent(page), [], 'option off must send nothing');
    const offPrevented = await page.evaluate(() => {
      const ev = new KeyboardEvent('keydown', { key: 'ArrowDown', bubbles: true, cancelable: true });
      document.body.dispatchEvent(ev);
      return ev.defaultPrevented;
    });
    assert.equal(offPrevented, false, 'option off must not preventDefault');
    assert.deepEqual(errors, []);
    console.log('PASS option off: arrows send nothing and keep their default');
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
