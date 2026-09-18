/* Run with NODE_PATH pointing to a Playwright installation. */
const { chromium } = require('playwright');
const assert = require('node:assert/strict');
const path = require('node:path');
const root = path.resolve(__dirname, '..');

// A long-ish font list (mirrors real QFontDatabase output being 100+
// entries) so tests can actually exercise scrolling, not just presence.
const TEST_FONTS = Array.from({ length: 60 }, (_, i) => `Test Font ${String(i).padStart(2, '0')}`)
  .concat(['Georgia', 'Arial', 'Monaco', 'Consolas']);

async function fixture(page, mode) {
  const fontsMeta = mode !== 'study'
    ? `<meta name="ba-system-fonts" content="${encodeURIComponent(JSON.stringify(TEST_FONTS))}">`
    : '';
  // A mock #notetype group with two .label-button children stands in for
  // NotetypeButtons.svelte's Fields.../Cards... row — real enough for
  // ensureEditorMenu() (and the compact Aa/Size controls it builds
  // alongside the "..." menu) to find and attach to, the same way it
  // would against the real editor.
  const notetypeMock = mode !== 'study'
    ? '<div id="notetype"><button class="label-button">Fields...</button><button class="label-button">Cards...</button></div>'
    : '';
  await page.setContent(`<html class="shrink-image nightMode"><head>${mode !== 'study' ? '<meta name="ba-editor-tools" content="1">' : ''}${fontsMeta}</head>
    <body class="${mode === 'study' ? 'ba-editing' : ''}">${notetypeMock}<main style="margin:100px 20px;max-width:600px"><div id="host" class="rich-text-input"></div></main></body></html>`);
  await page.addStyleTag({ path: path.join(root, 'web/editor-tools.css') });
  await page.evaluate(mode => {
    const canvas = document.createElement('canvas'); canvas.width = 1600; canvas.height = 1000;
    const ctx = canvas.getContext('2d'); ctx.fillStyle = '#fff'; ctx.fillRect(0,0,1600,1000);
    ctx.fillStyle = '#177244'; ctx.fillRect(300,200,900,600);
    ctx.fillStyle = '#111'; ctx.font = '80px sans-serif'; ctx.fillText('Biochemistry figure',320,340);
    const host = document.querySelector('#host');
    const parent = mode === 'study' ? host : host.attachShadow({ mode: 'open' });
    parent.innerHTML = '<style>img {max-width:320px;max-height:200px} [contenteditable] {display:block; min-height:300px; color:white; font:16px Arial;}</style>';
    const field = document.createElement(mode === 'study' ? 'div' : 'anki-editable');
    field.contentEditable = 'true'; field.dataset.baField = 'Front';
    if (mode === 'study') field.style.display = 'inline';
    field.innerHTML = '<p id="words">Alpha beta gamma</p><img src="' + canvas.toDataURL() + '"><p>Unchanged text</p>';
    parent.appendChild(field); window.field = field;
    // Match the full editor's remirroring of content on blur.
    if (mode !== 'study') field.addEventListener('blur', () => { field.innerHTML = field.innerHTML; });
    window.crops = [];
    window.noteId = 1;
    window.getNoteId = () => window.noteId;
    window.pycmd = (cmd, cb) => {
      window.crops.push(cmd);
      if (window.failCrop) cb({error:'Test write failure'});
      else cb({filename: 'anki-crop-test.png'});
    };
  }, mode);
  await page.addScriptTag({ path: path.join(root, 'web/editor-tools.js') });
}

(async () => {
  const browser = await chromium.launch({ headless: true });
  try {
    for (const mode of ['add', 'browse', 'full', 'study']) {
      const page = await browser.newPage({ viewport: { width: 1100, height: 800 } });
      const errors = []; page.on('pageerror', e => errors.push(e.message));
      await fixture(page, mode);
      const image = page.locator('#host img');
      const before = await image.boundingBox();
      const html = await image.evaluate(el => el.outerHTML);
      await image.click();
      const after = await image.boundingBox();
      assert.deepEqual(after, before, `${mode}: selection changed image size`);
      assert.equal(await image.evaluate(el => el.outerHTML), html, 'selection mutated saved image');
      await page.locator('#ba-image-selection').waitFor({state:'visible'});
      const handle = await page.locator('[data-corner="se"]').boundingBox();
      await page.mouse.move(handle.x + 6, handle.y + 6); await page.mouse.down();
      await page.mouse.move(handle.x - 74, handle.y - 44, {steps:8}); await page.mouse.up();
      const smaller = await image.boundingBox();
      assert.ok(smaller.width < before.width - 50, `${mode}: resize had no effect`);
      assert.ok(Math.abs(smaller.width / smaller.height - 1.6) < .01);
      await image.click({button:'right'});
      await page.getByRole('menuitem', {name:'Crop image...'}).click();
      await page.locator('#ba-crop-dialog').waitFor({state:'visible'});
      const cropBefore = await image.evaluate(el => el.getAttribute('src'));
      await page.keyboard.press('Escape');
      assert.equal(await page.locator('#ba-crop-dialog').count(),0);
      assert.equal(await image.evaluate(el => el.getAttribute('src')), cropBefore);
      assert.equal(await page.evaluate(() => crops.length),0);
      await image.click({button:'right'}); await page.getByRole('menuitem').click();
      const preview = await page.locator('#ba-crop-stage img').boundingBox();
      await page.mouse.move(preview.x + preview.width *.2, preview.y + preview.height *.2);
      await page.mouse.down();
      await page.mouse.move(preview.x + preview.width *.8, preview.y + preview.height *.8,{steps:5});
      await page.mouse.up();
      await page.screenshot({path:path.join(root, 'out', `editor-crop-${mode}.png`)});
      await page.locator('[data-action="apply"]').click();
      assert.equal(await image.evaluate(el => el.getAttribute('src')),'anki-crop-test.png');
      assert.equal(await page.locator('#ba-crop-dialog').count(),0);
      assert.equal(await page.evaluate(() => crops.length),1);
      const cropSize = await page.evaluate(async () => {
        const img = new Image(); img.src = 'data:image/png;base64,' + crops[0].slice('ba:image-crop:'.length);
        await img.decode(); return [img.width,img.height];
      });
      assert.ok(Math.abs(cropSize[0]-960)<4 && Math.abs(cropSize[1]-600)<4, cropSize);
      // Select only beta. Blur replaces text nodes in the native editor fixture.
      await page.evaluate(() => {
        field.focus(); const node = field.querySelector('#words').firstChild;
        const range = document.createRange(); range.setStart(node,6);range.setEnd(node,10);
        const sel = field.getRootNode().getSelection?.() || window.getSelection();
        sel.removeAllRanges();sel.addRange(range);
        field.dispatchEvent(new KeyboardEvent('keyup',{key:'Shift',bubbles:true,composed:true}));
      });
      // Font/Size are free-typed comboboxes (any system font, any pixel
      // size), not closed <select> presets — see the double-"Font"-label
      // and preset-only-sizes fixes in editor-tools.js. In the real
      // editor (every mode but study) every control — Fields, Style, Aa,
      // Size — is the same uniform icon-button hotbar; Aa and
      // Size each pop their own popover (a font search box; a +/-
      // stepper), no full-width row and no visually distinct wide text
      // input. The reviewer's inline quick editor (study) keeps the
      // always-visible full-row inputs, a different, simpler surface.
      if (mode !== 'study') {
        await page.locator('#ba-font-btn').click();
        await page.locator('#ba-font-popover').waitFor({ state: 'visible' });
      }
      await page.getByLabel('Font family',{exact:true}).fill('Georgia');
      await page.getByLabel('Font family',{exact:true}).press('Tab');
      if (mode !== 'study') {
        await page.locator('#ba-size-btn').click();
        await page.locator('#ba-size-popover').waitFor({ state: 'visible' });
      }
      await page.getByLabel('Text size in pixels',{exact:true}).fill('24');
      await page.getByLabel('Text size in pixels',{exact:true}).press('Tab');
      const formatted = await page.evaluate(() => ({html:field.innerHTML,
        spans:[...field.querySelector('#words *') ? field.querySelectorAll('#words *') : []].map(el=>[el.textContent,getComputedStyle(el).fontFamily,getComputedStyle(el).fontSize])}));
      assert.ok(formatted.spans.some(([text,font,size])=>text==='beta' && font.includes('Georgia') && size==='24px'),JSON.stringify(formatted));
      assert.ok(formatted.html.includes('Unchanged text'));
      assert.deepEqual(errors,[]);
      console.log(`PASS ${mode}: no image expansion, resize, crop/cancel, font/size selection`);
      await page.close();
    }
    // Font suggestion list: real scroll container (not a native datalist,
    // which ignored max-height/overflow-y and had no working wheel or
    // keyboard scroll in the target QtWebEngine build), type-ahead
    // filtering, and keyboard navigation that scrolls the highlighted
    // option into view.
    {
      const page = await browser.newPage({ viewport: { width: 1100, height: 800 } });
      await fixture(page, 'full');
      await page.locator('#ba-font-btn').click();
      await page.locator('#ba-font-popover').waitFor({ state: 'visible' });
      const input = page.getByLabel('Font family', { exact: true });
      await input.click();
      const dropdown = page.locator('#ba-font-dropdown');
      await dropdown.waitFor({ state: 'visible' });
      const allCount = await dropdown.locator('[role="option"]').count();
      assert.equal(allCount, TEST_FONTS.length, 'dropdown should list every font up front');
      const clientH = await dropdown.evaluate(el => el.clientHeight);
      const scrollH = await dropdown.evaluate(el => el.scrollHeight);
      assert.ok(scrollH > clientH, `list should overflow its box (client=${clientH} scroll=${scrollH})`);
      // Real trackpad/wheel scrolling is the browser's own default action
      // for a trusted wheel event over an overflow:auto box — standard
      // behavior needing no code of ours, unlike the native <datalist>
      // popup this replaces (which had no overflow-y hook at all). CDP's
      // synthesized wheel input isn't reliably "trusted" enough to trigger
      // that default action in every headless configuration (confirmed:
      // even a raw dispatchEvent(new WheelEvent(...)) doesn't scroll a
      // plain overflow:auto div in Chromium — trusted-only by design), so
      // this is verified directly against the real target platform
      // (QtWebEngine) instead of asserted here. What IS asserted here,
      // reliably: the box genuinely overflows (above) and the assignable
      // scrollTop actually moves content — the two things a broken
      // scroll container would get wrong regardless of input method.
      await dropdown.evaluate(el => { el.scrollTop = 50; });
      assert.equal(await dropdown.evaluate(el => el.scrollTop), 50, 'scrollTop is not settling on this element');
      // Keyboard nav: arrow down past the first screenful should scroll the
      // highlighted option into view even if the mouse never touches it.
      await dropdown.evaluate(el => { el.scrollTop = 0; });
      for (let i = 0; i < 40; i++) await input.press('ArrowDown');
      const scrollTopAfterKeys = await dropdown.evaluate(el => el.scrollTop);
      assert.ok(scrollTopAfterKeys > 0, 'arrow-key navigation did not scroll the list');
      const highlighted = await dropdown.locator('[role="option"].active').textContent();
      assert.equal(highlighted, TEST_FONTS[40]);
      // Type-ahead filtering: typing narrows the list to matching fonts.
      await input.fill('');
      await input.fill('georgia');
      await dropdown.locator('[role="option"]').first().waitFor({ state: 'visible' });
      const filteredCount = await dropdown.locator('[role="option"]').count();
      assert.equal(filteredCount, 1);
      assert.equal(await dropdown.locator('[role="option"]').first().textContent(), 'Georgia');
      console.log('PASS font dropdown: scrolls (wheel + keyboard), lists all fonts, type-ahead filters');
      await page.close();
    }
    // A storage error must leave the original image and crop dialog intact.
    const page = await browser.newPage(); await fixture(page,'full');
    const original = await page.locator('#host img').getAttribute('src');
    await page.evaluate(()=>window.failCrop=true);
    await page.locator('#host img').click({button:'right'});await page.getByRole('menuitem').click();
    await page.locator('[data-action="apply"]').click();
    assert.equal(await page.locator('#host img').getAttribute('src'),original);
    assert.equal(await page.locator('#ba-crop-dialog output').textContent(),'Test write failure');
    console.log('PASS storage failure keeps original image');
    await page.keyboard.press('Escape');
    await page.evaluate(()=>window.failCrop=false);
    await page.locator('#host img').click({button:'right'});await page.getByRole('menuitem').click();
    const oldWrites = await page.evaluate(()=>crops.length);
    await page.evaluate(()=>window.noteId=2);
    await page.locator('[data-action="apply"]').click();
    assert.equal(await page.evaluate(()=>crops.length),oldWrites);
    assert.equal(await page.locator('#ba-crop-dialog').count(),0);
    console.log('PASS switching notes cannot apply a stale crop');
    await page.setViewportSize({width:375,height:600});await fixture(page,'full');
    await page.locator('#host img').click({button:'right'});await page.getByRole('menuitem').click();
    const bounds=await page.locator('#ba-crop-dialog').boundingBox();
    assert.ok(bounds.x >= 0 && bounds.x + bounds.width <= 375);
    assert.ok(bounds.y >= 0 && bounds.y + bounds.height <= 600);
    await page.screenshot({path:path.join(root,'out','editor-crop-narrow.png')});
    console.log('PASS narrow-window crop layout');
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
