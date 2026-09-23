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
  // One instance per window, one observer. A second copy of the script (a
  // page that pulls it in again) must not add a second observer doing the
  // same work on every mutation; it hands over to the running instance,
  // which re-runs and, if the document has a new <body> (document.open(),
  // as Playwright's setContent and the deck preflight do), moves its one
  // observer there.
  if (window.__rfCardScale && typeof window.__rfCardScale.boot === "function") {
    window.__rfCardScale.boot();
    return;
  }

  var ABS = /^\s*(\d*\.?\d+)(px|pt|rem)\s*$/i;
  var REL = /^\s*(\d*\.?\d+)(em|%)\s*$/i;
  var SCALED = /--rf-deck-scale/;
  // Upper bound on the rules one render may touch. A real note type has a
  // few dozen; anything past this is left at its own size rather than
  // letting one pathological card stall the page before paint.
  var MAX_RULES = 2000;
  var seen = typeof WeakSet === "function" ? new WeakSet() : null;
  var lastScale = null;

  // Cheap stable hash of a <style> element's text, stored on the element so
  // a sheet this script already rewrote is recognised and never walked
  // again (the CSSOM edits below do not change the element's text).
  function hash(text) {
    var h = 5381;
    for (var i = 0; i < text.length; i++) h = ((h << 5) + h + text.charCodeAt(i)) | 0;
    return text.length + ":" + (h >>> 0).toString(36);
  }

  function eachRule(rules, fn, budget) {
    for (var i = 0; i < rules.length && budget.n > 0; i++) {
      var r = rules[i];
      if (r.style && typeof r.selectorText === "string") { budget.n--; fn(r); }
      var inner = null;
      try { inner = r.cssRules; } catch (_) {}
      if (inner && inner.length) eachRule(inner, fn, budget);
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

  function done(style) {
    return style.sheet && (seen ? seen.has(style.sheet)
      : style.getAttribute("data-rf-scaled") === hash(style.textContent || ""));
  }

  function apply() {
    var qa = document.getElementById("qa");
    if (!qa) return;
    var styles = qa.querySelectorAll("style");
    // Only a new card (a <style> sheet not rewritten yet) can change the
    // scale; the other body mutations (progress bar, ease labels) stop here.
    var changed = false;
    for (var j = 0; j < styles.length && !changed; j++) {
      changed = !!styles[j].sheet && !done(styles[j]);
    }
    if (!changed) return;
    var root = document.documentElement;
    var rootPx = parseFloat(getComputedStyle(root).fontSize) || 16;
    var base = 0;
    var budget = { n: MAX_RULES };
    for (var i = 0; i < styles.length; i++) {
      var style = styles[i], sheet = style.sheet;
      if (!sheet) continue;
      var rules;
      try { rules = sheet.cssRules; } catch (_) { continue; }
      var fresh = !done(style);
      eachRule(rules, function (r) {
        var v = r.style.getPropertyValue("font-size");
        if (!v) return;
        var scaled = SCALED.test(v);
        if (isCardSelector(r.selectorText)) {
          // Rewritten earlier: read the original size back out of the calc().
          var orig = scaled && /^calc\(\s*([^*\s]+)\s*\*/.exec(v);
          base = toPx(orig ? orig[1] : v, rootPx) || base;
        }
        // Idempotent: a value that already carries the scale is never
        // wrapped again, whatever marked (or failed to mark) its sheet.
        if (fresh && !scaled && ABS.test(v)) {
          r.style.setProperty("font-size", "calc(" + v.trim() + " * var(--rf-deck-scale, 1))",
            r.style.getPropertyPriority("font-size"));
        }
      }, budget);
      if (seen) seen.add(sheet);
      var h = hash(style.textContent || "");
      if (style.getAttribute("data-rf-scaled") !== h) style.setAttribute("data-rf-scaled", h);
    }
    // #qa carries the reviewer size in styled mode and the note type's own
    // size in native mode, so the scale is 1 there even without the CSS pin.
    var app = parseFloat(getComputedStyle(qa).fontSize) || 0;
    var scale = String(Math.round((app ? app / (base || 16) : 1) * 10000) / 10000);
    // Same card type, same scale: skip the write (and the style recalc).
    if (scale !== lastScale || root.style.getPropertyValue("--rf-deck-scale") !== scale) {
      lastScale = scale;
      root.style.setProperty("--rf-deck-scale", scale);
    }
  }

  // Only mutations that bring a <style> into #qa can need work. Our own
  // edits (CSSOM rules, the data-rf-scaled marker, the html custom
  // property) are not childList mutations, so they never re-enter here.
  function bringsStyle(records) {
    for (var i = 0; i < records.length; i++) {
      // A <style> whose text was replaced in place.
      if (records[i].target && records[i].target.tagName === "STYLE") return true;
      var added = records[i].addedNodes;
      for (var k = 0; k < added.length; k++) {
        var n = added[k];
        if (n.nodeType !== 1) continue;
        if (n.tagName === "STYLE" || n.id === "qa" ||
            (n.querySelector && n.querySelector("style"))) return true;
      }
    }
    return false;
  }

  var observer = null, observed = null;

  function boot() {
    apply();
    if (!window.MutationObserver || !document.body) return;
    if (observed === document.body) return;
    // Anki swaps #qa's contents (card <style> included) on every question
    // and answer; callbacks run before the next paint, so no flash.
    if (observer) observer.disconnect();
    else observer = new MutationObserver(function (records) {
      if (bringsStyle(records)) apply();
    });
    observer.observe(document.body, { childList: true, subtree: true });
    observed = document.body;
  }

  window.__rfCardScale = { boot: boot };

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
