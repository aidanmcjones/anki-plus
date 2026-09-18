// Image and text tools for Add, Browse, Edit Current and in-place study editing.
(function () {
  "use strict";
  if (window.__baEditorTools) return;
  var selected = null, selectedRef = null, selectionBox, menu, toolbar, bookmark = null, drag = null;
  var dialog = null, crop = null;
  var imageObserver = new MutationObserver(function () { if (selected) positionSelection(); });
  var editorPage = !!document.querySelector('meta[name="ba-editor-tools"]');

  function fieldFor(node) {
    var el = node && (node.nodeType === 1 ? node : node.parentElement);
    var field = el && el.closest('[contenteditable="true"], [contenteditable=""]');
    if (!field) return null;
    if (editorPage) return field.tagName === "ANKI-EDITABLE" || field.closest(".field")
      || field.getRootNode().host?.closest(".rich-text-input") ? field : null;
    return document.body.classList.contains("ba-editing") && field.hasAttribute("data-ba-field") ? field : null;
  }

  function targetOf(event) { return event.composedPath()[0]; }
  function noteKey() { return typeof window.getNoteId === "function" ? window.getNoteId() : null; }
  function selectionFor(field) {
    var root = field.getRootNode();
    return root.getSelection ? root.getSelection() : window.getSelection();
  }

  // Anki remirrors field nodes on blur. Paths survive the toolbar taking focus.
  function nodePath(field, node) {
    var path = [];
    while (node && node !== field) {
      path.unshift(Array.prototype.indexOf.call(node.parentNode.childNodes, node));
      node = node.parentNode;
    }
    return node === field ? path : null;
  }
  function atPath(field, path) {
    return path.reduce(function (node, i) { return node && node.childNodes[i]; }, field);
  }
  function rememberSelection(field) {
    if (!field) return;
    var sel = selectionFor(field);
    if (!sel || !sel.rangeCount) return;
    var range = sel.getRangeAt(0);
    var start = nodePath(field, range.startContainer), end = nodePath(field, range.endContainer);
    if (start && end) bookmark = { field: field, note: noteKey(), start: start, end: end,
      startOffset: range.startOffset, endOffset: range.endOffset };
  }
  function restoreSelection() {
    if (!bookmark || bookmark.note !== noteKey() || !bookmark.field.isConnected || !fieldFor(bookmark.field)) return null;
    var b = bookmark, field = b.field;
    field.focus({ preventScroll: true });
    var start = atPath(field, b.start), end = atPath(field, b.end);
    if (!start || !end) return null;
    var range = document.createRange();
    try {
      range.setStart(start, b.startOffset);
      range.setEnd(end, b.endOffset);
      var sel = selectionFor(field);
      sel.removeAllRanges(); sel.addRange(range);
      return field;
    } catch (_) { return null; }
  }
  function changed(field) {
    field.dispatchEvent(new Event("input", { bubbles: true, composed: true }));
  }
  function format(command, value) {
    var field = restoreSelection();
    if (!field) return;
    document.execCommand("styleWithCSS", false, true);
    document.execCommand(command, false, value);
    changed(field);
    rememberSelection(field);
  }
  // Real installed fonts, from Qt's QFontDatabase (see __init__.py's
  // _system_fonts_meta) — not a hardcoded handful. Falls back to a small
  // generic list only if the meta tag is somehow missing, so the control
  // never renders empty.
  function systemFonts() {
    try {
      var meta = document.querySelector('meta[name="ba-system-fonts"]');
      if (meta && meta.content) {
        var list = JSON.parse(decodeURIComponent(meta.content));
        if (list && list.length) return list;
      }
    } catch (_) {}
    return ["Arial", "Helvetica", "Verdana", "Georgia", "Times New Roman", "Courier New", "system-ui"];
  }
  function escapeAttr(s) {
    return String(s).replace(/&/g, "&amp;").replace(/"/g, "&quot;").replace(/</g, "&lt;");
  }
  // Arbitrary pixel sizes, not a preset list — document.execCommand's
  // "fontSize" command only accepts the legacy 1-7 HTML size levels, so a
  // typed value like "37" is applied by wrapping the selection in a span
  // with an explicit inline font-size instead of going through execCommand.
  function applyFontSizePx(px) {
    var field = restoreSelection();
    if (!field) return;
    var sel = selectionFor(field);
    if (!sel || !sel.rangeCount || sel.isCollapsed) return;
    var range = sel.getRangeAt(0);
    var span = document.createElement("span");
    span.style.fontSize = px + "px";
    try {
      span.appendChild(range.extractContents());
      range.insertNode(span);
      sel.removeAllRanges();
      var newRange = document.createRange();
      newRange.selectNodeContents(span);
      sel.addRange(newRange);
      changed(field);
      rememberSelection(field);
    } catch (_) {}
  }
  function ensureToolbar() {
    if (!toolbar || !toolbar.isConnected) {
      toolbar = document.createElement("div");
      toolbar.id = "ba-text-tools";
      toolbar.setAttribute("role", "toolbar");
      toolbar.setAttribute("aria-label", "Text formatting");
      var fonts = systemFonts();
      // A single label per control, carried by aria-label + placeholder —
      // no separate visible <label> text duplicating what the control
      // itself already says. <input list> (a combobox, not a closed
      // <select>) accepts any typed font name, not just one from the
      // list; the <datalist> just gives searchable/type-ahead suggestions
      // drawn from every font actually installed on this machine.
      toolbar.innerHTML = '<span class="ba-tool"><input type="text" id="ba-font-input" list="ba-font-list" placeholder="Font" aria-label="Font family" autocomplete="off" spellcheck="false">'
        + '<datalist id="ba-font-list">' + fonts.map(function (font) { return '<option value="' + escapeAttr(font) + '">'; }).join("") + '</datalist></span>'
        + '<span class="ba-tool"><input type="number" id="ba-size-input" placeholder="Size" aria-label="Text size in pixels" min="6" max="300" step="1"></span>';
      var fontInput = toolbar.querySelector("#ba-font-input");
      fontInput.addEventListener("change", function () {
        if (fontInput.value) format("fontName", fontInput.value);
      });
      var sizeInput = toolbar.querySelector("#ba-size-input");
      function applySize() {
        var px = parseInt(sizeInput.value, 10);
        if (px) applyFontSizePx(px);
      }
      sizeInput.addEventListener("change", applySize);
      sizeInput.addEventListener("keydown", function (e) {
        if (e.key === "Enter") { e.preventDefault(); applySize(); }
      });
      // Keep selection snapshots made in the field, including across its blur.
      toolbar.addEventListener("pointerdown", function () {
        if (bookmark) rememberSelection(bookmark.field);
      });
      document.body.prepend(toolbar);
    }
    if (!editorPage) {
      var group = document.querySelector("#ba-edit-bar .ba-edit-fmt");
      if (group && toolbar.parentNode !== group) group.appendChild(toolbar);
    }
    toolbar.hidden = !editorPage && !document.body.classList.contains("ba-editing");
  }

  // Fields... and Cards... are two more standalone toolbar buttons on top
  // of the text/image tools above — the user's ask was to fold both into
  // one compact menu rather than adding a third item to the bar. We never
  // remove or relocate the stock buttons themselves (addcard.js's own
  // comment about this still applies: nothing here may touch a node
  // Svelte owns beyond text content) — just hide them and proxy-click
  // them from our own menu, so the exact same saveNow()+bridgeCommand
  // flow Anki's own NotetypeButtons.svelte runs still runs, unmodified.
  function ensureEditorMenu() {
    if (!editorPage) return;
    var nt = document.getElementById("notetype");
    if (!nt) return;
    if (document.getElementById("ba-editor-menu-btn")) return;
    // Positional, not text-matched: NotetypeButtons.svelte always renders
    // Fields then Cards, in that order — matching by English text prefix
    // would silently break on any non-English locale (the button text is
    // tr.editingFields()/tr.editingCards(), translated), and unlike the
    // hide-only cleanFieldsCardsLabels() in addcard.js, a failed match
    // here wouldn't just leave dots on a label: with the CSS-level
    // `#notetype .label-button { display: none !important; }` rule in
    // editor-tools.css doing the actual hiding (so there's no first-paint
    // flash while this function waits on the MutationObserver), a bug
    // here would hide Fields/Cards with no menu ever appearing to
    // replace them — a real functionality loss, not just a cosmetic one.
    var btns = nt.querySelectorAll(".label-button");
    var fieldsBtn = btns.length > 0 ? btns[0] : null;
    var cardsBtn = btns.length > 1 ? btns[1] : null;
    if (!fieldsBtn || !cardsBtn) return; // not rendered yet — retried by the caller
    // LabelButton's own compiled .label-button rule sets display:flex with
    // enough specificity to beat a plain inline style (its scoped Svelte
    // class wins ties against the `hidden` attribute's UA rule too) — an
    // inline style alone left both buttons laid out and hit-testable
    // despite `hidden`/`style.display`. `!important` in our own stylesheet
    // (editor-tools.css, loaded after Anki's bundled CSS) settles it.
    fieldsBtn.hidden = true;
    fieldsBtn.classList.add("ba-toolbar-button-hidden");
    cardsBtn.hidden = true;
    cardsBtn.classList.add("ba-toolbar-button-hidden");
    var wrap = document.createElement("span");
    wrap.className = "ba-editor-menu-wrap";
    wrap.innerHTML = '<button type="button" id="ba-editor-menu-btn" aria-haspopup="true" aria-expanded="false" aria-label="More editor tools" title="More editor tools">⋯</button>'
      + '<div id="ba-editor-menu" role="menu" hidden>'
      + '<button type="button" role="menuitem" data-action="fields">Fields...</button>'
      + '<button type="button" role="menuitem" data-action="card-styling">Card styling...</button>'
      + '<button type="button" role="menuitem" data-action="advanced">Advanced: edit raw template...</button>'
      + '</div>';
    nt.appendChild(wrap);
    var editorMenuBtn = wrap.querySelector("#ba-editor-menu-btn"), editorMenu = wrap.querySelector("#ba-editor-menu");
    function closeMenu() {
      editorMenu.hidden = true;
      editorMenuBtn.setAttribute("aria-expanded", "false");
    }
    editorMenuBtn.addEventListener("click", function (e) {
      e.stopPropagation();
      var opening = editorMenu.hidden;
      editorMenu.hidden = !opening;
      editorMenuBtn.setAttribute("aria-expanded", String(opening));
    });
    editorMenu.addEventListener("click", function (e) {
      var action = e.target && e.target.dataset && e.target.dataset.action;
      if (!action) return;
      closeMenu();
      if (action === "fields") fieldsBtn.click();
      else if (action === "advanced") cardsBtn.click();
      else if (action === "card-styling" && typeof pycmd === "function") pycmd("ba:card-styling:open");
    });
    document.addEventListener("click", function (e) {
      if (!editorMenu.hidden && !wrap.contains(e.target)) closeMenu();
    });
    document.addEventListener("keydown", function (e) {
      if (e.key === "Escape" && !editorMenu.hidden) { e.preventDefault(); closeMenu(); }
    }, true);
  }

  function hideSelection() {
    imageObserver.disconnect();
    selected = null;
    selectedRef = null;
    if (selectionBox) selectionBox.hidden = true;
    if (menu) menu.hidden = true;
  }
  function positionSelection() {
    resolveImage();
    if (!selected || !selected.isConnected || !fieldFor(selected)) { hideSelection(); return; }
    var r = selected.getBoundingClientRect();
    if (!r.width || !r.height) { hideSelection(); return; }
    Object.assign(selectionBox.style, { left: r.left + "px", top: r.top + "px", width: r.width + "px", height: r.height + "px" });
  }
  function selectImage(img) {
    selected = img;
    var field = fieldFor(img);
    selectedRef = { field: field, note: noteKey(), path: nodePath(field, img), src: img.getAttribute("src") };
    imageObserver.disconnect();
    imageObserver.observe(field, { childList: true, subtree: true, attributes: true });
    if (!selectionBox || !selectionBox.isConnected) {
      selectionBox = document.createElement("div");
      selectionBox.id = "ba-image-selection";
      ["nw", "ne", "sw", "se"].forEach(function (corner) {
        var handle = document.createElement("button");
        handle.type = "button"; handle.dataset.corner = corner;
        handle.setAttribute("aria-label", "Resize image " + corner);
        handle.title = "Resize image";
        handle.addEventListener("pointerdown", startResize);
        handle.addEventListener("pointermove", resize);
        handle.addEventListener("pointerup", finishResize);
        handle.addEventListener("pointercancel", finishResize);
        selectionBox.appendChild(handle);
      });
      document.body.appendChild(selectionBox);
    }
    selectionBox.hidden = false;
    positionSelection();
  }
  function setSize(img, width) {
    // Explicit dimensions must beat both card CSS and Anki's thumbnail rules.
    img.style.setProperty("width", Math.round(width) + "px", "important");
    img.style.setProperty("height", "auto", "important");
    img.style.setProperty("max-width", "100%", "important");
    img.style.setProperty("max-height", "none", "important");
    img.width = Math.round(width);
    img.removeAttribute("height");
  }
  function resolveImage() {
    if (selectedRef && selectedRef.note !== noteKey()) { hideSelection(); closeCrop(); return null; }
    if (selectedRef && (!selected || !selected.isConnected)) {
      var ref = selectedRef;
      var live = ref.field.isConnected && atPath(ref.field, ref.path);
      selected = live && live.tagName === "IMG" && live.getAttribute("src") === ref.src ? live : null;
    }
    return selected;
  }
  function startResize(event) {
    resolveImage();
    if (!selected || event.button !== 0) return;
    event.preventDefault(); event.stopPropagation();
    var field = fieldFor(selected), r = selected.getBoundingClientRect();
    field.focus({ preventScroll: true });
    var container = selected.parentElement;
    while (container && !container.clientWidth) container = container.parentElement;
    drag = { img: selected, field: field, width: r.width, height: r.height,
      maxWidth: container?.clientWidth || innerWidth,
      x: event.clientX, y: event.clientY, corner: event.currentTarget.dataset.corner };
    event.currentTarget.setPointerCapture(event.pointerId);
  }
  function resize(event) {
    if (!drag) return;
    var dx = (event.clientX - drag.x) * (drag.corner.includes("w") ? -1 : 1);
    var dy = (event.clientY - drag.y) * (drag.corner.includes("n") ? -1 : 1);
    var delta = Math.abs(dx) >= Math.abs(dy) ? dx : dy * drag.width / drag.height;
    var width = Math.min(Math.max(16, drag.width + delta), Math.max(16, drag.maxWidth));
    setSize(drag.img, width);
    positionSelection();
  }
  function finishResize() {
    if (drag) changed(drag.field);
    drag = null;
  }
  function showMenu(event, img) {
    selectImage(img);
    if (!menu || !menu.isConnected) {
      menu = document.createElement("div"); menu.id = "ba-image-menu"; menu.setAttribute("role", "menu");
      var button = document.createElement("button"); button.type = "button";
      button.textContent = "Crop image..."; button.setAttribute("role", "menuitem");
      button.addEventListener("click", function () { menu.hidden = true; openCrop(resolveImage()); });
      menu.appendChild(button); document.body.appendChild(menu);
    }
    menu.hidden = false;
    menu.style.left = Math.max(0, Math.min(event.clientX, innerWidth - menu.offsetWidth - 8)) + "px";
    menu.style.top = Math.max(0, Math.min(event.clientY, innerHeight - menu.offsetHeight - 8)) + "px";
    menu.firstChild.focus({ preventScroll: true });
  }

  function openCrop(img) {
    var field = img && fieldFor(img);
    if (!field) return;
    var imagePath = nodePath(field, img), source = img.getAttribute("src");
    var displayWidth = img.getBoundingClientRect().width;
    dialog = document.createElement("dialog"); dialog.id = "ba-crop-dialog";
    dialog.setAttribute("aria-labelledby", "ba-crop-title");
    dialog.innerHTML = '<h2 id="ba-crop-title">Crop image</h2><div id="ba-crop-stage"><img alt="Image to crop" draggable="false"><div id="ba-crop-box"></div></div>'
      + '<footer><output aria-live="polite"></output><button type="button" data-action="reset">Reset</button>'
      + '<button type="button" data-action="cancel">Cancel</button><button type="button" data-action="apply">Crop</button></footer>';
    var preview = dialog.querySelector("img"), stage = dialog.querySelector("#ba-crop-stage");
    crop = { field: field, note: noteKey(), path: imagePath, source: source, width: displayWidth, preview: preview,
      box: { x: 0, y: 0, w: 1, h: 1 }, dragging: null, busy: false };
    var thisCrop = crop;
    preview.onload = function () { if (crop === thisCrop) drawCrop(); };
    preview.onerror = function () { if (crop === thisCrop) dialog.querySelector("output").textContent = "Could not load this image."; };
    preview.src = img.currentSrc || img.src;
    stage.addEventListener("pointerdown", function (e) {
      if (crop.busy || e.button !== 0) return;
      e.preventDefault();
      crop.dragging = cropPoint(e); stage.setPointerCapture(e.pointerId);
    });
    stage.addEventListener("pointermove", function (e) {
      if (!crop.dragging) return;
      var point = cropPoint(e), start = crop.dragging;
      crop.box = { x: Math.min(point.x, start.x), y: Math.min(point.y, start.y),
        w: Math.abs(point.x - start.x), h: Math.abs(point.y - start.y) };
      drawCrop();
    });
    stage.addEventListener("pointerup", function () { crop.dragging = null; });
    stage.addEventListener("pointercancel", function () { crop.dragging = null; });
    dialog.querySelector('[data-action="reset"]').onclick = function () { crop.box = { x: 0, y: 0, w: 1, h: 1 }; drawCrop(); };
    dialog.querySelector('[data-action="cancel"]').onclick = closeCrop;
    dialog.querySelector('[data-action="apply"]').onclick = applyCrop;
    dialog.addEventListener("cancel", function (e) { e.preventDefault(); closeCrop(); });
    document.body.appendChild(dialog); dialog.showModal(); drawCrop();
  }
  function closeCrop() {
    if (dialog) { dialog.close(); dialog.remove(); }
    dialog = null; crop = null;
  }
  function cropPoint(event) {
    var r = crop.preview.getBoundingClientRect();
    return { x: Math.max(0, Math.min(1, (event.clientX - r.left) / r.width)),
      y: Math.max(0, Math.min(1, (event.clientY - r.top) / r.height)) };
  }
  function drawCrop() {
    if (!crop || !dialog) return;
    var b = crop.box, box = dialog.querySelector("#ba-crop-box");
    Object.assign(box.style, { left: b.x * 100 + "%", top: b.y * 100 + "%", width: b.w * 100 + "%", height: b.h * 100 + "%" });
    dialog.querySelector("output").textContent = Math.round(b.w * crop.preview.naturalWidth) + " x " + Math.round(b.h * crop.preview.naturalHeight) + " px";
  }
  function applyCrop() {
    if (!crop || crop.busy) return;
    if (crop.note !== noteKey()) { closeCrop(); hideSelection(); return; }
    var c = crop, p = c.preview, b = c.box;
    var x = Math.round(b.x * p.naturalWidth), y = Math.round(b.y * p.naturalHeight);
    var w = Math.min(p.naturalWidth - x, Math.round(b.w * p.naturalWidth));
    var h = Math.min(p.naturalHeight - y, Math.round(b.h * p.naturalHeight));
    if (w < 1 || h < 1) { dialog.querySelector("output").textContent = "Select an area of the image."; return; }
    try {
      var canvas = document.createElement("canvas"); canvas.width = w; canvas.height = h;
      canvas.getContext("2d").drawImage(p, x, y, w, h, 0, 0, w, h);
      var png = canvas.toDataURL("image/png").split(",")[1];
      c.busy = true;
      dialog.querySelector('[data-action="apply"]').disabled = true;
      dialog.querySelector("output").textContent = "Saving crop...";
      pycmd("ba:image-crop:" + png, function (result) {
        if (crop !== c) return;
        if (!result || result.error) { cropError(result?.error || "Could not save the cropped image."); return; }
        var target = c.field.isConnected && atPath(c.field, c.path);
        if (c.note !== noteKey() || !target || target.tagName !== "IMG" || target.getAttribute("src") !== c.source || !fieldFor(target)) {
          cropError("The field changed. Close this dialog and select the image again."); return;
        }
        // Focus before mutating so Anki's DOM mirror does not replace the node.
        closeCrop();
        c.field.focus({ preventScroll: true });
        target.src = encodeURIComponent(result.filename);
        setSize(target, c.width * w / p.naturalWidth);
        changed(c.field);
        selectImage(target);
        target.addEventListener("load", positionSelection, { once: true });
      });
    } catch (error) {
      cropError("Could not crop this image. " + error.message);
    }
  }
  function cropError(message) {
    if (!crop) return;
    crop.busy = false;
    dialog.querySelector('[data-action="apply"]').disabled = false;
    dialog.querySelector("output").textContent = message;
  }

  function boot() {
    if (editorPage) document.body.classList.add("ba-editor-tools-page");
    ensureToolbar();
    ensureEditorMenu();
    document.addEventListener("click", function (event) {
      var target = targetOf(event);
      if (target.tagName === "IMG" && fieldFor(target) && !target.classList.contains("mathjax")) {
        // Prevent the stock ImageOverlay from expanding or covering this image.
        event.preventDefault(); event.stopImmediatePropagation();
        selectImage(target); return;
      }
      if (!target.closest?.('#ba-image-selection, #ba-image-menu, #ba-crop-dialog')) hideSelection();
    }, true);
    document.addEventListener("dblclick", function (event) {
      var target = targetOf(event);
      if (target.tagName === "IMG" && fieldFor(target)) { event.preventDefault(); event.stopImmediatePropagation(); }
    }, true);
    document.addEventListener("contextmenu", function (event) {
      var target = targetOf(event);
      if (target.tagName !== "IMG" || !fieldFor(target) || target.classList.contains("mathjax")) return;
      event.preventDefault(); event.stopImmediatePropagation(); showMenu(event, target);
    }, true);
    document.addEventListener("keyup", function (e) { rememberSelection(fieldFor(targetOf(e))); }, true);
    document.addEventListener("pointerup", function (e) { rememberSelection(fieldFor(targetOf(e))); }, true);
    document.addEventListener("focusin", function (e) { rememberSelection(fieldFor(targetOf(e))); }, true);
    document.addEventListener("selectionchange", function () {
      var active = document.activeElement;
      while (active?.shadowRoot?.activeElement) active = active.shadowRoot.activeElement;
      rememberSelection(fieldFor(active));
    });
    document.addEventListener("keydown", function (e) {
      if (dialog) {
        // Escape cancels the crop, never the surrounding inline edit session.
        e.stopImmediatePropagation();
        if (e.key === "Escape") { e.preventDefault(); closeCrop(); }
      } else if (e.key === "Escape" && menu && !menu.hidden) {
        e.preventDefault(); e.stopImmediatePropagation(); menu.hidden = true;
      }
    }, true);
    window.addEventListener("scroll", function () { if (selected) positionSelection(); }, true);
    window.addEventListener("resize", function () { if (selected) positionSelection(); drawCrop(); });
    new MutationObserver(function () {
      ensureToolbar();
      ensureEditorMenu();
      if (selected) positionSelection();
      if (crop && (!crop.field.isConnected || !fieldFor(crop.field))) closeCrop();
    }).observe(document.body, { childList: true, subtree: true, attributes: true, attributeFilter: ["class", "contenteditable"] });
  }
  window.__baEditorTools = { clear: function () { hideSelection(); closeCrop(); bookmark = null; } };
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot);
  else boot();
})();
