// Anki Design — note-editor webview script (Add Cards + Browse).
//
// Tags <html data-ba-editor="add|browse"> so addcard.css can scope its
// overrides to the editors we own (never Edit-Current). The mode comes
// from the <meta name="ba-editor-mode"> Python injects, because Anki's
// CSP blocks inline scripts on the editor page, so this work happens in a
// loaded JS file rather than a <script> in the page head.
//
// Toolbar ordering (gear last, notetype first) is CSS-only — see the
// `.item#settings > *` block in addcard.css. It used to be a DOM move
// here, which corrupted Svelte's slot anchors and wiped the attach-media
// button group out of the Browse pane; that comment explains the details.
// Nothing in this file may relocate a node Anki's Svelte owns.
(function () {
  "use strict";
  var MODE = "add";
  try {
    var m = document.querySelector('meta[name="ba-editor-mode"]');
    if (m && m.content) MODE = m.content;
    document.documentElement.dataset.baEditor = MODE;
    var theme = document.querySelector('meta[name="ba-theme"]');
    if (theme && theme.content) {
      document.documentElement.dataset.rfTheme = theme.content;
    }
  } catch (_) {}

  // Toolbar readiness probe. The visual reordering itself is done in CSS;
  // this only reports whether the toolbar has rendered, so the reveal
  // (below) waits for a painted toolbar rather than a bare page.
  function toolbarReady() {
    return !!(
      document.querySelector(".button-toolbar.btn-toolbar") &&
      document.getElementById("settings")
    );
  }

  // Strip the trailing "…"/"..." from the Fields/Cards label-buttons —
  // the editorial chrome reads cleaner without the dots. Anki's source
  // string is "Fields..." / "Cards..." (translated). Both renderings of
  // the ellipsis (three dots vs ellipsis char) are handled.
  function cleanFieldsCardsLabels() {
    var nt = document.getElementById("notetype");
    if (!nt) return false;
    var btns = nt.querySelectorAll(".label-button");
    if (!btns.length) return false;
    btns.forEach(function (b) {
      // The label text lives in a leaf text node; walk to find it.
      var walker = document.createTreeWalker(b, NodeFilter.SHOW_TEXT, null);
      var n;
      while ((n = walker.nextNode())) {
        var txt = n.nodeValue;
        if (!txt) continue;
        var stripped = txt.replace(/[…]+|\.{2,}/g, "").trim();
        if (stripped !== txt) {
          n.nodeValue = stripped;
        }
      }
    });
    // Truthy = setup ran (buttons present) regardless of whether dots
    // were actually stripped this pass. settleAndReveal needs this so
    // the reveal doesn't wait for a "did work" signal that never comes
    // when the labels are already clean from a prior pass.
    return true;
  }

  // Tag editor never collapses — we want the tags input always visible so
  // the closed-state "horrible bare label" never appears. The header text
  // is also reset to "TAGS" so it matches the FRONT/BACK field labels.
  // The field labels and the tag label both use .collapse-label; field
  // labels carry title="Collapse field" / "Expand field", the tag label
  // uses bare "Collapse" / "Expand". Scope strictly to the tag label.
  function keepTagsOpen() {
    var label = document.querySelector(
      '.collapse-label[title="Collapse"], .collapse-label[title="Expand"]'
    );
    if (!label) return false;
    if (label.getAttribute("title") === "Expand") {
      try { label.click(); } catch (_) {}
    }
    label.style.pointerEvents = "none";
    label.style.cursor = "default";
    return true;
  }

  // Move the tag section (label + .collapsible/tag-editor) from its
  // default position OUTSIDE the scroll-area-relative wrapper to INSIDE
  // the .scroll-content container, right after .fields. This pins TAGS
  // visually beneath BACK instead of floating at the bottom of the pane
  // (which is what happens when scroll-area-relative flex-grows to fill).
  // CSS alone couldn't fix this: killing the outer flex-grow collapsed
  // the inner field columns; killing only one collapsed the row width.
  // DOM reparenting sidesteps the whole flex chain.
  function moveTagsIntoFields() {
    var scrollContent = document.querySelector(".scroll-content");
    var tagLabel = document.querySelector(
      '.collapse-label[title="Collapse"], .collapse-label[title="Expand"]'
    );
    if (!scrollContent || !tagLabel) return false;
    // The .collapsible that wraps the tag-editor is the next element
    // sibling after the label (or somewhere nearby — find it by checking
    // each sibling for a descendant .tag-editor).
    var tagCollapsible = null;
    var node = tagLabel.nextElementSibling;
    while (node) {
      if (node.classList && node.classList.contains("collapsible")
          && node.querySelector(".tag-editor")) {
        tagCollapsible = node;
        break;
      }
      node = node.nextElementSibling;
    }
    if (!tagCollapsible) return false;
    // Already moved? Stop polling.
    if (scrollContent.contains(tagLabel)) return true;
    scrollContent.appendChild(tagLabel);
    scrollContent.appendChild(tagCollapsible);
    return true;
  }

  // Stock Anki caps a field image's default (and "shrink to fit") display
  // size at 250x125 (`ImageOverlay maxWidth={250} maxHeight={125}` in
  // ts/routes/editor/NoteEditor.svelte) — a thumbnail meant for old-style
  // "paste a screenshot into a field" workflows. It bears no relation to
  // how big the image actually renders on the card (that's the note type's
  // own CSS, applied only in Preview/Reviewer, never in the editor), so a
  // card whose image is meant to fill most of the card shows as a postage
  // stamp here — you can't judge the real size until you review it.
  //
  // ImageOverlay sets four CSS custom properties as an *inline* style on
  // <html> once, when it mounts (`document.documentElement.style.setProperty`
  // in its <script> block) — not reactively, so there's no store to hook.
  // We can't change the 250/125 constants without touching ts/, but the
  // constants only reach the page as these four custom properties, and
  // inline styles set later win over inline styles set earlier for the
  // same property. Re-setting them after Anki's own script has run raises
  // the cap for every image in every field, in both the "shrunk to fit"
  // state (`--editor-shrink-max-*`) and the freshly-inserted default state
  // (`--editor-default-max-*`) — same mechanism Anki itself uses, just a
  // bigger number. A per-image explicit width/height (or an explicit
  // `data-editor-shrink="false"`, i.e. "Actual size" already chosen) is
  // unaffected: this only changes what "fit to a sane size" means.
  //
  // 640x480 is a rough stand-in for "roughly how big images look once a
  // note type is done constraining them" — most reasonably-sized card
  // images render close to their natural size at that cap, without an
  // oversized paste turning a field into a giant unscrollable image. It
  // isn't the note type's actual CSS (that varies per note type and isn't
  // reachable from this webview), so it's a relative-size improvement,
  // not a pixel-exact match to the reviewer.
  var IMG_DEFAULT_MAX_W = "640px";
  var IMG_DEFAULT_MAX_H = "480px";
  function raiseImageSizeCap() {
    var root = document.documentElement;
    var cur = root.style.getPropertyValue("--editor-default-max-width");
    if (cur === IMG_DEFAULT_MAX_W) return true; // already applied, nothing to redo
    root.style.setProperty("--editor-shrink-max-width", IMG_DEFAULT_MAX_W);
    root.style.setProperty("--editor-shrink-max-height", IMG_DEFAULT_MAX_H);
    root.style.setProperty("--editor-default-max-width", IMG_DEFAULT_MAX_W);
    root.style.setProperty("--editor-default-max-height", IMG_DEFAULT_MAX_H);
    return true;
  }

  function poll(fn, max) {
    if (fn()) return;
    var n = 0;
    var iv = setInterval(function () {
      n += 1;
      if (fn() || n > (max || 30)) clearInterval(iv);
    }, 200);
  }

  // Fade the body in once the DOM has been rearranged — the inline <style>
  // in head starts it at opacity 0 so the FOUC + the toolbar/tag shuffle
  // never reach the user. Reveal as soon as the first pass of each setup
  // function succeeds, or after a hard timeout (so a failed setup never
  // leaves the user staring at a blank pane).
  function reveal() {
    if (document.documentElement.dataset.baReady === "1") return;
    document.documentElement.dataset.baReady = "1";
    // Tell Python the editor body is ready to be revealed — this drops
    // the open-time curtain that addcard_embed.open_inline put up to
    // hide the webview's load-time white flash. Browse has its own
    // curtain logic (keyed off the webview's loadFinished), so the cmd
    // is only meaningful in Add. Wrapped in try since pycmd may not be
    // available in every embedding context.
    if (MODE === "add") {
      try { pycmd("ba:embed-ready"); } catch (_) {}
    }
  }
  function settleAndReveal() {
    var ok = true;
    ok = toolbarReady() && ok;
    ok = cleanFieldsCardsLabels() && ok;
    ok = keepTagsOpen() && ok;
    ok = moveTagsIntoFields() && ok;
    raiseImageSizeCap(); // cosmetic only — never gates reveal
    if (ok) reveal();
    return ok;
  }
  // First pass right after this script runs — if the editor is already
  // ready (often the case), this reveals on the next frame with no flash.
  // Otherwise keep trying until everything succeeds or we hit the cap.
  if (!settleAndReveal()) {
    poll(settleAndReveal, 30);
  }
  // Safety net: 700ms after script start, reveal unconditionally so a
  // partial init never traps the user in a blank pane.
  setTimeout(reveal, 700);

  // Re-apply whenever the toolbar mutates (Anki re-renders on field focus
  // change, notetype switch, etc.).
  try {
    new MutationObserver(function () {
      cleanFieldsCardsLabels();
      keepTagsOpen();
      moveTagsIntoFields();
    }).observe(document.body, { childList: true, subtree: true });
  } catch (_) {}

  // ImageOverlay is a *component*, not a page-load-once script: switching
  // Browse between Preview/Edit re-shows the pane without a page reload,
  // but the editor's Svelte tree (and this fork's NoteEditor instance)
  // gets torn down and remounted, so ImageOverlay's mount effect reruns
  // and stomps our --editor-*-max-* overrides back to 250/125 — confirmed
  // live (measured a field image at 192x125 again after Card→Edit→Card→
  // Edit, despite raiseImageSizeCap() having already run once on initial
  // load). The body-mutation observer above doesn't catch this: Anki
  // writes the reset directly onto <html>'s style attribute, which is
  // outside document.body entirely. Watch that attribute directly and
  // reapply whenever it drifts from ours; raiseImageSizeCap()'s own
  // early-return (cur === IMG_DEFAULT_MAX_W) stops this from looping
  // against its own writes.
  try {
    new MutationObserver(function () {
      raiseImageSizeCap();
    }).observe(document.documentElement, {
      attributes: true,
      attributeFilter: ["style"],
    });
  } catch (_) {}
})();
