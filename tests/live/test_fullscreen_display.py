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
  1. enters macOS full screen on the main window and waits for AppKit's
     did-enter (fullscreen_inset attaches its empty toolbar then);
  2. with the bar hidden, records the baseline: the content view's top in
     screen coordinates and the sidebar wordmark's y relative to the
     full screen window (no strip: the content starts at the window top);
  3. moves the cursor to the top edge of that screen with Quartz
     (CGWarpMouseCursorPosition + a posted kCGEventMouseMoved, via ctypes);
  4. samples every 50 ms for 3 s: the wordmark's window-relative y, the
     content view's height, and the bottom edge of AppKit's revealed
     full screen toolbar window;
  5. asserts the content moved down exactly once (one rise, no fall, so no
     flicker), stayed down, and that at every sample where the bar was out
     the content top sat on the bar's bottom edge (lockstep, within 2 pt);
  6. moves the cursor back where it was and leaves full screen.

Posting mouse events needs Accessibility permission for the Python binary
(System Settings > Privacy & Security > Accessibility). Without it only the
warp happens; if the bar never reveals, the "bar revealed" check fails and
says so. arm64 only (NSRect is returned in registers there).
"""

from __future__ import annotations

import ctypes
import os
import platform
import sys
import time

NEEDS_DISPLAY = True

SAMPLE_MS = 50
SAMPLE_S = 3.0
LOCKSTEP_TOL = 2.0   # pt
MOVE_MIN = 20.0      # pt: the smallest push that counts as "the bar came out"


class NSPoint(ctypes.Structure):
    _fields_ = [("x", ctypes.c_double), ("y", ctypes.c_double)]


class NSSize(ctypes.Structure):
    _fields_ = [("w", ctypes.c_double), ("h", ctypes.c_double)]


class NSRect(ctypes.Structure):
    _fields_ = [("origin", NSPoint), ("size", NSSize)]


class CGPoint(ctypes.Structure):
    _fields_ = [("x", ctypes.c_double), ("y", ctypes.c_double)]


def _fi():
    for k, m in sys.modules.items():
        if k.endswith(".fullscreen_inset") and hasattr(m, "CocoaBridge"):
            return m
    return None


def _quartz():
    cg = ctypes.cdll.LoadLibrary(
        "/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices")
    cg.CGWarpMouseCursorPosition.argtypes = [CGPoint]
    cg.CGWarpMouseCursorPosition.restype = ctypes.c_int32
    cg.CGEventCreateMouseEvent.argtypes = [ctypes.c_void_p, ctypes.c_uint32, CGPoint, ctypes.c_uint32]
    cg.CGEventCreateMouseEvent.restype = ctypes.c_void_p
    cg.CGEventPost.argtypes = [ctypes.c_uint32, ctypes.c_void_p]
    cg.CGEventPost.restype = None
    cg.CGEventCreate.argtypes = [ctypes.c_void_p]
    cg.CGEventCreate.restype = ctypes.c_void_p
    cg.CGEventGetLocation.argtypes = [ctypes.c_void_p]
    cg.CGEventGetLocation.restype = CGPoint
    cg.CFRelease.argtypes = [ctypes.c_void_p]
    cg.CFRelease.restype = None
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


def run(t):
    if os.environ.get("ANKI_LIVE_DISPLAY") != "1":
        t.note("skipped: set ANKI_LIVE_DISPLAY=1 to run on the real display")
        t.check("skipped (opt-in real-display test)", True)
        return
    if platform.machine() != "arm64":
        t.check("arm64 Mac", False, platform.machine())
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
    nswin = br.nswindow(mw)
    view = int(mw.winId())

    def content_rect():
        """Content view rect in Cocoa screen coords (origin bottom-left)."""
        b = o.send(view, "bounds", restype=NSRect)
        r = o.send(view, "convertRect:toView:", b, None, restype=NSRect,
                   argtypes=(NSRect, ctypes.c_void_p))
        return o.send(nswin, "convertRectToScreen:", r, restype=NSRect, argtypes=(NSRect,))

    def win_frame():
        return o.send(nswin, "frame", restype=NSRect)

    def bar_bottom():
        """Bottom edge (Cocoa y) of AppKit's full screen toolbar window, or
        None while it is not on screen."""
        wins = o.send(br._app(), "windows")
        n = o.send(wins, "count", restype=ctypes.c_ulong)
        for i in range(n):
            w = o.send(wins, "objectAtIndex:", i, argtypes=(ctypes.c_ulong,))
            name = o.class_name(w)
            if "FullScreen" in name and "Toolbar" in name:
                if not o.send(w, "isVisible", restype=ctypes.c_bool):
                    return None
                f = o.send(w, "frame", restype=NSRect)
                return f.origin.y
        return None

    cg = _quartz()
    cursor0 = _cursor(cg)
    try:
        mw.setWindowState(mw.windowState() | Qt.WindowState.WindowFullScreen)
        entered = t.wait_until(lambda: br.is_fullscreen(nswin) and ctl.attached, timeout=10)
        t.check("entered full screen and the toolbar attached on did-enter", entered,
                f"fullscreen={br.is_fullscreen(nswin)} attached={ctl.attached}")
        if not entered:
            return
        opts = br.presentation_options()
        t.check("AutoHideToolbar in effect", bool(opts & fi.PRESENT_AUTOHIDE_TOOLBAR), hex(opts))

        # Park the cursor mid-screen so the bar is hidden, then baseline.
        scr = mw.screen().geometry()
        mid_x = scr.left() + scr.width() / 2
        _move_cursor(cg, mid_x, scr.top() + scr.height() / 2)
        t.pump(1500)
        wf = win_frame()
        c0 = content_rect()
        top0 = c0.origin.y + c0.size.height
        win_top = wf.origin.y + wf.size.height
        t.check("no strip: with the bar hidden the content starts at the window top",
                abs(win_top - top0) <= 1.0, f"window top {win_top} content top {top0}")
        t.check("no Qt contents margin", mw.contentsMargins().top() == 0,
                mw.contentsMargins().top())
        el = t.element_center(".ba-side-mark", frac_y=0.0)
        mark_off = None
        if el is not None:
            mark_off = mw.web.mapTo(mw, el).y()
        t.check("sidebar wordmark found", mark_off is not None, "")

        def mark_y(ctop):
            # window-relative y (from the full screen window's top, down)
            return (win_top - ctop) + (mark_off or 0)

        _move_cursor(cg, mid_x, scr.top())
        samples = []
        end = time.monotonic() + SAMPLE_S
        while time.monotonic() < end:
            c = content_rect()
            ctop = c.origin.y + c.size.height
            samples.append((round(mark_y(ctop), 1), round(c.size.height, 1), bar_bottom()))
            t.pump(SAMPLE_MS)
        t.note(f"samples (wordmark y, content h, bar bottom): {samples}")

        base = mark_y(top0)
        moved = [s[0] - base for s in samples]
        peak = max(moved) if moved else 0
        t.check("bar revealed and the content moved down", peak >= MOVE_MIN,
                f"max push {peak:.1f} pt (Accessibility permission needed for posted events)")
        half = peak / 2
        state = [m >= half for m in moved]
        rises = sum(1 for a, b in zip(state, state[1:]) if not a and b) + (1 if state and state[0] else 0)
        falls = sum(1 for a, b in zip(state, state[1:]) if a and not b)
        t.check("moved exactly once, never back (no flicker)", rises == 1 and falls == 0,
                f"rises={rises} falls={falls}")
        t.check("stayed down to the end of the sampling", bool(state) and all(state[-10:]),
                moved[-10:])
        steps = sorted({round(m) for m in moved})
        t.check("animated (intermediate positions seen), not a jump", len(steps) >= 3, steps)
        lock = [abs((s[2]) - (win_top - (s[0] - (mark_off or 0))))
                for s in samples if s[2] is not None]
        t.check("content top sits on the bar's bottom edge in every sample (lockstep)",
                bool(lock) and max(lock) <= LOCKSTEP_TOL,
                f"{len(lock)} samples, max gap {max(lock) if lock else None}")
    finally:
        _move_cursor(cg, *cursor0)
        mw.setWindowState(mw.windowState() & ~Qt.WindowState.WindowFullScreen)
        t.wait_until(lambda: not br.is_fullscreen(nswin), timeout=10)
        t.pump(1000)
        t.check("left full screen and the toolbar is gone",
                not br.is_fullscreen(nswin) and not ctl.attached and br.toolbar(nswin) == 0,
                f"fullscreen={br.is_fullscreen(nswin)} attached={ctl.attached}")
