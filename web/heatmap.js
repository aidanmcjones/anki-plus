/* Anki Design — heatmap behavior: open on the newest activity (scrolled fully
   right, so the latest days are never cropped), and an instant custom tooltip
   in place of the slow native `title`. Pure progressive enhancement: if this
   never runs the grid is still a valid static heatmap. */
(function () {
  "use strict";

  var TIP_ID = "rf-hm-tip";
  // Per-scroll-element guard; survives deck-browser re-renders (new node = new
  // init) without rebinding the same element twice.
  var seen = (window.__ankiDesignHM = window.__ankiDesignHM || new WeakSet());

  // Module-scope (not per-element): the server re-renders the whole grid as
  // a fresh DOM subtree on every deck-browser refresh, so there's no single
  // persisting node to stash "how many columns did this use to have" on.
  // A plain variable surviving across those re-renders (as long as the page
  // itself isn't reloaded) is what lets us notice "the window just grew by
  // one" and animate only that one case.
  var lastColCount = null;

  function tipEl() {
    var t = document.getElementById(TIP_ID);
    if (!t) {
      t = document.createElement("div");
      t.id = TIP_ID;
      t.className = "rf-hm-tip";
      document.body.appendChild(t);
    }
    return t;
  }

  function countText(n) {
    if (n <= 0) return "No reviews";
    return n === 1 ? "1 review" : n + " reviews";
  }

  function showTip(cell) {
    var n = parseInt(cell.getAttribute("data-count") || "0", 10) || 0;
    var human = cell.getAttribute("data-human") || "";
    var rel = cell.getAttribute("data-rel") || "";
    var peak = cell.getAttribute("data-peak") === "1";

    var html =
      '<div class="rf-hm-tip-n">' + countText(n) + "</div>" +
      '<div class="rf-hm-tip-d">' + human +
      (rel ? ' &middot; <span class="rf-hm-tip-rel">' + rel + "</span>" : "") +
      "</div>";
    if (peak && n > 0)
      html += '<div class="rf-hm-tip-peak">★ Best day so far</div>';

    var t = tipEl();
    t.innerHTML = html;
    t.classList.add("show");

    // Measure, then place centered above the cell, clamped to the viewport.
    var r = cell.getBoundingClientRect();
    var tw = t.offsetWidth;
    var th = t.offsetHeight;
    var left = r.left + r.width / 2 - tw / 2;
    var top = r.top - th - 8;
    if (top < 4) top = r.bottom + 8; // flip below near the top edge
    left = Math.max(4, Math.min(left, window.innerWidth - tw - 4));
    t.style.left = Math.round(left) + "px";
    t.style.top = Math.round(top) + "px";
  }

  function hideTip() {
    var t = document.getElementById(TIP_ID);
    if (t) t.classList.remove("show");
  }

  function onOver(e) {
    var cell = e.target && e.target.closest
      ? e.target.closest(".rf-hm-cell")
      : null;
    if (!cell || cell.classList.contains("rf-hm-empty")) {
      hideTip();
      return;
    }
    showTip(cell);
  }

  function onOut(e) {
    var to = e.relatedTarget;
    if (to && to.closest && to.closest(".rf-hm-cell:not(.rf-hm-empty)")) return;
    hideTip();
  }

  function prefersReducedMotion() {
    try {
      return !!(window.matchMedia &&
        window.matchMedia("(prefers-reduced-motion: reduce)").matches);
    } catch (e) {
      return false;
    }
  }

  // The window can only grow forward in time (or roll once it hits the
  // `heatmap_weeks` cap), so a newly-earned column is always appended on
  // the right. Diff this render's column count against the last one we
  // saw and, if it grew, animate exactly the new trailing column(s) in —
  // "the box expanding out as soon as another column is achieved".
  function animateNewColumns(scroll) {
    var cols = scroll.querySelectorAll(".rf-hm-col");
    var count = cols.length;
    var prev = lastColCount;
    lastColCount = count; // resync every time, animated or not

    // Nothing to animate from on the very first paint this session, when
    // the grid shrank (a config change), or under reduced motion.
    if (prev === null || count <= prev || prefersReducedMotion()) return;

    var delta = count - prev;
    var fresh = Array.prototype.slice.call(cols, count - delta);
    fresh.forEach(function (col) { col.classList.add("rf-hm-col-enter"); });
    // Force a layout flush so the collapsed state above actually commits
    // before we flip to the expanded one below — otherwise both class
    // changes land in the same style recalc and there's nothing to
    // transition between.
    void scroll.offsetWidth;
    requestAnimationFrame(function () {
      fresh.forEach(function (col) { col.classList.add("rf-hm-col-enter-in"); });
    });
    // Cleanup: transitionend would double-fire per animated property, so
    // just sweep once, a beat after the longest transition (260ms) ends.
    setTimeout(function () {
      fresh.forEach(function (col) {
        col.classList.remove("rf-hm-col-enter", "rf-hm-col-enter-in");
      });
    }, 320);
  }

  function init(scroll) {
    if (!scroll || seen.has(scroll)) return;
    seen.add(scroll);
    animateNewColumns(scroll);

    // Default view = newest activity. scrollLeft auto-clamps to the max, so
    // assigning a huge value parks today flush at the right edge. Retried
    // because column widths settle a frame or two after first paint.
    var atRight = true;       // user-controlled: have they scrolled away?
    var userScrolled = false;
    var toRight = function () {
      scroll.scrollLeft = scroll.scrollWidth;
    };
    toRight();
    requestAnimationFrame(toRight);
    setTimeout(toRight, 60);
    setTimeout(toRight, 250);

    // Track whether the user has scrolled left themselves. Once they have,
    // resize won't snap them back — we respect their position.
    var userInitiated = false;
    scroll.addEventListener("scroll", function () {
      hideTip();
      if (userInitiated) {
        var maxLeft = scroll.scrollWidth - scroll.clientWidth;
        atRight = (maxLeft - scroll.scrollLeft) < 4;
      }
    }, { passive: true });
    // Distinguish programmatic scrolls from user scrolls.
    scroll.addEventListener("wheel", function () { userInitiated = true; }, { passive: true });
    scroll.addEventListener("touchstart", function () { userInitiated = true; }, { passive: true });
    scroll.addEventListener("pointerdown", function () { userInitiated = true; }, { passive: true });

    // On window resize, if the user hadn't scrolled away from "today", snap
    // back to the right edge. Today should stay visible when the window
    // narrows.
    function onResize() {
      if (atRight) toRight();
    }
    window.addEventListener("resize", onResize, { passive: true });

    // ResizeObserver catches sidebar-width-driven changes too (the heatmap's
    // own clientWidth changes without a window resize when content reflows).
    try {
      new ResizeObserver(function () {
        if (atRight) toRight();
      }).observe(scroll);
    } catch (e) {}

    scroll.addEventListener("mouseover", onOver);
    scroll.addEventListener("mouseout", onOut);
  }

  function scan() {
    var els = document.querySelectorAll(".rf-hm-scroll");
    for (var i = 0; i < els.length; i++) init(els[i]);
  }

  if (document.readyState !== "loading") scan();
  document.addEventListener("DOMContentLoaded", scan);
  window.addEventListener("load", scan);

  // The deck browser re-renders its content (and the dev watcher hot-reloads
  // it); re-scan when the DOM changes, debounced to one check per frame.
  var pending = false;
  try {
    new MutationObserver(function () {
      if (pending) return;
      pending = true;
      requestAnimationFrame(function () {
        pending = false;
        scan();
      });
    }).observe(document.documentElement, { childList: true, subtree: true });
  } catch (e) {}
})();
