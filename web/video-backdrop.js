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
   (date-)picked clip and started it via the `autoplay` attribute, and
   put a `ba-video--preinit` class on the container — see `_video_html` /
   `_video_state` in `__init__.py`. video-backdrop.css makes that class
   the thing that holds `.ba-video-a` at `opacity: 1` (see there for why
   it can't just be a bare rule on `.ba-video-a`), so that clip is
   actually visible from the first paint, before this script has even
   run — and if this script never runs at all (fetch failure, a JS
   error elsewhere on the page), that one clip just keeps looping
   forever via the native `autoplay`/`loop` attributes rather than
   sitting behind an opaque, silent rectangle. Once this script does
   run, `init()` strips `ba-video--preinit` in the same breath it hands
   out the real `.ba-video-front` class, and takes over crossfading and
   rotation from there.

   Deck browser and overview only, same as the scene backdrop: the
   reviewer has no backdrop and no motion, on purpose. */
(function () {
  "use strict";

  if (window.__ankiDesignVideoBackdrop) return;
  window.__ankiDesignVideoBackdrop = true;

  // stdHtml() writes <script> tags into <head>, *before* `web_content.body`
  // — unlike scene-shuffle.js (which only ever touches
  // `document.documentElement`, present from byte one), this script needs
  // the `.ba-video-a`/`.ba-video-b` elements that live in the body, so it
  // has to wait for the parser to get there.
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }

  function init() {
    var opts = (window.__baOpts && window.__baOpts.video) || {};
    var videos = Array.isArray(opts.videos) ? opts.videos.slice() : [];
    // An empty list here means the server already decided there was
    // nothing to play (see the "video" branch in
    // `on_webview_will_set_content`) and injected no `.ba-video` markup at
    // all — so there is nothing for this script to do. It is not this
    // script's job to second-guess that.
    if (videos.length === 0) return;

    var elA = document.querySelector(".ba-video-a");
    var elB = document.querySelector(".ba-video-b");
    if (!elA || !elB) return;

    var MIN_ROTATE_SECONDS = 5;
    // Must match video-backdrop.css's `.ba-video-el` transition duration —
    // this is how long advance() waits before pausing the element that
    // just faded out, so the pause doesn't cut the fade off mid-transition.
    var CROSSFADE_MS = 900;
    // How long to wait for a target clip's first frame to actually decode
    // before giving up on a rotation cycle rather than fade to it. Only
    // reached when the follow-buffer's prediction didn't pan out (a
    // reshuffle wrap-around, a selection change, a bad-file skip), so it
    // has to cover a cold fetch, not just a decode — generous on purpose.
    var FADE_READY_TIMEOUT_MS = 3000;
    // HTMLMediaElement.HAVE_CURRENT_DATA: the readyState threshold at
    // which an element has actually decoded a frame of ITS CURRENT src,
    // as opposed to still showing the last frame of whatever it displayed
    // before. See beginFade()'s callers for why this specific threshold —
    // not just "the src attribute looks right" — is the fade gate.
    var READY_HAVE_CURRENT_DATA = 2;

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
    // treats it as "already showing" instead of announcing a change to
    // the clip that's already there.
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
    // `.ba-video--preinit` (on `.ba-video`, see `_video_html`) is what
    // actually keeps `.ba-video-a` visible before this script runs — see
    // video-backdrop.css. `.ba-video-a` never moves (it's the same DOM
    // node all session; `front`/`back` are pointers that swap across it,
    // not classes on it), so if this class stayed on the container, its
    // `.ba-video--preinit .ba-video-a { opacity: 1 }` rule (0,2,0) would
    // keep forcing elA visible forever — including after the first
    // crossfade hands `.ba-video-front` to elB, at which point elA would
    // be stuck fully opaque *on top of* elB instead of faded out: two
    // clips visible at once. Has to come off the instant this script
    // takes over, in the same breath as handing out the real
    // `.ba-video-front` class above.
    var container = document.querySelector(".ba-video");
    if (container) container.classList.remove("ba-video--preinit");

    var badFiles = Object.create(null);
    // Holds the setTimeout id for the deferred outgoing.pause() below, so a
    // second advance() within CROSSFADE_MS of the first can cancel the
    // stale one before it fires. Without this, an onError-driven advance()
    // (reachable from any bad clip source — see onError()) firing twice in
    // quick succession leaves two pending pause() calls; the first one's
    // callback captured the element that was front *at that time*, which
    // by the time it fires is the element the second advance() just made
    // visible again — pausing the thing actually on screen and freezing
    // the backdrop.
    var pauseTimer = null;

    function playQuiet(el) {
      try {
        var p = el.play();
        if (p && p.catch) p.catch(function () {});
      } catch (e) {}
    }

    // Buffers a clip into `el` without playing it. `el` here is always the
    // currently-hidden element — playing it here would decode it
    // continuously (forever, since <video loop> never stops) from the
    // moment it's buffered until the crossfade that finally shows it,
    // which for a long rotate interval is most of the page's lifetime for
    // no visible benefit. beginFade() is the only place that ever calls
    // play() on an element, and only for the one becoming visible, and
    // only once that element has actually decoded a frame of the clip
    // being buffered here — see advance()/waitForReady() below.
    function prepare(el, entry) {
      if (!entry) return;
      if (el.getAttribute("src") === entry.url) return; // already loaded
      try {
        el.pause();
        el.setAttribute("src", entry.url);
        el.load();
      } catch (e) {}
    }

    // Next order[] index, from `fromIdx`, that isn't a known-bad file.
    // Wraps at most once around the whole list; -1 means every clip in
    // the library has failed.
    function nextPlayableIndex(fromIdx) {
      for (var n = 0; n < order.length; n++) {
        var idx = (fromIdx + n) % order.length;
        if (!badFiles[order[idx].url]) return idx;
      }
      return -1;
    }

    // Holds the live wait for a target clip to become ready, if one is in
    // flight — `{ el, entry, cleanup }`. Mirrors `pauseTimer`'s discipline:
    // at most one is ever live, and anything that picks a new target
    // (advance()) or tears the page down (teardown()) cancels it first
    // rather than letting a late loadeddata/canplay fade to a clip nobody
    // asked for anymore.
    var pendingFade = null;

    function cancelPendingFade() {
      if (pendingFade) {
        pendingFade.cleanup();
        pendingFade = null;
      }
    }

    // Waits for `el` to actually have decoded a frame of `entry` — as
    // opposed to still showing whatever it last displayed under a
    // different src — before handing off to beginFade(). Self-cancelling
    // via `pendingFade` above.
    function waitForReady(el, entry) {
      var settled = false;
      var timeoutId = null;
      function cleanup() {
        el.removeEventListener("loadeddata", onReady);
        el.removeEventListener("canplay", onReady);
        if (timeoutId !== null) window.clearTimeout(timeoutId);
      }
      function onReady() {
        if (settled) return;
        settled = true;
        cleanup();
        pendingFade = null;
        // el's src can only have moved on from `entry` via a later
        // prepare() call, and every path that reassigns `back`'s src
        // cancels this wait first (advance(), onError()) — so this
        // should never actually mismatch. Checked anyway: firing a fade
        // for whatever `entry` used to mean, against whatever `el` now
        // actually holds, is exactly the stale-frame bug this exists to
        // prevent.
        if (el.getAttribute("src") !== entry.url) return;
        beginFade(entry);
      }
      function onTimeout() {
        if (settled) return;
        settled = true;
        cleanup();
        pendingFade = null;
        // Give up on this rotation cycle rather than flash a stale
        // frame — `front` just keeps playing what it's already showing.
        // `at` stays pointed at `entry` (advance() already set it before
        // calling here), so the next scheduled advance() moves on from
        // this clip instead of retrying the one that just timed out.
      }
      timeoutId = window.setTimeout(onTimeout, FADE_READY_TIMEOUT_MS);
      el.addEventListener("loadeddata", onReady);
      el.addEventListener("canplay", onReady);
      pendingFade = { el: el, entry: entry, cleanup: cleanup };
    }

    // The actual crossfade to `entry`, which is already showing on `back`
    // (readyState >= HAVE_CURRENT_DATA) by the time this runs — whether
    // that was true the instant advance() checked, or only became true
    // later via waitForReady()'s loadeddata/canplay. Everything that used
    // to run unconditionally at the bottom of advance() lives here now,
    // so none of it — the class swap video-backdrop.css animates, the
    // front/back pointer swap, the outgoing element's deferred pause, or
    // lining up the clip after this one — can happen ahead of the frame
    // that's supposed to justify it.
    function beginFade(entry) {
      var outgoing = front;
      // The crossfade itself: toggling this class is what
      // video-backdrop.css actually animates (`opacity` transition on
      // `.ba-video-el`). The incoming element must actually be playing
      // for the fade to show anything but a frozen frame — prepare()
      // deliberately never calls play(), so this is the one place that
      // does.
      back.classList.add("ba-video-front");
      front.classList.remove("ba-video-front");
      playQuiet(back);
      var swap = front;
      front = back;
      back = swap;
      // Cancel any still-pending pause from a previous fade — see
      // `pauseTimer`'s comment above. Letting it fire against a stale
      // `outgoing` reference is exactly the freeze this guards against.
      if (pauseTimer !== null) {
        window.clearTimeout(pauseTimer);
        pauseTimer = null;
      }
      // Buffer the clip AFTER this one so the NEXT crossfade doesn't have
      // to start its fetch at the moment it needs something to show.
      // Computed now (stable — depends only on `at`, which doesn't change
      // again until the next advance()) but not *loaded* into `outgoing`
      // until the pause callback below confirms it's fully faded out and
      // hidden — see that callback for why.
      var followIdx = nextPlayableIndex(at + 1);
      // Pause the outgoing element once its fade-out has actually
      // finished (not immediately — that would cut the transition off
      // mid-fade and freeze it) so only the one on screen keeps
      // decoding. Self-cancelling: if another fade has started in the
      // meantime (see `pauseTimer` above), `outgoing` here may no longer
      // be the hidden element — it may be the one a later fade just made
      // `front` again — so re-check before pausing rather than trusting
      // the closure's stale snapshot.
      pauseTimer = window.setTimeout(function () {
        pauseTimer = null;
        if (outgoing !== front) {
          try { outgoing.pause(); } catch (e) {}
          // Safe to load the next-next clip into it only now: its
          // fade-out transition (video-backdrop.css's 900ms opacity,
          // matching CROSSFADE_MS) has actually finished, so it's fully
          // hidden, not just paused. Doing this immediately when the
          // fade started (the pre-fix behavior) mutated the src of an
          // element that was still visibly blending into the page for
          // the next 900ms — the same "mid-load, still on screen"
          // hazard advance()/waitForReady() guard against for the
          // incoming element below.
          if (followIdx !== -1) prepare(outgoing, order[followIdx]);
        }
      }, CROSSFADE_MS);
    }

    function advance() {
      if (order.length < 2) return;
      var idx = nextPlayableIndex(at + 1);
      // -1: every clip has failed, nothing left to show. idx === at:
      // every OTHER clip has failed, so the only "next" candidate
      // nextPlayableIndex can find by wrapping is the one already on
      // screen — crossfading a clip into itself every rotate interval
      // instead of holding it. Both cases: stop and hold the frame.
      //
      // Deliberately NOT cancelling any pending `pauseTimer` or
      // `pendingFade` on this path: there is no new crossfade here to
      // protect, so an earlier fade's deferred pause, or an earlier
      // fade still waiting on its target to become ready, should still
      // go through on its own schedule — their own re-checks (`outgoing
      // !== front` for the pause; the src comparison in waitForReady's
      // onReady) are what decide whether acting is still correct, and
      // those checks need the timer/listeners to actually survive.
      if (idx === -1 || idx === at) {
        stop();
        return;
      }
      at = idx;
      var entry = order[at];
      // A fade already waiting on a *different* target (an earlier
      // advance() whose pre-buffered clip didn't pan out, or whose grace
      // window hasn't expired yet) is now stale — this call just picked
      // a new target, and letting that old wait's listener fire later
      // would fade to a clip nobody asked for anymore. Nothing else to
      // unwind alongside it: nothing ever became visible on that path,
      // that's the whole point of waiting.
      cancelPendingFade();
      prepare(back, entry);
      if (back.getAttribute("src") === entry.url &&
          back.readyState >= READY_HAVE_CURRENT_DATA) {
        // Normal path: the follow-buffer (or a lucky idle fetch) already
        // got this clip's first frame decoded, so the element about to
        // fade in is actually showing IT, not whatever it displayed the
        // last time it held a different src. Fade now.
        beginFade(entry);
        return;
      }
      // back's displayed frame does NOT belong to `entry` yet — either
      // prepare() above just changed its src (a reshuffle wrap-around, a
      // selection change, a bad-file skip: any path where the follow-
      // buffer's prediction didn't match this advance()'s actual
      // target), or it already had the right src but hasn't decoded a
      // frame of it yet. Toggling the crossfade class here regardless —
      // the pre-fix behavior — starts the opacity transition while the
      // element is still showing its PREVIOUS clip's last frame: the
      // viewer sees that stale clip for however long loading takes, then
      // a hard content-swap to the real target underneath a fade that
      // already started ("a third scene plays for a second, then it
      // flips"). Defer instead: no class change, no play(), no pointer
      // swap, until the element proves it has the target's first frame.
      waitForReady(back, entry);
    }

    function onError(el) {
      var url = el.getAttribute("src") || "";
      if (url) badFiles[url] = true;
      // If this element was the live target of a readiness wait, that
      // wait can never resolve now — its src just errored, not loaded —
      // so tear it down before deciding what to do next, the same way
      // advance() does before starting a different one.
      if (pendingFade && pendingFade.el === el) cancelPendingFade();
      if (el === front) {
        // The clip actually on screen just broke (removed mid-session,
        // truly a 404 this time) — don't wait out the rotation interval
        // on a frozen or blank frame, move on now.
        advance();
      } else {
        // An off-screen preload failed; line up a different candidate
        // for it so the NEXT rotation still has something ready, instead
        // of discovering the gap only once it's already meant to be
        // visible.
        var idx = nextPlayableIndex(at + 1);
        if (idx !== -1) prepare(back, order[idx]);
      }
    }
    elA.addEventListener("error", function () { onError(elA); });
    elB.addEventListener("error", function () { onError(elB); });

    // Pre-buffer the clip that will follow the one already on screen.
    // Ample lead time in every realistic rotate interval (the settings
    // UI's fastest option is 1 minute; a 20s clip loads well inside
    // that). Skipped entirely for a single-clip library — with only one
    // playable clip, "the one that follows it" IS it, and there is
    // nothing to gain from decoding the same file into both elements.
    // Safe against the same "mid-load, still on screen" hazard advance()
    // guards against below: `back` (elB) has no `ba-video-front` class
    // and is not the element `.ba-video--preinit` holds visible (that's
    // `.ba-video-a`/`front` only) — it stays fully hidden, opacity 0, the
    // whole time this load is in flight, right up until the first
    // advance() decides whether to fade it in at all.
    if (order.length > 1) {
      prepare(back, order[nextPlayableIndex(at + 1)]);
    }

    var timer = null;
    var still = false;

    function stop() {
      if (timer !== null) {
        clearInterval(timer);
        timer = null;
      }
    }

    function start() {
      if (still || document.hidden || occluded) return;
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

    // `document.hidden` only ever fires for genuine tab/window occlusion,
    // not for the addon's own inline overlays (Browse, Stats, Settings)
    // opening on top of the deck browser — those are Qt-level widgets
    // painted over `mw.web` without ever hiding or blurring it, so a
    // video backdrop underneath keeps decoding, invisibly, behind an
    // opaque panel for as long as the overlay is up. Anki's Python side
    // pushes state here via these two window-level hooks (mirroring
    // `__baSetActive`/`__baSetStanding`) whenever an embed opens or
    // closes — see `_push_video_occlusion` in `__init__.py`. Window
    // blur/focus are wired too, belt-and-braces, for whatever occlusion
    // *does* reach the OS level (another app, another Space).
    // Tracks embed occlusion specifically (as opposed to `document.hidden`,
    // which onVisibility already handles): set true for the whole time an
    // inline embed is open, false once it closes. Without this, a window
    // focus event that arrives *while an embed is still open* (e.g. the
    // user clicks back into the Anki window without closing Settings first)
    // would call start() and resume decoding/painting behind the opaque
    // overlay — onWindowFocus only ever checked `document.hidden`, which
    // stays false the whole time an embed is up (see the comment above on
    // why `document.hidden` never fires for these).
    // Seeded from the render-time snapshot Python took in `_js_opts`
    // (`opts.occluded`, via `_any_embed_open()`) rather than always
    // starting false: a render that happens WHILE an embed is already
    // open — e.g. `_refresh_views()` firing from a Settings toggle with
    // Settings' own overlay still up — would otherwise default to
    // "not occluded" here and have applyMotionPref() below call start()
    // and begin decoding behind the overlay before `_push_video_occlusion`
    // ever gets a chance to push a correction. The two pause/resume hooks
    // above take over from here for every occlusion change that happens
    // AFTER this point.
    var occluded = !!opts.occluded;
    window.__baVideoPause = function () {
      occluded = true;
      pauseAll();
    };
    window.__baVideoResume = function () {
      occluded = false;
      if (document.hidden) return;
      start();
    };
    function onWindowBlur() {
      pauseAll();
    }
    function onWindowFocus() {
      if (!document.hidden) start();
    }
    window.addEventListener("blur", onWindowBlur);
    window.addEventListener("focus", onWindowFocus);

    // Reduced motion: hold the current frame and stop rotating — a
    // paused <video> is a still image at no further decode cost, same
    // contract scene-shuffle.js documents ("the landscape is still a
    // landscape, just a fixed one"). `quiet` is the OS-level signal,
    // always obeyed; Anki's own Preferences > Reduce motion only counts
    // when `backdrop_motion` is `"auto"` — see `_backdrop_motion_mode`
    // in `__init__.py`.
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
        // pauseAll(), not just front — back is mid-buffer (or holding a
        // pre-loaded clip) and keeps decoding otherwise; "still" means
        // stop, full stop.
        pauseAll();
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
      // A fade waiting on loadeddata/canplay that never gets to fire its
      // listener again — the page is going away — still holds a live
      // setTimeout and two event listeners otherwise.
      cancelPendingFade();
      document.removeEventListener("visibilitychange", onVisibility);
      window.removeEventListener("pagehide", teardown);
      window.removeEventListener("beforeunload", teardown);
      window.removeEventListener("blur", onWindowBlur);
      window.removeEventListener("focus", onWindowFocus);
      try { delete window.__baVideoPause; } catch (e) { window.__baVideoPause = undefined; }
      try { delete window.__baVideoResume; } catch (e) { window.__baVideoResume = undefined; }
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
  }
})();
