// Anki Design — Browse tab card-preview pane.
//
// The pane's webview is Anki's own reviewer page (revHtml + reviewer.js),
// so `_showQuestion` / `_showAnswer` do the rendering and we only add the
// two things a *preview* needs that a review doesn't: a side switch, and
// click-to-flip.
//
// Everything here is additive to <body>; `_showQuestion` only replaces
// `#qa`'s innerHTML, so the bar survives every re-render.
(function () {
  "use strict";

  try {
    document.documentElement.dataset.baPreview = "1";
    var theme = document.querySelector('meta[name="ba-theme"]');
    if (theme && theme.content) {
      document.documentElement.dataset.rfTheme = theme.content;
    }
  } catch (_) {}

  function ensureBar() {
    var bar = document.getElementById("ba-pv-bar");
    if (bar) return bar;
    if (!document.body) return null;
    bar = document.createElement("div");
    bar.id = "ba-pv-bar";
    bar.innerHTML =
      '<button type="button" data-side="question">Question</button>' +
      '<span class="ba-pv-sep"></span>' +
      '<button type="button" data-side="answer">Answer</button>';
    bar.addEventListener("click", function (e) {
      var t = e.target && e.target.closest("button[data-side]");
      if (!t) return;
      e.stopPropagation();
      try {
        pycmd("ba:pv-side:" + t.getAttribute("data-side"));
      } catch (_) {}
    });
    document.body.appendChild(bar);
    return bar;
  }

  // Called from Python after every render so the bar reflects the side
  // actually on screen (and hides itself for the empty state).
  window.__baPvSide = function (side) {
    var bar = ensureBar();
    if (!bar) return;
    bar.hidden = side === "none";
    var btns = bar.querySelectorAll("button[data-side]");
    for (var i = 0; i < btns.length; i++) {
      var on = btns[i].getAttribute("data-side") === side;
      btns[i].classList.toggle("is-on", on);
    }
  };

  // Click the card to flip — the same gesture as click-to-reveal in study.
  document.addEventListener("click", function (e) {
    var t = e.target;
    if (!t || !t.closest) return;
    if (t.closest("#ba-pv-bar")) return;
    if (t.closest("a")) return;
    if (t.closest(".ba-pv-empty")) return;
    // Let text selection through: only a plain click (no drag) flips.
    try {
      if (String(window.getSelection())) return;
    } catch (_) {}
    try {
      pycmd("ba:pv-toggle");
    } catch (_) {}
  });

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", ensureBar);
  } else {
    ensureBar();
  }
})();
