"""Real-Cocoa smoke run for fullscreen_inset.py's AppKit bridge (macOS).

Not part of the routine suite (it starts a Cocoa QApplication). Run:

    cd ~/dev/anki && QT_QPA_PLATFORM=cocoa out/pyenv/bin/python \
        <addon>/tests/cocoa_smoke_fullscreen.py

The QMainWindow is created but NEVER shown, activated or put in full
screen. The app's activation policy is set to Prohibited first, so no Dock
icon and no focus change. It proves, against the real AppKit:
  * the ctypes objc_msgSend signatures resolve (NSView -> NSWindow,
    styleMask, NSNotificationCenter, frames, notifications);
  * AppKit's own NSWindowDidEnterFullScreenNotification, posted through the
    real notification center, starts following the bar without attaching a
    toolbar, touching the presentation options or Qt's window delegate;
  * a move of a window of AppKit's full screen bar class, posted as a real
    NSWindowDidMoveNotification, moves the content view by the bar's
    reveal, step by step, and will-exit puts it back and stops following;
  * the window is never made visible.
"""

import ctypes
import os
import sys

os.environ["QT_QPA_PLATFORM"] = "cocoa"
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SRC = os.path.expanduser(os.environ.get("ANKI_SRC", "~/dev/anki"))
sys.path[:0] = [ROOT] + [os.path.join(SRC, p) for p in ("pylib", "out/pylib", "qt", "out/qt")]

from aqt.qt import QApplication, QMainWindow, QWidget  # noqa: E402

import fullscreen_inset as fi  # noqa: E402


def make_bar_window(o):
    """A never-shown NSWindow whose class name looks like AppKit's full
    screen bar window."""
    L = o.lib
    name = b"SmokeNSToolbarFullScreenWindow"
    cls = o.cls(name.decode())
    if not cls:
        cls = L.objc_allocateClassPair(o.cls("NSWindow"), name, 0)
        L.objc_registerClassPair(cls)
    R = fi._Rect.get()
    r = R()
    r.origin.x, r.origin.y, r.size.w, r.size.h = 10.0, 500.0, 300.0, 40.0
    w = o.send(o.send(cls, "alloc"), "initWithContentRect:styleMask:backing:defer:",
               r, 0, 2, True,
               argtypes=(R, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_bool))
    o.send(w, "setReleasedWhenClosed:", False, restype=None, argtypes=(ctypes.c_bool,))
    return w


def main():
    app = QApplication.instance() or QApplication(sys.argv)
    assert QApplication.platformName() == "cocoa", QApplication.platformName()
    br = fi._make_bridge()
    assert isinstance(br, fi.CocoaBridge), f"no bridge: {br!r}"
    o = br.o
    vp, ul, lg, bl = ctypes.c_void_p, ctypes.c_ulong, ctypes.c_long, ctypes.c_bool
    nsapp = br._app()
    o.send(nsapp, "setActivationPolicy:", 2, restype=bl, argtypes=(lg,))  # Prohibited

    win = QMainWindow()
    win.setWindowTitle("cocoa smoke (never shown)")
    win.setCentralWidget(QWidget())
    nswin = br.nswindow(win)
    assert nswin, "NSView -> NSWindow failed"
    print("ok  NSWindow", hex(nswin), o.class_name(nswin))
    mask = br.style_mask(nswin)
    print(f"ok  styleMask {mask:#x} fullscreen={bool(mask & fi.STYLE_FULLSCREEN)}")
    assert not br.is_fullscreen(nswin)
    assert not o.send(nswin, "toolbar")
    opts0 = int(o.send(nsapp, "presentationOptions", restype=ul))
    delegate = o.send(nswin, "delegate")
    hook0 = o.responds(delegate, "window:willUseFullScreenPresentationOptions:")

    fi._controller = None
    ctl = fi.install(win, bridge=br)
    assert ctl is not None, "install failed"

    center = o.send(o.cls("NSNotificationCenter"), "defaultCenter")

    def post(name):
        o.send(center, "postNotificationName:object:", o.nsstring(name), nswin,
               restype=None, argtypes=(vp, vp))

    post(fi.NOTE_DID_ENTER)
    assert ctl.following, "did-enter did not start following the bar"
    assert not o.send(nswin, "toolbar"), "a toolbar was attached"
    assert int(o.send(nsapp, "presentationOptions", restype=ul)) == opts0, "options changed"
    assert o.responds(delegate, "window:willUseFullScreenPresentationOptions:") == hook0, \
        "Qt's window delegate was modified"
    print("ok  did-enter: following the bar; no toolbar, options and delegate untouched")
    post(fi.NOTE_DID_ENTER)  # idempotent

    # The content follows AppKit's bar window: a real (never shown) window
    # whose class name matches, moved through the real notification center.
    bar = make_bar_window(o)
    assert fi.BAR_WINDOW_CLASS in o.class_name(bar)
    assert br.bar_bottom(bar) is None, "an invisible bar must count as hidden"
    R = fi._Rect.get()
    cv = o.send(nswin, "contentView")
    rest = o.send(cv, "frame", restype=R).origin.y
    real_owner, real_bottom = br.bar_owner, br.bar_bottom
    top = br.content_top(nswin)
    bottoms = {"y": top}
    br.bar_owner = lambda w: int(nswin) if w == bar else real_owner(w)
    br.bar_bottom = lambda w: bottoms["y"]
    seen = []
    for step in (12.0, 30.0, 54.0, 54.0, 20.0, 0.0):
        bottoms["y"] = top - step
        o.send(bar, "setFrameOrigin:", R().origin.__class__(10.0, 500.0 + step),
               restype=None, argtypes=(R().origin.__class__,))
        y = o.send(cv, "frame", restype=R).origin.y
        seen.append((step, ctl.offset, rest - y))
        assert abs(ctl.offset - step) < 0.01 and abs((rest - y) - step) < 0.01, seen
    print(f"ok  content follows the bar window's moves: {seen}")
    bottoms["y"] = top - 40.0
    o.send(bar, "setFrameOrigin:", R().origin.__class__(10.0, 700.0),
           restype=None, argtypes=(R().origin.__class__,))
    assert ctl.offset == 40.0
    br.bar_owner, br.bar_bottom = real_owner, real_bottom

    post(fi.NOTE_WILL_EXIT)
    assert not ctl.following, "will-exit did not stop following"
    assert ctl.offset == 0 and o.send(cv, "frame", restype=R).origin.y == rest, \
        "will-exit did not put the content view back"
    assert not br._bars_observed, "bar observer left installed"
    o.send(bar, "setFrameOrigin:", R().origin.__class__(10.0, 300.0),
           restype=None, argtypes=(R().origin.__class__,))
    assert o.send(cv, "frame", restype=R).origin.y == rest
    post(fi.NOTE_DID_EXIT)
    assert not o.send(nswin, "toolbar")
    print("ok  will-exit: content view back in place, bar no longer followed")

    visible = o.send(nswin, "isVisible", restype=bl)
    assert not visible, "window became visible"
    assert not win.isVisible()
    print("ok  window never visible")
    win.destroy()
    print("PASS cocoa_smoke_fullscreen")


if __name__ == "__main__":
    main()
