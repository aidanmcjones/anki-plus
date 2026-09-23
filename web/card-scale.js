// Anki Design: scale a note type's own font sizes with the reviewer font size.
//
// With Settings > Reviewer > "Style card content" on, reviewer.css sets .card
// and #qa to the reviewer size (--rf-card-font-size, 28px by default). Text
// that inherits (usually the question) follows it, but any absolute size the
// note type puts on a descendant does not: FunBiochem-E1's
// `.answer { font-size: 17px }` kept the answer at 17px under a 28px question.
// A CSS override cannot fix that for arbitrary note types (any class name, any
// specificity, pseudo-elements like the cloze "[...]" placeholder), so this
// rewrites the card's own <style> rules instead: every px/pt/rem font-size
// becomes `calc(<size> * var(--rf-deck-scale, 1))`, with the scale set to
// reviewer size / note type .card size. Text sized like .card lands on the
// reviewer size, and everything else (sources, table cells, sub/sup) keeps
// its ratio to it. Only the CSSOM is touched, never the card's DOM, so field
// HTML and inline editing are unaffected. reviewer.css pins the scale to 1 in
// native mode, where the note type's sizes are used as-is.
(function () {
  var ABS = /^\s*(\d*\.?\d+)(px|pt|rem)\s*$/i;
  var REL = /^\s*(\d*\.?\d+)(em|%)\s*$/i;
  var seen = typeof WeakSet === "function" ? new WeakSet() : null;

  function eachRule(rules, fn) {
    for (var i = 0; i < rules.length; i++) {
      var r = rules[i];
      if (r.style && typeof r.selectorText === "string") fn(r);
      var inner = null;
      try { inner = r.cssRules; } catch (_) {}
      if (inner && inner.length) eachRule(inner, fn);
    }
  }

  function isCardSelector(sel) {
    return sel.split(",").some(function (s) { return s.trim() === ".card"; });
  }

  // The note type's base size: what its `.card` rule gives body text in
  // stock Anki (16px when it sets none).
  function toPx(value, rootPx) {
    var m = ABS.exec(value);
    if (m) {
      var n = parseFloat(m[1]), u = m[2].toLowerCase();
      return u === "px" ? n : u === "pt" ? n * 4 / 3 : n * rootPx;
    }
    m = REL.exec(value);
    if (m) return parseFloat(m[1]) * (m[2] === "%" ? 0.16 : 16);
    return 0;
  }

  function apply() {
    var qa = document.getElementById("qa");
    if (!qa) return;
    var styles = qa.querySelectorAll("style");
    // Only a new card (a <style> sheet not seen yet) can change the scale;
    // skip the other body mutations (progress bar, ease labels) cheaply.
    var changed = !seen;
    for (var j = 0; j < styles.length && !changed; j++) {
      changed = !!styles[j].sheet && !seen.has(styles[j].sheet);
    }
    if (!changed) return;
    var root = document.documentElement;
    var rootPx = parseFloat(getComputedStyle(root).fontSize) || 16;
    var base = 0;
    for (var i = 0; i < styles.length; i++) {
      var sheet = styles[i].sheet;
      if (!sheet) continue;
      var rules;
      try { rules = sheet.cssRules; } catch (_) { continue; }
      var fresh = !seen || !seen.has(sheet);
      eachRule(rules, function (r) {
        var v = r.style.getPropertyValue("font-size");
        if (!v) return;
        if (isCardSelector(r.selectorText)) {
          // Rewritten earlier: read the original size back out of the calc().
          var orig = /^calc\(\s*([^*\s]+)\s*\*/.exec(v);
          base = toPx(orig ? orig[1] : v, rootPx) || base;
        }
        if (fresh && ABS.test(v)) {
          r.style.setProperty("font-size", "calc(" + v.trim() + " * var(--rf-deck-scale, 1))",
            r.style.getPropertyPriority("font-size"));
        }
      });
      if (seen) seen.add(sheet);
    }
    // #qa carries the reviewer size in styled mode and the note type's own
    // size in native mode, so the scale is 1 there even without the CSS pin.
    var app = parseFloat(getComputedStyle(qa).fontSize) || 0;
    var scale = app ? app / (base || 16) : 1;
    root.style.setProperty("--rf-deck-scale", String(Math.round(scale * 10000) / 10000));
  }

  function boot() {
    apply();
    if (!window.MutationObserver || !document.body) return;
    // Anki swaps #qa's contents (card <style> included) on every question
    // and answer; callbacks run before the next paint, so no flash.
    new MutationObserver(apply).observe(document.body, { childList: true, subtree: true });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
