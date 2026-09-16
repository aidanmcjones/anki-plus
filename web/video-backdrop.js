/* Anki Design nature-video backdrop: rotates between clips on a timer,
   crossfading between two stacked <video> elements so the swap never
   shows a black flash.

   Modeled on scene-shuffle.js — same guard-against-double-injection, same
   visibility/reduced-motion contract — but with an extra job scene-
   shuffle.js never had: real media has a network cost and can fail to
   load. So this file also pre-buffers the *next* clip into the hidden
   element well before it's needed (rather than starting that fetch at the
   moment of the crossfade), and tracks which clips 404 or error so a
   broken file gets skipped once and never retried.

   Python has already set `.ba-video-a`'s `src` to a deterministically
   (date-)picked clip and started it via the `autoplay` attribute — see
   `_video_html` / `_video_state` in `__init__.py` — so the first paint
   already has something playing. This only ever takes it from there.

   Deck browser and overview only, same as the scene backdrop: the
   reviewer has no backdrop and no motion, on purpose. */
(function () {
  "use strict";

  if (window.__ankiDesignVideoBackdrop) return;
  window.__ankiDesignVideoBackdrop = true;

  var opts = (window.__baOpts && window.__baOpts.video) || {};
  var videos = Array.isArray(opts.videos) ? opts.videos.slice() : [];
  // An empty list here means the server already decided there was nothing
  // to play (see the "video" branch in `on_webview_will_set_content`) and
  // injected no `.ba-video` markup at all — so there is nothing for this
  // script to do. It is not this script's job to second-guess that.
  if (videos.length === 0) return;

  var elA = document.querySelector(".ba-video-a");
  var elB = document.querySelector(".ba-video-b");
  if (!elA || !elB) return;

  var MIN_ROTATE_SECONDS = 5;

  var rotateSeconds = parseInt(opts.rotateSeconds, 10);
  if (!isFinite(rotateSeconds) || rotateSeconds < 0) rotateSeconds = 0;
  if (rotateSeconds > 0 && rotateSeconds < MIN_ROTATE_SECONDS) {
    rotateSeconds = MIN_ROTATE_SECONDS;
  }

  // Shuffled rather than walked in index order — same reasoning as
  // scene-shuffle.js's `order`: a fixed cycle becomes predictable after
  // one sitting.
  var order = videos.slice();
  for (var i = order.length - 1; i > 0; i--) {
    var j = Math.floor(Math.random() * (i + 1));
    var tmp = order[i];
    order[i] = order[j];
    order[j] = tmp;
  }
  // One clip is "play its own loop forever" by definition — nothing to
  // rotate to, so the timer never gets armed regardless of the config
  // value (mirrors scene-shuffle.js bailing when its own list is short).
  if (order.length < 2) rotateSeconds = 0;

  // Find the clip Python already put on screen, so the first advance()
  // treats it as "already showing" instead of announcing a change to the
  // clip that's already there.
  var initialSrc = elA.getAttribute("src") || "";
  var at = 0;
  for (var k = 0; k < order.length; k++) {
    if (order[k].url === initialSrc) {
      at = k;
      break;
    }
  }

  var front = elA;
  var back = elB;
  front.classList.add("ba-video-front");

  var badFiles = Object.create(null);

  function playQuiet(el) {
    try {
      var p = el.play();
      if (p && p.catch) p.catch(function () {});
    } catch (e) {}
  }

  function prepare(el, entry) {
    if (!entry) return;
    if (el.getAttribute("src") === entry.url) return; // already loaded
    try {
      el.pause();
      el.setAttribute("src", entry.url);
      el.load();
    } catch (e) {}
    playQuiet(el);
  }

  // Next order[] index, from `fromIdx`, that isn't a known-bad file.
  // Wraps at most once around the whole list; -1 means every clip in the
  // library has failed.
  function nextPlayableIndex(fromIdx) {
    for (var n = 0; n < order.length; n++) {
      var idx = (fromIdx + n) % order.length;
      if (!badFiles[order[idx].url]) return idx;
    }
    return -1;
  }

  function advance() {
    if (order.length < 2) return;
    var idx = nextPlayableIndex(at + 1);
    if (idx === -1) {
      stop();
      return; // nothing playable left; hold whatever frame is up
    }
    at = idx;
    var entry = order[at];
    if (back.getAttribute("src") !== entry.url) prepare(back, entry);
    // The crossfade itself: toggling this class is what video-backdrop.css
    // actually animates (`opacity` transition on `.ba-video-el`).
    back.classList.add("ba-video-front");
    front.classList.remove("ba-video-front");
    var swap = front;
    front = back;
    back = swap;
    // Buffer the clip AFTER this one into the newly-hidden element right
    // away, so the NEXT crossfade — up to `rotateSeconds` from now —
    // never has to start a fetch at the moment it needs to show something.
    var followIdx = nextPlayableIndex(at + 1);
    if (followIdx !== -1) prepare(back, order[followIdx]);
  }

  function onError(el) {
    var url = el.getAttribute("src") || "";
    if (url) badFiles[url] = true;
    if (el === front) {
      // The clip actually on screen just broke (removed mid-session,
      // truly a 404 this time) — don't wait out the rotation interval on
      // a frozen or blank frame, move on now.
      advance();
    } else {
      // An off-screen preload failed; line up a different candidate for
      // it so the NEXT rotation still has something ready, instead of
      // discovering the gap only once it's already meant to be visible.
      var idx = nextPlayableIndex(at + 1);
      if (idx !== -1) prepare(back, order[idx]);
    }
  }
  elA.addEventListener("error", function () { onError(elA); });
  elB.addEventListener("error", function () { onError(elB); });

  // Pre-buffer the clip that will follow the one already on screen. Ample
  // lead time in every realistic rotate interval (the settings UI's
  // fastest option is 1 minute; a 20s clip loads well inside that).
  prepare(back, order[nextPlayableIndex(at + 1)]);

  var timer = null;
  var still = false;

  function stop() {
    if (timer !== null) {
      clearInterval(timer);
      timer = null;
    }
  }

  function start() {
    if (still || document.hidden) return;
    playQuiet(front);
    if (timer === null && rotateSeconds > 0) {
      timer = setInterval(advance, rotateSeconds * 1000);
    }
  }

  function pauseAll() {
    stop();
    try { front.pause(); } catch (e) {}
    try { back.pause(); } catch (e) {}
  }

  function onVisibility() {
    if (document.hidden) pauseAll();
    else start();
  }

  // Reduced motion: hold the current frame and stop rotating — a paused
  // <video> is a still image at no further decode cost, same contract
  // scene-shuffle.js documents ("the landscape is still a landscape, just
  // a fixed one"). `quiet` is the OS-level signal, always obeyed; Anki's
  // own Preferences > Reduce motion only counts when `backdrop_motion` is
  // `"auto"` — see `_backdrop_motion_mode` in `__init__.py`.
  var quiet = null;
  try {
    quiet = window.matchMedia("(prefers-reduced-motion: reduce)");
  } catch (e) {}

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
    if (still) {
      stop();
      try { front.pause(); } catch (e) {}
    } else {
      start();
    }
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

  function teardown() {
    pauseAll();
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

  document.addEventListener("visibilitychange", onVisibility);
  window.addEventListener("pagehide", teardown);
  window.addEventListener("beforeunload", teardown);

  applyMotionPref();
})();
