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
  // Shared by the font suggestion list and the "..." editor menu: both are
  // position:fixed popups anchored under a trigger element. A static CSS
  // `right:0`/`top:100%` assumes the trigger sits at the right edge of a
  // positioned ancestor — true for a classic top-right "more" button, but
  // Fields/Cards (and now the "..." button that replaces them) render
  // wherever NotetypeButtons.svelte put them, which is NOT the right edge
  // of a narrow Browse editor pane. That mismatch pushed the "..." menu
  // mostly off the left edge of the viewport (getBoundingClientRect().left
  // came back around -151px on a real repro) — visually just a sliver, or
  // nothing at all, exactly matching "the menu opens empty". Compute the
  // position from the trigger's actual on-screen rect instead, clamped so
  // the popup never leaves the viewport regardless of where the trigger
  // ends up.
  function positionPopup(popup, anchor) {
    var r = anchor.getBoundingClientRect();
    popup.style.top = (r.bottom + 4) + "px";
    var width = popup.offsetWidth;
    var maxLeft = Math.max(8, window.innerWidth - width - 8);
    popup.style.left = Math.max(8, Math.min(r.left, maxLeft)) + "px";
    var maxHeight = window.innerHeight - r.bottom - 12;
    popup.style.maxHeight = Math.max(120, Math.min(320, maxHeight)) + "px";
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
  // Font is a custom combobox, not <input list=datalist>: a native
  // datalist popup can't be styled (no max-height/overflow-y a browser is
  // required to honor) and in this QtWebEngine build the 196-entry
  // suggestion list rendered with no working scroll at all — arrow keys
  // and wheel/trackpad both did nothing past the first screenful. A plain
  // div-based listbox (same family as #ba-editor-menu and #ba-image-menu)
  // gets a real scroll container plus keyboard nav we control directly.
  // Shared by both the reviewer's full-row font input and the real
  // editor's compact "Aa" popover — same filtering/keyboard/scroll
  // behavior either way, just different trigger chrome around it.
  function attachFontCombobox(input, dropdown, idPrefix, applyFn) {
    var matches = [];
    var highlight = -1;
    function updateHighlight() {
      var opts = dropdown.children;
      for (var i = 0; i < opts.length; i++) {
        var active = i === highlight;
        opts[i].setAttribute("aria-selected", String(active));
        opts[i].classList.toggle("active", active);
        if (active) opts[i].scrollIntoView({ block: "nearest" });
      }
      input.setAttribute("aria-activedescendant", highlight >= 0 ? idPrefix + "-opt-" + highlight : "");
    }
    function close() {
      dropdown.hidden = true;
      input.setAttribute("aria-expanded", "false");
      highlight = -1;
    }
    function open() {
      var q = input.value.trim().toLowerCase();
      var all = systemFonts();
      matches = q ? all.filter(function (f) { return f.toLowerCase().indexOf(q) !== -1; }) : all;
      if (!matches.length) { close(); return; }
      dropdown.innerHTML = matches.map(function (f, i) {
        return '<div role="option" id="' + idPrefix + '-opt-' + i + '" data-index="' + i + '">' + escapeAttr(f) + '</div>';
      }).join("");
      highlight = 0;
      dropdown.hidden = false;
      input.setAttribute("aria-expanded", "true");
      positionPopup(dropdown, input);
      updateHighlight();
    }
    function choose(index) {
      var f = matches[index];
      if (!f) return;
      input.value = f;
      close();
      applyFn(f);
    }
    input.addEventListener("input", open);
    input.addEventListener("focus", open);
    input.addEventListener("keydown", function (e) {
      if (dropdown.hidden) {
        if (e.key === "ArrowDown" || e.key === "ArrowUp") { e.preventDefault(); open(); }
        return;
      }
      if (e.key === "ArrowDown") {
        e.preventDefault();
        highlight = Math.min(highlight + 1, matches.length - 1);
        updateHighlight();
      } else if (e.key === "ArrowUp") {
        e.preventDefault();
        highlight = Math.max(highlight - 1, 0);
        updateHighlight();
      } else if (e.key === "Enter") {
        e.preventDefault();
        if (highlight >= 0) choose(highlight);
      } else if (e.key === "Escape") {
        close();
      }
    });
    // mousedown, not click: fires before the input's blur handler would
    // otherwise close the dropdown out from under the click.
    dropdown.addEventListener("mousedown", function (e) {
      var opt = e.target.closest("[role=option]");
      if (!opt) return;
      e.preventDefault();
      choose(Number(opt.dataset.index));
    });
    input.addEventListener("blur", function () { setTimeout(close, 150); });
    input.addEventListener("change", function () { if (input.value) applyFn(input.value); });
    return { open: open, close: close };
  }
  function ensureToolbar() {
    // The real editor (Add/Browse/Edit Current) gets compact Aa/Size
    // controls folded into the icon toolbar instead — see
    // ensureCompactFontSize(). This full-width row stays only for the
    // reviewer's inline quick editor, a smaller, simpler surface where a
    // dedicated row is the right call.
    if (editorPage) return;
    if (!toolbar || !toolbar.isConnected) {
      toolbar = document.createElement("div");
      toolbar.id = "ba-text-tools";
      toolbar.setAttribute("role", "toolbar");
      toolbar.setAttribute("aria-label", "Text formatting");
      // A single label per control, carried by aria-label + placeholder —
      // no separate visible <label> text duplicating what the control
      // itself already says.
      toolbar.innerHTML = '<span class="ba-tool ba-font-tool"><input type="text" id="ba-font-input" placeholder="Font" aria-label="Font family" role="combobox" aria-autocomplete="list" aria-expanded="false" autocomplete="off" spellcheck="false">'
        + '<div id="ba-font-dropdown" role="listbox" aria-label="Font suggestions" hidden></div></span>'
        + '<span class="ba-tool"><input type="number" id="ba-size-input" placeholder="Size" aria-label="Text size in pixels" min="6" max="300" step="1"></span>';
      var fontInput = toolbar.querySelector("#ba-font-input");
      var fontDropdown = toolbar.querySelector("#ba-font-dropdown");
      attachFontCombobox(fontInput, fontDropdown, "ba-font", function (f) { format("fontName", f); });
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
    var group = document.querySelector("#ba-edit-bar .ba-edit-fmt");
    if (group && toolbar.parentNode !== group) group.appendChild(toolbar);
    toolbar.hidden = !document.body.classList.contains("ba-editing");
  }
  // Compact replacement for the full-width row, folded directly into the
  // stock icon toolbar next to B/I/U/etc: a small "Aa" button that pops
  // the same searchable font combobox as above, and a narrow numeric size
  // box — no separate row, no full-width text inputs, reclaiming the
  // vertical space the row used to take.
  function ensureCompactFontSize(nt) {
    if (document.getElementById("ba-font-btn")) return;
    var wrap = document.createElement("span");
    wrap.className = "ba-compact-font-wrap";
    wrap.innerHTML = '<button type="button" id="ba-font-btn" aria-haspopup="true" aria-expanded="false" title="Font family">Aa</button>'
      + '<div id="ba-font-popover" hidden>'
      + '<input type="text" id="ba-font-input" placeholder="Search fonts" aria-label="Font family" role="combobox" aria-autocomplete="list" aria-expanded="false" autocomplete="off" spellcheck="false">'
      + '<div id="ba-font-dropdown" role="listbox" aria-label="Font suggestions" hidden></div>'
      + '</div>'
      + '<input type="number" id="ba-size-input" placeholder="Size" title="Text size in pixels" aria-label="Text size in pixels" min="6" max="300" step="1">';
    nt.appendChild(wrap);
    var fontBtn = wrap.querySelector("#ba-font-btn");
    var fontPopover = wrap.querySelector("#ba-font-popover");
    var fontInput = wrap.querySelector("#ba-font-input");
    var fontDropdown = wrap.querySelector("#ba-font-dropdown");
    var combobox = attachFontCombobox(fontInput, fontDropdown, "ba-font", function (f) { format("fontName", f); });
    function closePopover() {
      fontPopover.hidden = true;
      fontBtn.setAttribute("aria-expanded", "false");
      combobox.close();
    }
    fontBtn.addEventListener("click", function (e) {
      e.stopPropagation();
      var opening = fontPopover.hidden;
      if (!opening) { closePopover(); return; }
      fontPopover.hidden = false;
      fontBtn.setAttribute("aria-expanded", "true");
      positionPopup(fontPopover, fontBtn);
      fontInput.value = "";
      fontInput.focus();
    });
    document.addEventListener("click", function (e) {
      if (!fontPopover.hidden && !wrap.contains(e.target)) closePopover();
    });
    document.addEventListener("keydown", function (e) {
      if (e.key === "Escape" && !fontPopover.hidden) { e.preventDefault(); closePopover(); }
    }, true);
    var sizeInput = wrap.querySelector("#ba-size-input");
    function applySize() {
      var px = parseInt(sizeInput.value, 10);
      if (px) applyFontSizePx(px);
    }
    sizeInput.addEventListener("change", applySize);
    sizeInput.addEventListener("keydown", function (e) {
      if (e.key === "Enter") { e.preventDefault(); applySize(); }
    });
    wrap.addEventListener("pointerdown", function () {
      if (bookmark) rememberSelection(bookmark.field);
    });
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
    ensureCompactFontSize(nt);
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
      // See positionPopup's comment: a static CSS right:0 assumed this
      // button sits at its container's right edge, which put the menu
      // mostly off the left side of the viewport in a narrower pane
      // (Browse) — reachable, technically, but indistinguishable from
      // "the menu opens empty" at a glance. Position from the button's
      // actual rect instead, every time it opens (the toolbar can reflow
      // between opens).
      if (opening) positionPopup(editorMenu, editorMenuBtn);
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

  // The stock "remove formatting" eraser (plus its own small dropdown
  // chevron for picking which formats to strip) is RemoveFormatButton.
  // svelte's two IconButtons — NOT plain adjacent siblings, confirmed by
  // reading the actual rendered DOM: the chevron sits inside
  // WithFloating's own <span class="floating-reference"> wrapper (plus a
  // <svelte-css-wrapper> Svelte adds for its --border-*-radius custom
  // props), and the eraser icon is that *span's* previous sibling — one
  // level up from the chevron itself, in its own <svelte-css-wrapper>.
  // The chevron's class ("remove-format-button" — a literal class name in
  // source, not translated UI text) is stable and English-independent;
  // reaching the eraser from it is positional the same way Fields/Cards
  // are found above, just one ancestor hop first.
  function ensureEraserHidden() {
    var chevron = document.querySelector(".remove-format-button");
    if (!chevron) return;
    if (chevron.classList.contains("ba-toolbar-button-hidden")) return;
    chevron.hidden = true;
    chevron.classList.add("ba-toolbar-button-hidden");
    var floatingRef = chevron.closest(".floating-reference");
    var eraserWrapper = floatingRef && floatingRef.previousElementSibling;
    if (eraserWrapper) {
      eraserWrapper.hidden = true;
      eraserWrapper.classList.add("ba-toolbar-button-hidden");
    }
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
    ensureEraserHidden();
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
      ensureEraserHidden();
      if (selected) positionSelection();
      if (crop && (!crop.field.isConnected || !fieldFor(crop.field))) closeCrop();
    }).observe(document.body, { childList: true, subtree: true, attributes: true, attributeFilter: ["class", "contenteditable"] });
  }
  window.__baEditorTools = { clear: function () { hideSelection(); closeCrop(); bookmark = null; } };
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot);
  else boot();
})();
