// Anki Design — Browse tab card-preview pane.
//
// The pane's webview is Anki's own reviewer page (revHtml + reviewer.js),
// so `_showAnswer` does the rendering. Python hands it the *whole* card —
// front and back in one view — so there is no side switch here and no
// click-to-flip: there is nothing to flip to, and the gesture was
// competing with selecting text on the card.
//
// What's left is the tag strip. Everything here is additive to <body>;
// `_showAnswer` only replaces `#qa`'s innerHTML, so the strip survives
// every re-render.
(function () {
  "use strict";

  try {
    document.documentElement.dataset.baPreview = "1";
    var theme = document.querySelector('meta[name="ba-theme"]');
    if (theme && theme.content) {
      document.documentElement.dataset.rfTheme = theme.content;
    }
  } catch (_) {}

  // A side switch left over from a previous version of this file — the
  // pane's webview survives a web/ hot-reload — would sit under the card
  // doing nothing at all.
  try {
    var old = document.getElementById("ba-pv-bar");
    if (old && old.parentNode) old.parentNode.removeChild(old);
  } catch (_) {}

  // Collapsed <details> ("extra info" disclosures some note types wrap
  // around a secondary explanation) default to closed — that's the right
  // call in study, where the disclosure asks a small amount of extra
  // recall of you, but wrong here: this pane's whole premise is "the card
  // as you'll see it, right now, no clicking" (see the file header), and a
  // collapsed triangle reads as unfinished content. Force every <details>
  // in the rendered card open on this pane specifically; native toggling
  // still works afterward if someone wants to re-collapse it while
  // skimming. `#qa`'s innerHTML is replaced wholesale on every re-render
  // (`_showAnswer`), not the element itself, so a MutationObserver on it
  // catches every card without needing a Python-side hook.
  function expandDetails(root) {
    if (!root) return;
    try {
      var list = root.querySelectorAll("details:not([open])");
      for (var i = 0; i < list.length; i++) {
        list[i].setAttribute("open", "");
      }
    } catch (_) {}
  }

  function watchQA() {
    var qa = document.getElementById("qa");
    if (!qa) return;
    expandDetails(qa);
    try {
      var mo = new MutationObserver(function () {
        expandDetails(qa);
      });
      mo.observe(qa, { childList: true, subtree: true });
    } catch (_) {}
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", watchQA);
  } else {
    watchQA();
  }

  function ensureTags() {
    var el = document.getElementById("ba-pv-tags");
    if (el) return el;
    if (!document.body) return null;
    el = document.createElement("div");
    el.id = "ba-pv-tags";
    el.hidden = true;
    document.body.appendChild(el);
    return el;
  }

  function escapeHTML(s) {
    return String(s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  // Called from Python after every render. An untagged note gets no strip
  // at all, rather than an empty rule across the bottom of the pane.
  window.__baPvTags = function (tags) {
    var el = ensureTags();
    if (!el) return;
    if (!tags || !tags.length) {
      el.hidden = true;
      el.innerHTML = "";
      return;
    }
    el.hidden = false;
    el.innerHTML = tags
      .map(function (t) {
        // A nested tag is one tag, not two: keep the path but let the
        // separator recede.
        var parts = escapeHTML(t)
          .split("::")
          .join('<span class="ba-pv-tag-sep">/</span>');
        return '<span class="ba-pv-tag">' + parts + "</span>";
      })
      .join("");
  };

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", ensureTags);
  } else {
    ensureTags();
  }
})();
