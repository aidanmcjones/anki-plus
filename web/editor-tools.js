// Image and text tools for Add, Browse, Edit Current and in-place study editing.
(function () {
  "use strict";
  if (window.__baEditorTools) return;
  var selected = null, selectedRef = null, selectionBox, menu, toolbar, bookmark = null, drag = null;
  var dialog = null, crop = null;
  var imageObserver = new MutationObserver(function () { if (selected) positionSelection(); });
  var editorPage = !!document.querySelector('meta[name="ba-editor-tools"]');
  // "add" | "browse" | "" (Edit Current, which addcard.js leaves stock —
  // see its own header comment). Card-list arrow/button navigation only
  // makes sense in Browse: Add Cards has no list of existing cards to
  // move through.
  var editorModeMeta = document.querySelector('meta[name="ba-editor-mode"]');
  var editorMode = editorModeMeta ? editorModeMeta.content : "";

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
    // The real editor (Add/Browse/Edit Current) gets uniform icon-button
    // controls folded into the icon toolbar instead — see ensureHotbar().
    // This full-width row stays only for the reviewer's inline quick
    // editor, a smaller, simpler surface where a dedicated row is the
    // right call.
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
  // One unified hotbar, every control the same icon-button chrome — the
  // user explicitly rejected "three pop-ups and two different-looking
  // button types" (a QDialog for styling, a proxy-click to the stock
  // Fields dialog, a wide text input for Size) in favor of "a document
  // editor hotbar. They all have the same formatting." Fields.../Cards...
  // are still two standalone stock buttons we fold away the same way as
  // before (hidden, never removed — addcard.js's own comment about that
  // still applies), but nothing here proxy-clicks either of them anymore,
  // and nothing here opens a QDialog or any other window: every popover
  // is inline, in this same pane, built from plain divs styled to match.
  // (An earlier round kept one explicit "Advanced: edit raw template"
  // button proxying to the stock Cards/CardLayout dialog; the user asked
  // for it gone outright — "no one would ever use that" — so raw
  // template editing is only reachable through Anki's own Tools -> Manage
  // Note Types now, entirely outside this pane.)
  var hotbarPopovers = [];
  function closeAllHotbarPopovers(except) {
    hotbarPopovers.forEach(function (p) { if (p !== except) p.close(); });
  }
  // Every hotbar control shares this shell: a button with the hotbar's
  // uniform chrome (.ba-hotbar-btn — same box, border and hover as every
  // other control here) plus a position:fixed popover under it. Content
  // is filled in by the caller; open/close/positioning is identical for
  // all of them, which is what makes them look and behave like one
  // family of controls instead of ad hoc pop-ups.
  function buildHotbarControl(container, idBase, label, title, ariaLabel) {
    var wrap = document.createElement("span");
    wrap.className = "ba-hotbar-item";
    wrap.innerHTML = '<button type="button" id="' + idBase + '-btn" class="ba-hotbar-btn" aria-haspopup="true" aria-expanded="false" title="' + escapeAttr(title) + '" aria-label="' + escapeAttr(ariaLabel || title) + '">' + label + '</button>'
      + '<div id="' + idBase + '-popover" class="ba-popover" role="dialog" aria-label="' + escapeAttr(title) + '" hidden></div>';
    container.appendChild(wrap);
    var btn = wrap.querySelector("#" + idBase + "-btn");
    var popover = wrap.querySelector("#" + idBase + "-popover");
    var api = {
      close: function () {
        popover.hidden = true;
        btn.setAttribute("aria-expanded", "false");
      },
      open: function () {
        closeAllHotbarPopovers(api);
        popover.hidden = false;
        btn.setAttribute("aria-expanded", "true");
        positionPopup(popover, btn);
      },
    };
    btn.addEventListener("click", function (e) {
      e.stopPropagation();
      if (popover.hidden) api.open(); else api.close();
    });
    document.addEventListener("click", function (e) {
      if (!popover.hidden && !wrap.contains(e.target)) api.close();
    });
    document.addEventListener("keydown", function (e) {
      if (e.key === "Escape" && !popover.hidden) { e.preventDefault(); api.close(); }
    }, true);
    hotbarPopovers.push(api);
    return { btn: btn, popover: popover, open: api.open, close: api.close };
  }

  function buildFontControl(container) {
    // Distinct accessible names for the button/popover vs. the search
    // input inside it — sharing "Font family" across all three made them
    // ambiguous to anything (a test, a screen reader) selecting by
    // accessible name alone; only the actual combobox should answer to it.
    var c = buildHotbarControl(container, "ba-font", "Aa", "Font picker", "Open font picker");
    c.popover.innerHTML = '<input type="text" id="ba-font-search" placeholder="Search fonts" aria-label="Font family" role="combobox" aria-autocomplete="list" aria-expanded="false" autocomplete="off" spellcheck="false">'
      + '<div id="ba-font-dropdown" role="listbox" aria-label="Font suggestions" hidden></div>';
    var input = c.popover.querySelector("#ba-font-search");
    var dropdown = c.popover.querySelector("#ba-font-dropdown");
    attachFontCombobox(input, dropdown, "ba-font", function (f) { format("fontName", f); });
    c.btn.addEventListener("click", function () {
      if (!c.popover.hidden) { input.value = ""; input.focus(); }
    });
  }

  // Size used to be an always-visible wide number input — visually
  // nothing like a B/I/U icon button. Now it's the same button chrome as
  // every other hotbar control; the popover it pops holds a compact
  // +/- stepper plus a small number field, styled as one unit.
  function buildSizeControl(container) {
    // Distinct accessible names — only the number field itself answers to
    // "Text size in pixels"; the trigger button is described separately.
    var c = buildHotbarControl(container, "ba-size", "–+", "Text size", "Open text size");
    c.popover.classList.add("ba-size-popover");
    c.popover.innerHTML = '<div class="ba-stepper">'
      + '<button type="button" class="ba-hotbar-btn" data-step="-1" aria-label="Decrease text size">−</button>'
      + '<input type="number" id="ba-size-value" aria-label="Text size in pixels" min="6" max="300" step="1" value="16">'
      + '<button type="button" class="ba-hotbar-btn" data-step="1" aria-label="Increase text size">+</button>'
      + '</div>';
    var input = c.popover.querySelector("#ba-size-value");
    function apply() {
      var px = parseInt(input.value, 10);
      if (px) applyFontSizePx(px);
    }
    c.popover.querySelectorAll("[data-step]").forEach(function (b) {
      b.addEventListener("click", function () {
        var delta = Number(b.dataset.step);
        var next = Math.max(6, Math.min(300, (parseInt(input.value, 10) || 16) + delta));
        input.value = String(next);
        apply();
      });
    });
    input.addEventListener("change", apply);
    input.addEventListener("keydown", function (e) { if (e.key === "Enter") { e.preventDefault(); apply(); } });
  }

  // Fields: add/rename/delete note type fields inline — "a button for
  // fields... which can give you a dropdown and allow you to add
  // fields." Schema changes route through card_styling.py's
  // ba:fields:mutate (col.models.add_field/rename_field/remove_field,
  // the same primitives the stock Fields dialog itself uses), never
  // through the stock dialog — that dialog must never appear from here.
  function buildFieldsControl(container) {
    var c = buildHotbarControl(container, "ba-fields", "Fields", "Fields", "Open fields");
    c.popover.classList.add("ba-fields-popover");
    c.popover.innerHTML = '<div class="ba-fields-list" role="list"></div>'
      + '<div class="ba-fields-add">'
      + '<input type="text" id="ba-field-new" placeholder="New field name" aria-label="New field name" autocomplete="off">'
      + '<button type="button" class="ba-hotbar-btn" id="ba-field-add-btn" aria-label="Add field">+</button>'
      + '</div>'
      + '<div class="ba-fields-error" role="alert" hidden></div>';
    var list = c.popover.querySelector(".ba-fields-list");
    var newInput = c.popover.querySelector("#ba-field-new");
    var addBtn = c.popover.querySelector("#ba-field-add-btn");
    var errorEl = c.popover.querySelector(".ba-fields-error");

    function showError(msg) {
      errorEl.textContent = msg || "";
      errorEl.hidden = !msg;
    }

    function render(fields) {
      list.innerHTML = "";
      fields.forEach(function (f) {
        var row = document.createElement("div");
        row.className = "ba-field-row";
        row.setAttribute("role", "listitem");
        // Display pretty (word-spaced), edit raw: the rename input below
        // still seeds from f.name untouched, and mutate() always sends
        // the real CamelCase name — only this label's text is cosmetic.
        var displayName = splitCamelCase(f.name);
        var nameEl = document.createElement("span");
        nameEl.className = "ba-field-name";
        nameEl.tabIndex = 0;
        nameEl.setAttribute("role", "button");
        nameEl.setAttribute("aria-label", "Rename " + displayName);
        nameEl.textContent = displayName;
        var deleteBtn = document.createElement("button");
        deleteBtn.type = "button";
        deleteBtn.className = "ba-field-delete";
        deleteBtn.setAttribute("aria-label", "Delete " + displayName);
        deleteBtn.textContent = "Delete";
        row.appendChild(nameEl);
        row.appendChild(deleteBtn);
        list.appendChild(row);

        function startRename() {
          var input = document.createElement("input");
          input.type = "text";
          input.value = f.name;
          input.className = "ba-field-rename-input";
          input.setAttribute("aria-label", "Rename field " + f.name);
          row.replaceChild(input, nameEl);
          input.focus();
          input.select();
          var committed = false;
          function commit() {
            if (committed) return;
            committed = true;
            var newName = input.value.trim();
            if (!newName || newName === f.name) { refresh(); return; }
            mutate("rename", { ord: f.ord, newName: newName });
          }
          input.addEventListener("keydown", function (e) {
            if (e.key === "Enter") { e.preventDefault(); commit(); }
            else if (e.key === "Escape") { e.preventDefault(); committed = true; refresh(); }
          });
          input.addEventListener("blur", commit);
        }
        nameEl.addEventListener("click", startRename);
        nameEl.addEventListener("keydown", function (e) { if (e.key === "Enter") startRename(); });

        // Inline click-again-to-confirm — no dialog. A stray click just
        // arms it; a click anywhere else (popover close, Escape) resets
        // via the next render() from a fresh open.
        deleteBtn.addEventListener("click", function () {
          if (!deleteBtn.classList.contains("ba-armed")) {
            deleteBtn.classList.add("ba-armed");
            deleteBtn.textContent = "Confirm?";
            return;
          }
          mutate("delete", { ord: f.ord });
        });
      });
    }

    function refresh() {
      if (typeof pycmd !== "function") return;
      pycmd("ba:fields:get", function (res) {
        if (res && res.error) { showError(res.error); return; }
        showError("");
        render((res && res.fields) || []);
      });
    }
    function mutate(op, extra) {
      if (typeof pycmd !== "function") return;
      var payload = Object.assign({ op: op }, extra);
      pycmd("ba:fields:mutate:" + JSON.stringify(payload), function (res) {
        if (res && res.error) { showError(res.error); return; }
        showError("");
        render((res && res.fields) || []);
      });
    }

    addBtn.addEventListener("click", function () {
      var name = newInput.value.trim();
      if (!name) return;
      mutate("add", { name: name });
      newInput.value = "";
    });
    newInput.addEventListener("keydown", function (e) {
      if (e.key === "Enter") { e.preventDefault(); addBtn.click(); }
    });

    c.btn.addEventListener("click", function () {
      if (!c.popover.hidden) refresh();
    });
  }

  // Card styling: font/base size/answer size/alignment/image max-height,
  // written into the note type's CSS via the same safe, idempotent
  // override-block writer as before (card_styling.py's apply_styling) —
  // only the chrome moved, from a QDialog to this popover. Explicit
  // Apply/Reset (not live-as-you-type) since this changes every card of
  // the note type, not just the current selection.
  function buildStylingControl(container) {
    var c = buildHotbarControl(container, "ba-styling", "Style", "Card styling", "Open card styling");
    c.popover.classList.add("ba-styling-popover");
    var aligns = [["left", "Left"], ["center", "Center"], ["right", "Right"]];
    c.popover.innerHTML = '<div class="ba-styling-field"><label for="ba-style-font">Font</label>'
      + '<div class="ba-font-combo"><input type="text" id="ba-style-font" placeholder="Card default" aria-label="Card font family" role="combobox" autocomplete="off" spellcheck="false">'
      + '<div id="ba-style-font-dropdown" role="listbox" aria-label="Font suggestions" hidden></div></div></div>'
      + '<div class="ba-styling-field"><label>Base size</label><div class="ba-stepper">'
      + '<button type="button" class="ba-hotbar-btn" data-step="base:-1">−</button>'
      + '<input type="number" id="ba-style-base" min="6" max="300" step="1" aria-label="Base text size">'
      + '<button type="button" class="ba-hotbar-btn" data-step="base:1">+</button></div></div>'
      + '<div class="ba-styling-field"><label>Answer size</label><div class="ba-stepper">'
      + '<button type="button" class="ba-hotbar-btn" data-step="answer:-1">−</button>'
      + '<input type="number" id="ba-style-answer" min="6" max="300" step="1" aria-label="Answer text size">'
      + '<button type="button" class="ba-hotbar-btn" data-step="answer:1">+</button></div></div>'
      + '<div class="ba-styling-field"><label>Alignment</label><div class="ba-align-group" role="group" aria-label="Text alignment">'
      + aligns.map(function (a) { return '<button type="button" class="ba-hotbar-btn" data-align="' + a[0] + '">' + a[1] + '</button>'; }).join("")
      + '</div></div>'
      + '<div class="ba-styling-field"><label>Image max height</label><div class="ba-stepper">'
      + '<button type="button" class="ba-hotbar-btn" data-step="image:-20">−</button>'
      + '<input type="number" id="ba-style-image" min="20" max="4000" step="1" aria-label="Image max height">'
      + '<button type="button" class="ba-hotbar-btn" data-step="image:20">+</button></div></div>'
      + '<div class="ba-styling-error" role="alert" hidden></div>'
      + '<div class="ba-styling-actions">'
      + '<button type="button" class="ba-hotbar-btn" id="ba-style-reset">Reset</button>'
      + '<button type="button" class="ba-hotbar-btn ba-primary" id="ba-style-apply">Apply</button>'
      + '</div>';
    var fontInput = c.popover.querySelector("#ba-style-font");
    var fontDropdown = c.popover.querySelector("#ba-style-font-dropdown");
    var baseInput = c.popover.querySelector("#ba-style-base");
    var answerInput = c.popover.querySelector("#ba-style-answer");
    var imageInput = c.popover.querySelector("#ba-style-image");
    var alignBtns = c.popover.querySelectorAll("[data-align]");
    var errorEl = c.popover.querySelector(".ba-styling-error");
    var currentAlign = "center";

    attachFontCombobox(fontInput, fontDropdown, "ba-style-font", function (f) { fontInput.value = f; });

    function showError(msg) {
      errorEl.textContent = msg || "";
      errorEl.hidden = !msg;
    }
    function setAlign(value) {
      currentAlign = value;
      alignBtns.forEach(function (b) { b.classList.toggle("active", b.dataset.align === value); });
    }
    alignBtns.forEach(function (b) {
      b.addEventListener("click", function () { setAlign(b.dataset.align); });
    });
    c.popover.querySelectorAll("[data-step]").forEach(function (b) {
      b.addEventListener("click", function () {
        var parts = b.dataset.step.split(":");
        var target = parts[0] === "base" ? baseInput : parts[0] === "answer" ? answerInput : imageInput;
        var delta = Number(parts[1]);
        var min = Number(target.min), max = Number(target.max);
        var next = Math.max(min, Math.min(max, (parseInt(target.value, 10) || min) + delta));
        target.value = String(next);
      });
    });

    function refresh() {
      if (typeof pycmd !== "function") return;
      pycmd("ba:styling:get", function (res) {
        if (!res || res.error) { showError(res && res.error); return; }
        showError("");
        fontInput.value = res.font || "";
        baseInput.value = res.base_size;
        answerInput.value = res.answer_size;
        imageInput.value = res.image_max_height;
        setAlign(res.align || "center");
      });
    }
    c.popover.querySelector("#ba-style-apply").addEventListener("click", function () {
      if (typeof pycmd !== "function") return;
      var payload = {
        font: fontInput.value.trim(),
        base_size: parseInt(baseInput.value, 10) || 20,
        answer_size: parseInt(answerInput.value, 10) || 24,
        align: currentAlign,
        image_max_height: parseInt(imageInput.value, 10) || 500,
      };
      pycmd("ba:styling:apply:" + JSON.stringify(payload), function (res) {
        if (res && res.error) { showError(res.error); return; }
        showError("");
        c.close();
      });
    });
    c.popover.querySelector("#ba-style-reset").addEventListener("click", function () {
      if (typeof pycmd !== "function") return;
      pycmd("ba:styling:reset", function (res) {
        if (res && res.error) { showError(res.error); return; }
        refresh();
      });
    });

    c.btn.addEventListener("click", function () {
      if (!c.popover.hidden) refresh();
    });
  }

  // Arrow-key / ‹› button navigation through the Cards-view table, for
  // the full-screen editor (table collapsed — see browse_embed.py's
  // _set_table_collapsed). One function, two triggers, per the user's
  // ask; both funnel through browse_embed.navigate_card, which moves
  // Table's own row cursor rather than loading a card ourselves — same
  // save semantics as clicking another row, not a second implementation
  // of them.
  var navPrevBtn = null, navNextBtn = null;
  function isTypingContext() {
    var active = document.activeElement;
    while (active && active.shadowRoot && active.shadowRoot.activeElement) {
      active = active.shadowRoot.activeElement;
    }
    if (!active) return false;
    var tag = active.tagName;
    if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") return true;
    return !!active.isContentEditable;
  }
  function updateNavButtons(hasPrevious, hasNext) {
    if (navPrevBtn) navPrevBtn.disabled = !hasPrevious;
    if (navNextBtn) navNextBtn.disabled = !hasNext;
  }
  function flashNavEdge(btn) {
    if (!btn) return;
    btn.classList.add("ba-nav-edge");
    setTimeout(function () { btn.classList.remove("ba-nav-edge"); }, 220);
  }
  function navigateCard(direction, triggerBtn) {
    if (editorMode !== "browse" || typeof pycmd !== "function") return;
    pycmd("ba:cards:nav:" + direction, function (res) {
      if (!res || res.error) return;
      if (res.atEnd) {
        flashNavEdge(triggerBtn || (direction === "prev" ? navPrevBtn : navNextBtn));
        updateNavButtons(res.hasPrevious, res.hasNext);
        return;
      }
      updateNavButtons(res.hasPrevious, res.hasNext);
      // The loaded card's id is already visible via the editor's own
      // note-load rendering (field values updating is the position
      // signal — cheap and always accurate, no separate counter to keep
      // in sync).
    });
  }

  function ensureHotbar() {
    if (!editorPage) return;
    var nt = document.getElementById("notetype");
    if (!nt) return;
    if (document.getElementById("ba-hotbar")) return;
    // Positional, not text-matched: NotetypeButtons.svelte always renders
    // Fields then Cards, in that order — matching by English text prefix
    // would silently break on any non-English locale (the button text is
    // tr.editingFields()/tr.editingCards(), translated), and unlike the
    // hide-only cleanFieldsCardsLabels() in addcard.js, a failed match
    // here wouldn't just leave dots on a label: with the CSS-level
    // `#notetype .label-button { display: none !important; }` rule in
    // editor-tools.css doing the actual hiding (so there's no first-paint
    // flash while this function waits on the MutationObserver), a bug
    // here would hide Fields/Cards with no replacement ever appearing —
    // a real functionality loss, not just a cosmetic one.
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

    var hotbar = document.createElement("span");
    hotbar.id = "ba-hotbar";
    hotbar.className = "ba-hotbar";
    nt.appendChild(hotbar);

    // ‹ › card-list navigation, same chrome as every other hotbar
    // button. Browse only (editorMode) — Add Cards has no card list to
    // move through. Shown whenever the pane is Browse's editor, not
    // gated to the table-collapsed/full-screen state specifically: the
    // card list stays reachable by click either way, so there's no
    // layout reason to hide these only some of the time, and a
    // consistent toolbar is simpler than one that gains/loses buttons
    // as the table pane is dragged open or closed.
    if (editorMode === "browse") {
      navPrevBtn = document.createElement("button");
      navPrevBtn.type = "button";
      navPrevBtn.className = "ba-hotbar-btn";
      navPrevBtn.id = "ba-nav-prev-btn";
      navPrevBtn.title = "Previous card";
      navPrevBtn.setAttribute("aria-label", "Previous card");
      navPrevBtn.textContent = "‹";
      navPrevBtn.addEventListener("click", function (e) {
        e.stopPropagation();
        navigateCard("prev", navPrevBtn);
      });
      hotbar.appendChild(navPrevBtn);
    }

    buildFieldsControl(hotbar);
    buildStylingControl(hotbar);
    buildFontControl(hotbar);
    buildSizeControl(hotbar);
    // No "..." / Advanced button — the user explicitly asked for it gone
    // ("not necessary, no one would ever use that"). cardsBtn is still
    // hidden above, same as fieldsBtn, so the stock Cards button never
    // shows in the toolbar; raw template editing is still reachable
    // through Anki's own Tools -> Manage Note Types, unrelated to this
    // pane, which is fine — nothing here needs to proxy to it anymore.

    if (editorMode === "browse") {
      navNextBtn = document.createElement("button");
      navNextBtn.type = "button";
      navNextBtn.className = "ba-hotbar-btn";
      navNextBtn.id = "ba-nav-next-btn";
      navNextBtn.title = "Next card";
      navNextBtn.setAttribute("aria-label", "Next card");
      navNextBtn.textContent = "›";
      navNextBtn.addEventListener("click", function (e) {
        e.stopPropagation();
        navigateCard("next", navNextBtn);
      });
      hotbar.appendChild(navNextBtn);
    }
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

  // "QuestionImage" renders, via addcard.css's text-transform:uppercase
  // on .label-name, as "QUESTIONIMAGE" — capitalization alone never
  // inserts a word break. Field names are real schema (deck-building
  // automation and templates depend on the exact CamelCase — see
  // card_styling.py's field mutations), so this only ever rewrites what
  // lands on screen: the .label-name text node here, and the Fields
  // popover's list below. Nothing here changes the underlying name;
  // rename/add still read and write the raw value.
  function splitCamelCase(name) {
    return String(name)
      .replace(/([a-z0-9])([A-Z])/g, "$1 $2")
      .replace(/([A-Za-z])(\d)/g, "$1 $2")
      .replace(/(\d)([A-Za-z])/g, "$1 $2");
  }
  function ensureFieldLabelsSpaced() {
    if (!editorPage) return;
    document.querySelectorAll(".label-container .label-name").forEach(function (el) {
      var spaced = splitCamelCase(el.textContent);
      if (spaced !== el.textContent) el.textContent = spaced;
    });
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
    // Growth is capped at the first ancestor with real room. A shrink-to-fit parent (flex
    // item, inline-block) is exactly as wide as the image, which would cap growth at the
    // current size: the image could shrink but never grow.
    var container = selected.parentElement;
    while (container && container !== document.body && container.clientWidth <= r.width + 2)
      container = container.parentElement;
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
    ensureHotbar();
    ensureEraserHidden();
    ensureFieldLabelsSpaced();
    if (editorMode === "browse") {
      // Bare arrows, gated on focus rather than a modifier: this is the
      // same key the card LIST itself already uses to move between rows
      // (Table.to_previous_row/to_next_row are bound to Up/Down there
      // too), so it matches what a user already knows rather than
      // introducing a second convention. isTypingContext() is what keeps
      // this from hijacking the caret in a field, the Fields-popover
      // rename input, the font search box, the size stepper, etc. — bare
      // arrows only navigate cards when focus is nowhere any of those
      // are, which in practice means the editor chrome itself (a hotbar
      // button, empty space) has focus or nothing does.
      document.addEventListener("keydown", function (e) {
        if (isTypingContext()) return;
        if (e.key === "ArrowUp" || e.key === "ArrowLeft") {
          e.preventDefault();
          navigateCard("prev");
        } else if (e.key === "ArrowDown" || e.key === "ArrowRight") {
          e.preventDefault();
          navigateCard("next");
        }
      }, true);
    }
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
      ensureHotbar();
      ensureEraserHidden();
      ensureFieldLabelsSpaced();
      if (selected) positionSelection();
      if (crop && (!crop.field.isConnected || !fieldFor(crop.field))) closeCrop();
    }).observe(document.body, { childList: true, subtree: true, attributes: true, attributeFilter: ["class", "contenteditable"] });
  }
  window.__baEditorTools = { clear: function () { hideSelection(); closeCrop(); bookmark = null; } };
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot);
  else boot();
})();
