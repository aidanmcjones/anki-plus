/* Anki Design scene shuffle: moves the backdrop's landscape on every few
   seconds.

   The rest of the scene is deliberately script-free: gradients and clip
   paths on `transform` and `opacity`, so a webview that stops painting stops
   the animation for free. Swapping which landscape is up cannot be done that
   way, since it is a discrete choice and not a curve, so this is the one
   timer the backdrop has. Being the one timer, it is also the only place in
   the add-on that can burn CPU behind a hidden window, hence the visibility
   guard below: everything else gets that for nothing, and this must not be
   the exception that costs it.

   Python has already stamped `data-rf-scene` on <html> with the day's scene
   (see `_scene_variant`). This only ever moves it on from there, so if the
   script never loads, is blocked, or throws, the page still shows a correct
   and complete scene. It is decoration on decoration.

   Deck browser and overview only. The reviewer has no backdrop and no
   motion, on purpose. */
(function () {
  "use strict";

  // The deck browser re-renders its whole document on any collection op, and
  // the congrats path re-evaluates the page scripts into a document that is
  // already live. Either can run this file twice; two intervals on one
  // <html> would swap the scene at irregular doubled-up intervals and only
  // one of them would ever be cleared.
  if (window.__ankiDesignSceneShuffle) return;
  window.__ankiDesignSceneShuffle = true;

  // Only reached when the payload predates the scene list, e.g. a webview
  // rendered by an older version still open behind a reload. `__init__.py`'s
  // `_SCENES` is the source of truth; a name out of step with it falls
  // through to the base silhouettes rather than breaking anything.
  var FALLBACK_SCENES = [
    "peaks", "dunes", "forest", "canyon", "lake",
    "volcano", "isles", "tundra", "spires", "ruins"
  ];
  var FALLBACK_SECONDS = 10;

  var root = document.documentElement;
  var opts = (window.__baOpts && window.__baOpts.scene) || {};

  // A list Python sent is authoritative even when it is short: one entry is
  // how it says "the user pinned this scene", and there is then nothing to
  // cycle. Only an absent or malformed list falls back.
  var scenes = Array.isArray(opts.list) && opts.list.length
    ? opts.list.slice()
    : FALLBACK_SCENES.slice();
  if (scenes.length < 2) return;

  var seconds = parseInt(opts.shuffleSeconds, 10);
  if (!isFinite(seconds) || seconds < 2) seconds = FALLBACK_SECONDS;
  var period = seconds * 1000;

  // Shuffled rather than walked in order, because a fixed cycle makes the
  // whole rotation predictable after one sitting: peaks, then dunes, then
  // forest, every session, forever. A fresh permutation plus a random entry
  // point costs nothing and means the next scene is never the one you have
  // learned to expect.
  var order = scenes;
  for (var i = order.length - 1; i > 0; i--) {
    var j = Math.floor(Math.random() * (i + 1));
    var swap = order[i];
    order[i] = order[j];
    order[j] = swap;
  }
  var at = Math.floor(Math.random() * order.length);

  var timer = null;
  var still = false;

  function advance() {
    at = (at + 1) % order.length;
    // A random entry point lands on the scene already showing about one time
    // in ten, which reads as the rotation having stalled. Stepping past it is
    // cheaper than seeding the index from the current scene, which would
    // have to cope with a pinned or unknown name being up.
    if (order[at] === root.getAttribute("data-rf-scene")) {
      at = (at + 1) % order.length;
    }
    try {
      root.setAttribute("data-rf-scene", order[at]);
    } catch (e) {}
  }

  function stop() {
    if (timer !== null) {
      clearInterval(timer);
      timer = null;
    }
  }

  function start() {
    if (timer !== null || still || document.hidden) return;
    timer = setInterval(advance, period);
  }

  function onVisibility() {
    if (document.hidden) stop();
    else start();
  }

  function teardown() {
    stop();
    document.removeEventListener("visibilitychange", onVisibility);
    window.removeEventListener("pagehide", teardown);
    window.removeEventListener("beforeunload", teardown);
    if (classWatch) {
      classWatch.disconnect();
      classWatch = null;
    }
    if (quiet) {
      if (quiet.removeEventListener) {
        quiet.removeEventListener("change", applyMotionPref);
      } else if (quiet.removeListener) {
        quiet.removeListener(applyMotionPref);
      }
    }
  }

  // Reduced motion means the first scene stays up: the landscape is still a
  // landscape, just a fixed one, which is the same bargain scene.css makes
  // for the drifts. Watched rather than read once, since the OS setting can
  // change under a window that is open for hours and `matchMedia` is a
  // passive listener with no cost until it fires.
  var quiet = null;
  try {
    quiet = window.matchMedia("(prefers-reduced-motion: reduce)");
  } catch (e) {}

  // Anki's own Reduce motion preference, which is a different signal from the
  // OS one and only consulted when `backdrop_motion` is "auto". It lives as a
  // class on body, and Anki toggles it with an eval into the LIVE document
  // (aqt/webview.py on_body_classes_need_update, reached from the
  // body_classes_need_update hook), so it can flip while this page is open.
  // Reading it once at startup would also race that eval on a cold load.
  // Hence the observer rather than a single read.
  function ankiQuiet() {
    if (opts.motion !== "auto") return false;
    try {
      return !!(document.body &&
        document.body.classList.contains("reduce-motion"));
    } catch (e) {
      return false;
    }
  }

  function applyMotionPref() {
    still = !!(quiet && quiet.matches) || ankiQuiet();
    if (still) stop();
    else start();
  }

  if (quiet) {
    if (quiet.addEventListener) quiet.addEventListener("change", applyMotionPref);
    else if (quiet.addListener) quiet.addListener(applyMotionPref);
  }

  var classWatch = null;
  if (opts.motion === "auto") {
    try {
      classWatch = new MutationObserver(applyMotionPref);
      classWatch.observe(document.body, {
        attributes: true,
        attributeFilter: ["class"],
      });
    } catch (e) {}
  }

  document.addEventListener("visibilitychange", onVisibility);
  window.addEventListener("pagehide", teardown);
  window.addEventListener("beforeunload", teardown);

  applyMotionPref();
})();
