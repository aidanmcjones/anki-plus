"""OPT-IN live test on the REAL display: Safari-style full screen reveal.

Skipped unless ANKI_LIVE_DISPLAY=1 (run_in_app.py reads NEEDS_DISPLAY and
prints SKIP otherwise). With it set, the harness launches its throwaway app
instance with QT_QPA_PLATFORM=cocoa, so a real window appears, goes full
screen, and the mouse cursor is moved. Run it only with the user's consent,
while they are not using the Mac:

    cd ~/dev/anki && ANKI_LIVE_DISPLAY=1 out/pyenv/bin/python \\
        ~/dev/anki-design/tests/live/run_in_app.py \\
        ~/dev/anki-design/tests/live/test_fullscreen_display.py

What it does:
  1. enters macOS full screen on the main window and waits for
     fullscreen_inset to report it is following AppKit's bar;
  2. with the bar hidden, records the baseline: the window top, the content
     top, the sidebar wordmark, and a strip of screen pixels over the
     sidebar (no strip: the content starts at the window top);
  3. moves the cursor to the top edge of that screen with Quartz
     (CGWarpMouseCursorPosition + a posted kCGEventMouseMoved, via ctypes);
  4. samples for 2.5 s, as fast as a screen capture allows (about every
     40 ms): the visible bottom edge of AppKit's full screen bar, and where
     the app's pixels are. The push is measured on the SCREEN, by matching
     each captured strip against the baseline strip (vertical shift of the
     row profile below the bar), so it counts whatever moved the content;
  5. moves the cursor back to mid-screen and samples 1.5 s more while the
     bar hides;
  6. asserts the content moved down exactly once while the bar was out (no
     flicker), passed through intermediate positions, stayed down, slid
     back to 0 when the bar hid, and that in every sample the content's
     shift equals the bar's reveal (lockstep, within 3 pt plus the bar's own
     motion between the two reads);
  7. leaves full screen: toolbar gone, content view back at its place.

Diagnostics per sample go to the notes, and in full as JSON when
ANKI_FS_DIAG_OUT=<file> is set: window frame, contentView frame,
contentLayoutRect, the content view's safeAreaInsets, Qt's safe area
margins and contentsRect, the toolbar's visibility, the bar window's frame,
the bar's visible bottom and the controller's offset.

Reading the screen needs Screen Recording permission and posting mouse
events needs Accessibility permission for the process that launches the
harness (Terminal, or the agent host). arm64 only (NSRect is returned in
registers there).
"""

from __future__ import annotations

import ctypes
import json
import os
import platform
import sys
import time

NEEDS_DISPLAY = True

SAMPLE_S = 2.5
REST_S = 1.5         # s: bar must stay closed this long with the cursor away
TOP_X, TOP_W, TOP_H = 10, 120, 60  # pt: strip at the content's top-left (sidebar)
REST_ERR_MIN = 400.0  # row-profile units; a bar over the strip is far above
REST_ERR_FRAC = 0.12  # ... or this fraction of the strip's mean row level
HIDE_S = 1.5
LOCKSTEP_TOL = 3.0   # pt, plus the bar's own motion between two reads
MOVE_MIN = 20.0      # pt: the smallest push that counts as "the bar came out"
STRIP_W = 120        # pt: width of the pixel strip over the sidebar
STRIP_H = 460        # pt: from the screen top down
MATCH_TOP = 130      # pt: rows compared start here (below any bar)
MATCH_BOTTOM = 380   # pt
MAX_SHIFT = 80       # pt


class NSPoint(ctypes.Structure):
    _fields_ = [("x", ctypes.c_double), ("y", ctypes.c_double)]


class NSSize(ctypes.Structure):
    _fields_ = [("w", ctypes.c_double), ("h", ctypes.c_double)]


class NSRect(ctypes.Structure):
    _fields_ = [("origin", NSPoint), ("size", NSSize)]


class NSEdgeInsets(ctypes.Structure):
    _fields_ = [("top", ctypes.c_double), ("left", ctypes.c_double),
                ("bottom", ctypes.c_double), ("right", ctypes.c_double)]


CGPoint = NSPoint


def _fi():
    for k, m in sys.modules.items():
        if k.endswith(".fullscreen_inset") and hasattr(m, "CocoaBridge"):
            return m
    return None


def _quartz():
    cg = ctypes.cdll.LoadLibrary(
        "/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices")
    vp = ctypes.c_void_p
    cg.CGWarpMouseCursorPosition.argtypes = [CGPoint]
    cg.CGWarpMouseCursorPosition.restype = ctypes.c_int32
    cg.CGEventCreateMouseEvent.argtypes = [vp, ctypes.c_uint32, CGPoint, ctypes.c_uint32]
    cg.CGEventCreateMouseEvent.restype = vp
    cg.CGEventPost.argtypes = [ctypes.c_uint32, vp]
    cg.CGEventPost.restype = None
    cg.CGEventCreate.argtypes = [vp]
    cg.CGEventCreate.restype = vp
    cg.CGEventGetLocation.argtypes = [vp]
    cg.CGEventGetLocation.restype = CGPoint
    cg.CFRelease.argtypes = [vp]
    cg.CFRelease.restype = None
    cg.CGDisplayCreateImageForRect.argtypes = [ctypes.c_uint32, NSRect]
    cg.CGDisplayCreateImageForRect.restype = vp
    cg.CGGetDisplaysWithPoint.argtypes = [CGPoint, ctypes.c_uint32,
                                          ctypes.POINTER(ctypes.c_uint32),
                                          ctypes.POINTER(ctypes.c_uint32)]
    cg.CGGetDisplaysWithPoint.restype = ctypes.c_int32
    cg.CGDisplayBounds.argtypes = [ctypes.c_uint32]
    cg.CGDisplayBounds.restype = NSRect
    for f in ("CGImageGetWidth", "CGImageGetHeight", "CGImageGetBytesPerRow"):
        getattr(cg, f).argtypes = [vp]
        getattr(cg, f).restype = ctypes.c_size_t
    cg.CGImageGetDataProvider.argtypes = [vp]
    cg.CGImageGetDataProvider.restype = vp
    cg.CGDataProviderCopyData.argtypes = [vp]
    cg.CGDataProviderCopyData.restype = vp
    cg.CFDataGetLength.argtypes = [vp]
    cg.CFDataGetLength.restype = ctypes.c_long
    cg.CFDataGetBytePtr.argtypes = [vp]
    cg.CFDataGetBytePtr.restype = vp
    cg.CGPreflightScreenCaptureAccess.restype = ctypes.c_bool
    cg.AXIsProcessTrusted.restype = ctypes.c_bool
    return cg


def _move_cursor(cg, x: float, y: float) -> None:
    p = CGPoint(x, y)
    cg.CGWarpMouseCursorPosition(p)
    ev = cg.CGEventCreateMouseEvent(None, 5, p, 0)  # kCGEventMouseMoved
    if ev:
        cg.CGEventPost(0, ev)  # kCGHIDEventTap
        cg.CFRelease(ev)


def _cursor(cg):
    ev = cg.CGEventCreate(None)
    p = cg.CGEventGetLocation(ev)
    cg.CFRelease(ev)
    return p.x, p.y


def _display_at(cg, x: float, y: float):
    """(display id, its global top-left origin) for a global point."""
    did, n = ctypes.c_uint32(0), ctypes.c_uint32(0)
    cg.CGGetDisplaysWithPoint(CGPoint(x, y), 1, ctypes.byref(did), ctypes.byref(n))
    b = cg.CGDisplayBounds(did.value)
    return did.value, (b.origin.x, b.origin.y)


def _row_profile(cg, x: float, y: float, w: float, h: float):
    """Screen strip (global top-left coords, pt) as one number per point
    row: the summed brightness of every 2nd pixel of that row. None if the
    screen cannot be read."""
    did, (ox, oy) = _display_at(cg, x, y)
    img = cg.CGDisplayCreateImageForRect(did, NSRect(NSPoint(x - ox, y - oy), NSSize(w, h)))
    if not img:
        return None
    try:
        iw, ih = cg.CGImageGetWidth(img), cg.CGImageGetHeight(img)
        bpr = cg.CGImageGetBytesPerRow(img)
        data = cg.CGDataProviderCopyData(cg.CGImageGetDataProvider(img))
        raw = ctypes.string_at(cg.CFDataGetBytePtr(data), cg.CFDataGetLength(data))
        cg.CFRelease(data)
    finally:
        cg.CFRelease(img)
    scale = max(1, round(ih / h))
    out = []
    for pr in range(int(h)):
        r = pr * scale
        row = raw[r * bpr: r * bpr + iw * 4]
        out.append(sum(row[0::8]) + sum(row[1::8]) + sum(row[2::8]))
    return out


def _shift(base, cur):
    """Vertical shift (pt, down positive) that best maps the baseline rows
    MATCH_TOP..MATCH_BOTTOM onto `cur`, and the residual error."""
    if not base or not cur:
        return None, None
    best = (None, None)
    rows = range(MATCH_TOP, MATCH_BOTTOM)
    for s in range(-10, MAX_SHIFT + 1):
        if MATCH_BOTTOM + s > len(cur) or MATCH_TOP + s < 0:
            continue
        err = sum(abs(base[r] - cur[r + s]) for r in rows) / len(rows)
        if best[1] is None or err < best[1]:
            best = (s, err)
    return best


def _profile_err(a, b):
    """Mean absolute row difference of two row profiles (no shift)."""
    if not a or not b:
        return None
    n = min(len(a), len(b))
    return round(sum(abs(a[i] - b[i]) for i in range(n)) / n, 1)


def _analyse(samples, win_top):
    """The checks' inputs from the samples: push per sample (pt), the bar's
    reveal per sample (pt below the window top), and the verdicts."""
    push = [s["push"] for s in samples if s["push"] is not None]
    reveal = [None if s["bar_vis"] is None else round(win_top - s["bar_vis"], 1)
              for s in samples]
    peak = max(push) if push else 0
    half = peak / 2 if peak else 0.5
    state = [p >= half for p in push]
    rises = sum(1 for a, b in zip(state, state[1:]) if not a and b) + (1 if state and state[0] else 0)
    falls = sum(1 for a, b in zip(state, state[1:]) if a and not b)
    steps = sorted({round(p) for p in push})
    gaps = []
    prev_rev = None
    for s, rv in zip(samples, reveal):
        if s["push"] is None or rv is None:
            continue
        motion = abs(rv - prev_rev) if prev_rev is not None else 0.0
        prev_rev = rv
        gaps.append(abs(max(rv, 0.0) - s["push"]) - motion)
    return {"push": push, "reveal": reveal, "peak": peak, "state": state,
            "rises": rises, "falls": falls, "steps": steps, "gaps": gaps}


def _screen_locked() -> bool:
    import subprocess
    try:
        out = subprocess.run(["ioreg", "-n", "Root", "-d1"], capture_output=True,
                             text=True, timeout=5).stdout
    except Exception:
        return False
    return '"CGSSessionScreenIsLocked"=Yes' in out


def run(t):
    if os.environ.get("ANKI_LIVE_DISPLAY") != "1":
        t.note("skipped: set ANKI_LIVE_DISPLAY=1 to run on the real display")
        t.check("skipped (opt-in real-display test)", True)
        return
    if platform.machine() != "arm64":
        t.check("arm64 Mac", False, platform.machine())
        return
    if _screen_locked():
        t.check("screen unlocked (a locked screen shows no reveal)", False,
                "CGSSessionScreenIsLocked")
        return
    from aqt.qt import QApplication, QPoint, Qt

    mw = t.mw
    t.check("real display (cocoa)", QApplication.platformName() == "cocoa",
            QApplication.platformName())
    fi = _fi()
    ctl = getattr(fi, "_controller", None) if fi else None
    t.check("fullscreen_inset controller installed", ctl is not None, repr(ctl))
    if ctl is None:
        return
    br = ctl._bridge
    o = br.o
    vp, ul, bl = ctypes.c_void_p, ctypes.c_ulong, ctypes.c_bool
    nswin = br.nswindow(mw)
    view = int(mw.winId())
    cg = _quartz()
    t.note(f"permissions: screen capture={cg.CGPreflightScreenCaptureAccess()}"
           f" accessibility={cg.AXIsProcessTrusted()}")

    def rect(r):
        return [round(r.origin.y, 1), round(r.size.h, 1)]

    def content_view():
        return o.send(nswin, "contentView") or 0

    def content_rect():
        """Content view rect in Cocoa screen coords (origin bottom-left)."""
        cv = content_view() or view
        b = o.send(cv, "bounds", restype=NSRect)
        r = o.send(cv, "convertRect:toView:", b, None, restype=NSRect,
                   argtypes=(NSRect, vp))
        return o.send(nswin, "convertRectToScreen:", r, restype=NSRect, argtypes=(NSRect,))

    def win_frame():
        return o.send(nswin, "frame", restype=NSRect)

    def app_windows():
        wins = o.send(br._app(), "windows")
        n = o.send(wins, "count", restype=ul)
        return [o.send(wins, "objectAtIndex:", i, argtypes=(ul,)) for i in range(n)]

    def bar_window():
        w = br.find_bar(nswin)
        if w:
            return w
        for w in app_windows():
            name = o.class_name(w)
            if "FullScreen" in name and "Toolbar" in name:
                return w
        return 0

    def bar():
        """(bottom y, height) of AppKit's full screen toolbar window in
        Cocoa screen coords, or None while it is not on screen."""
        w = bar_window()
        if not w or not o.send(w, "isVisible", restype=bl):
            return None
        return rect(o.send(w, "frame", restype=NSRect))

    def tree(v, depth=0, out=None):
        out = [] if out is None else out
        if not v or depth > 4:
            return out
        f = o.send(v, "frame", restype=NSRect)
        out.append("  " * depth + f"{o.class_name(v)} y={f.origin.y:.0f} h={f.size.h:.0f}")
        subs = o.send(v, "subviews")
        for i in range(min(o.send(subs, "count", restype=ul), 8)):
            tree(o.send(subs, "objectAtIndex:", i, argtypes=(ul,)), depth + 1, out)
        return out

    def cocoa_state():
        tb = o.send(nswin, "toolbar") or 0
        qwin = mw.windowHandle()
        sa = qwin.safeAreaMargins().top() if qwin is not None and hasattr(qwin, "safeAreaMargins") else None
        cv = content_view()
        return {
            "win": rect(win_frame()),
            "cv": rect(o.send(cv, "frame", restype=NSRect)) if cv else None,
            "cv_is_qnsview": cv == view,
            "layout": rect(o.send(nswin, "contentLayoutRect", restype=NSRect)),
            "ns_safe_top": round(o.send(view, "safeAreaInsets", restype=NSEdgeInsets).top, 1),
            "qt_safe_top": sa,
            "qt_contents_top": mw.contentsRect().top(),
            "web_y": mw.web.mapTo(mw, QPoint(0, 0)).y(),
            "tb_visible": bool(o.send(tb, "isVisible", restype=bl)) if tb else None,
            "bar": bar(),
            "bar_vis": (lambda b: None if b is None else round(b, 1))(
                br.bar_bottom(bar_window()) if bar_window() else None),
            "offset": round(ctl.offset, 1),
        }

    diag_out = os.environ.get("ANKI_FS_DIAG_OUT")
    cursor0 = _cursor(cg)
    log = {}
    try:
        _cycle(t, dict(locals()), log)
    finally:
        _move_cursor(cg, *cursor0)
        if mw.isFullScreen() or br.is_fullscreen(nswin):
            mw.setWindowState(mw.windowState() & ~Qt.WindowState.WindowFullScreen)
            t.wait_until(lambda: not br.is_fullscreen(nswin), timeout=10)
            t.pump(1000)
        cv = o.send(nswin, "contentView")
        cvf = o.send(cv, "frame", restype=NSRect) if cv else None
        t.check("left full screen: toolbar gone, content view back in place",
                not br.is_fullscreen(nswin) and not _following(ctl) and _toolbar_count(o, nswin) == 0
                and ctl.offset == 0 and cvf is not None and cvf.origin.y == 0,
                f"fullscreen={br.is_fullscreen(nswin)} following={_following(ctl)} "
                f"offset={ctl.offset} cv y={cvf.origin.y if cvf else None}")
        if diag_out:
            try:
                with open(diag_out, "w") as fh:
                    json.dump(log, fh, indent=1, default=str)
            except Exception as e:
                t.note(f"diag write failed: {e!r}")


def _is_active(br) -> bool:
    return bool(br.o.send(br._app(), "isActive", restype=ctypes.c_bool))


def _activate(t, br, mw) -> None:
    """Bring the throwaway app to the front (macOS 14+ cooperative
    activation may still refuse while the user works in another app)."""
    o = br.o
    app = br._app()
    if o.responds(app, "activate"):
        o.send(app, "activate", restype=None)
    o.send(app, "activateIgnoringOtherApps:", True, restype=None,
           argtypes=(ctypes.c_bool,))
    mw.show()
    mw.raise_()
    mw.activateWindow()
    t.wait_until(lambda: _is_active(br), timeout=1.5)


def _sample(t, env, cg, base, sx, top, t0, until_s, out):
    while time.monotonic() - t0 < until_s:
        st = env["cocoa_state"]()
        prof = _row_profile(cg, sx, top, STRIP_W, STRIP_H)
        st["ms"] = round((time.monotonic() - t0) * 1000)
        st["cursor"] = [round(v) for v in _cursor(cg)]  # test-side only
        st["active"] = _is_active(env["br"])
        st["push"], st["err"] = _shift(base, prof)
        out.append(st)
        t.pump(10)


def _following(ctl) -> bool:
    """The controller is active in full screen (older builds: toolbar attached)."""
    return bool(getattr(ctl, "following", getattr(ctl, "attached", False)))


def _toolbar_count(o, nswin) -> int:
    return 1 if o.send(nswin, "toolbar") else 0


def _cycle(t, env, res):
    """Windowed baseline, enter full screen, check the bar is closed at
    rest, reveal it, sample, hide it, sample."""
    from aqt.qt import QPoint, Qt

    mw, br, o, fi, ctl, cg = (env[k] for k in ("mw", "br", "o", "fi", "ctl", "cg"))
    nswin, content_rect, win_frame = env["nswin"], env["content_rect"], env["win_frame"]
    bar_window, tree, app_windows = env["bar_window"], env["tree"], env["app_windows"]

    # The throwaway app must be the active app for AppKit to take the
    # window full screen (the user's own apps may be in front).
    _activate(t, br, mw)
    active = _is_active(br)
    scr = mw.screen().geometry()
    mid_x = scr.left() + scr.width() / 2
    _move_cursor(cg, mid_x, scr.top() + scr.height() / 2)
    t.pump(600)

    # Windowed baseline: the plain title bar's height as AppKit lays it out
    # (frame minus content view), and the top of the content as pixels.
    wf0 = win_frame()
    cvf0 = o.send(o.send(nswin, "contentView"), "frame", restype=NSRect)
    title_h = round(wf0.size.h - cvf0.size.h, 1)
    res["windowed_title_h"] = title_h
    g0 = mw.mapToGlobal(QPoint(0, 0))
    win_strip = _row_profile(cg, g0.x() + TOP_X, g0.y(), TOP_W, TOP_H)

    mw.setWindowState(mw.windowState() | Qt.WindowState.WindowFullScreen)
    entered = t.wait_until(lambda: br.is_fullscreen(nswin) and _following(ctl), timeout=6)
    how = "setWindowState"
    if not entered:
        o.send(nswin, "toggleFullScreen:", None, restype=None, argtypes=(ctypes.c_void_p,))
        entered = t.wait_until(lambda: br.is_fullscreen(nswin) and _following(ctl), timeout=8)
        how = "toggleFullScreen:"
    t.check("entered full screen and the controller follows the bar", entered,
            f"fullscreen={br.is_fullscreen(nswin)} following={_following(ctl)} via {how} "
            f"app active={active} visible={mw.isVisible()}")
    if not entered:
        return
    t.pump(1200)
    if os.environ.get("ANKI_FS_DEACTIVATE") == "1":
        # Put another app in front without leaving this Space: Finder.
        o.send(br._app(), "deactivate", restype=None)
        apps = o.send(o.cls("NSRunningApplication"),
                      "runningApplicationsWithBundleIdentifier:",
                      o.nsstring("com.apple.finder"), argtypes=(ctypes.c_void_p,))
        finder = o.send(apps, "firstObject") if apps else 0
        if finder:
            o.send(finder, "activateWithOptions:", 0, restype=ctypes.c_bool,
                   argtypes=(ctypes.c_ulong,))
        t.wait_until(lambda: not _is_active(br), timeout=2)
        t.pump(400)
        res["space_still_ours"] = bool(br.is_fullscreen(nswin)) and bool(
            o.send(nswin, "isOnActiveSpace", restype=ctypes.c_bool))
        t.note(f"deactivated: app active={_is_active(br)}, "
               f"window on active space={res['space_still_ours']}")
    res["options"] = hex(int(o.send(br._app(), "presentationOptions", restype=ctypes.c_ulong)))

    # At rest, before the cursor goes anywhere near the top: no bar, no
    # toolbar, the content's top pixels are the windowed content's top.
    wf = win_frame()
    win_top = wf.origin.y + wf.size.h
    g1 = mw.mapToGlobal(QPoint(0, 0))
    rest = []
    t_r = time.monotonic()
    while time.monotonic() - t_r < REST_S:
        vis = br.bar_bottom(bar_window()) if bar_window() else None
        top_prof = _row_profile(cg, g1.x() + TOP_X, g1.y(), TOP_W, TOP_H)
        e0 = _profile_err(win_strip, top_prof)
        rest.append({"ms": round((time.monotonic() - t_r) * 1000),
                     "bar_vis": None if vis is None else round(vis, 1),
                     "reveal": fi.reveal_amount(win_top, vis) if hasattr(fi, "reveal_amount")
                     else (0 if vis is None else max(0.0, win_top - vis)),
                     "toolbar": _toolbar_count(o, nswin), "err": e0,
                     "active": _is_active(br), "offset": round(ctl.offset, 1)})
        t.pump(40)
    res["rest"] = rest
    ref = _profile_err(win_strip, win_strip)
    worst = max(r["err"] for r in rest if r["err"] is not None) if rest else None
    level = (sum(win_strip) / len(win_strip)) if win_strip else 0
    t.note(f"at rest: windowed title bar {title_h} pt, options {res['options']}, "
           f"pixel err max {worst} (strip mean {round(level)}), samples "
           + "; ".join(f"{r['ms']}:vis={r['bar_vis']} rv={r['reveal']} tb={r['toolbar']} "
                       f"err={r['err']} act={int(r['active'])} off={r['offset']}"
                       for r in rest[:8]))
    t.check("no NSToolbar attached in full screen",
            all(r["toolbar"] == 0 for r in rest), [r["toolbar"] for r in rest][:5])
    t.check(f"bar closed at rest for {REST_S} s (bar window off screen, content top "
            "pixels match the windowed content top)",
            bool(rest) and all(r["reveal"] == 0 and r["offset"] == 0 for r in rest)
            and (res.get("space_still_ours") is False  # another Space is on screen
                 or (worst is not None
                     and worst <= max(REST_ERR_MIN, REST_ERR_FRAC * level))),
            f"max reveal {max(r['reveal'] for r in rest) if rest else None}, "
            f"max offset {max(r['offset'] for r in rest) if rest else None}, "
            f"pixel err {worst} vs limit {round(max(REST_ERR_MIN, REST_ERR_FRAC * level))}, "
            f"self {ref}"
            + (" (pixels not compared: macOS switched Spaces when another app came"
               " to the front)" if res.get("space_still_ours") is False else ""))
    if os.environ.get("ANKI_FS_DEACTIVATE") == "1":
        t.note("ANKI_FS_DEACTIVATE=1: app deactivated for the at-rest check; "
               "reveal checks skipped (AppKit reveals only the active app's bar)")
        return
    c0 = content_rect()
    top0 = c0.origin.y + c0.size.h
    t.check("no strip: with the bar hidden the content starts at the window top",
            abs(win_top - top0) <= 1.0 and ctl.offset == 0,
            f"window top {win_top} content top {top0} offset {ctl.offset}")
    t.check("no Qt contents margin", mw.contentsMargins().top() == 0,
            mw.contentsMargins().top())
    el = t.element_center(".ba-side-mark", frac_y=0.0)
    t.check("sidebar wordmark found", el is not None, "")
    gx = mw.web.mapToGlobal(el).x() if el is not None else scr.left() + 100
    sx = max(scr.left(), gx - STRIP_W / 2)
    res["baseline"] = env["cocoa_state"]()
    res["windows"] = [f"{o.class_name(w)} {env['rect'](o.send(w, 'frame', restype=NSRect))}"
                      f" parent={o.send(w, 'parentWindow') == nswin}"
                      for w in app_windows()]
    base = _row_profile(cg, sx, scr.top(), STRIP_W, STRIP_H)

    # Reveal. AppKit only reveals the bar of the active app.
    _activate(t, br, mw)
    res["active_before_reveal"] = _is_active(br)
    _move_cursor(cg, mid_x, scr.top())
    t0 = time.monotonic()
    samples = []
    _sample(t, env, cg, base, sx, scr.top(), t0, SAMPLE_S, samples)
    bw = bar_window()
    res["bar_tree"] = tree(o.send(bw, "contentView")) if bw else []
    # Hide.
    _move_cursor(cg, mid_x, scr.top() + scr.height() / 2)
    t1 = time.monotonic()
    hide = []
    _sample(t, env, cg, base, sx, scr.top(), t1, HIDE_S, hide)
    res["samples"], res["hide"] = samples, hide
    a = _analyse(samples, win_top)
    res["analysis"] = {k: a[k] for k in ("peak", "rises", "falls", "steps")}

    def line(s):
        return (f"{s['ms']}:{s['push']}/{s['err'] and round(s['err'])} cur={s['cursor']} "
                f"act={int(s['active'])} "
                f"vis={s['bar_vis']} off={s['offset']} barwin={s['bar']} cv={s['cv']} "
                f"lay={s['layout']} safe={s['ns_safe_top']}/{s['qt_safe_top']}")
    t.note(f"baseline {res['baseline']}")
    t.note(f"windows {res['windows']}")
    t.note("reveal: " + "; ".join(line(s) for s in samples[:12]))
    t.note("reveal end: " + line(samples[-1]) + f" | analysis {res['analysis']}")
    t.note("hide: " + "; ".join(line(s) for s in hide[:12]) + " ... " + line(hide[-1]))
    t.note("bar tree: " + " | ".join(res["bar_tree"][:14]))

    revealed = any(r is not None and r >= MOVE_MIN for r in a["reveal"])
    why = ""
    if a["peak"] < MOVE_MIN:
        if revealed:
            why = " (the bar did reveal, so posted events work; the content stayed put)"
        elif not all(s["active"] for s in samples):
            why = (" (the bar never revealed: the test app was not the active app,"
                   " someone else's app is in front; rerun while the Mac is idle)")
        else:
            why = (" (the bar never revealed: posted mouse events need"
                   " Accessibility permission)")
    t.check("bar revealed and the content moved down", a["peak"] >= MOVE_MIN,
            f"max push {a['peak']} pt{why}")
    t.check("moved exactly once, never back while the bar was out (no flicker)",
            a["rises"] == 1 and a["falls"] == 0,
            f"rises={a['rises']} falls={a['falls']} cursor left the top edge: "
            f"{any(s['cursor'][1] > scr.top() + 4 for s in samples)}, app lost focus: "
            f"{not all(s['active'] for s in samples)}")
    hp = [s["push"] for s in hide if s["push"] is not None]
    back = bool(hp) and all(p <= 1 for p in hp[-5:])
    mono = all(b <= a_ + 1 for a_, b in zip(hp, hp[1:]))
    t.check("stayed down while the bar was out, slid back to 0 when it hid",
            bool(a["state"]) and all(a["state"][-10:]) and back and mono,
            f"out {a['push'][-5:]} hide {hp}")
    t.check("animated (intermediate positions seen), not a jump", len(a["steps"]) >= 3, a["steps"])
    gaps = a["gaps"] + _analyse(hide, win_top)["gaps"]
    tree_h = [ln for ln in res["bar_tree"] if "TitlebarContainerView" in ln]
    t.check("revealed bar is the plain title bar height (windowed title bar, within 2 pt)",
            abs(a["peak"] - title_h) <= 2.0,
            f"revealed {a['peak']} pt, windowed title bar {title_h} pt, container {tree_h[:1]}")
    t.check("content shift equals the bar's reveal in every sample (lockstep)",
            bool(gaps) and max(gaps) <= LOCKSTEP_TOL,
            f"{len(gaps)} samples, max excess gap {max(gaps) if gaps else None}")
