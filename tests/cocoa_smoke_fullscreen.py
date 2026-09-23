"""Real-Cocoa smoke run for fullscreen_inset.py's AppKit bridge (macOS).

Not part of the routine suite (it starts a Cocoa QApplication). Run:

    cd ~/dev/anki && QT_QPA_PLATFORM=cocoa out/pyenv/bin/python \
        <addon>/tests/cocoa_smoke_fullscreen.py

The QMainWindow is created but NEVER shown, activated or put in full
screen. The app's activation policy is set to Prohibited first, so no Dock
icon and no focus change. It proves, against the real AppKit:
  * the ctypes objc_msgSend signatures resolve (NSView -> NSWindow,
    styleMask, toolbar, toolbarStyle, NSNotificationCenter, NSApp options);
  * AppKit's own NSWindowDidEnterFullScreenNotification, posted through the
    real notification center, reaches the controller and attaches the empty
    toolbar (our delegate, no items, unified compact style);
  * Qt's window delegate answers window:willUseFullScreenPresentationOptions:
    with AutoHideToolbar for this window only;
  * NSWindowWillExitFullScreenNotification removes it and restores the
    style; the window is never made visible.
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
    print(f"ok  styleMask {mask:#x} fullSizeContentView={bool(mask & fi.STYLE_FULLSIZE_CONTENT)}"
          f" fullscreen={bool(mask & fi.STYLE_FULLSCREEN)}")
    assert not br.is_fullscreen(nswin)
    assert br.toolbar(nswin) == 0
    style0 = o.send(nswin, "toolbarStyle", restype=lg)
    opts0 = br.presentation_options()
    delegate = o.send(nswin, "delegate")
    print("    delegate", o.class_name(delegate), "implements willUseFullScreenPresentationOptions:",
          o.responds(delegate, "window:willUseFullScreenPresentationOptions:"))

    fi._controller = None
    ctl = fi.install(win, bridge=br)
    assert ctl is not None, "install failed"

    # Qt's delegate now answers the presentation options question, through
    # the real runtime: AutoHideToolbar for our window, others unchanged.
    sel = "window:willUseFullScreenPresentationOptions:"
    assert o.responds(delegate, sel), "options hook not added"
    proposed = fi.PRESENT_FULLSCREEN | fi.PRESENT_AUTOHIDE_MENUBAR | (1 << 0)
    got = o.send(delegate, sel, nswin, proposed, restype=ul, argtypes=(vp, ul))
    assert got == proposed | fi.PRESENT_AUTOHIDE_TOOLBAR, hex(got)
    other = o.send(delegate, sel, 0x10, proposed, restype=ul, argtypes=(vp, ul))
    assert other == proposed, hex(other)
    odd = o.send(delegate, sel, nswin, fi.PRESENT_FULLSCREEN, restype=ul, argtypes=(vp, ul))
    assert odd == fi.PRESENT_FULLSCREEN, hex(odd)
    print(f"ok  delegate options hook: {proposed:#x} -> {got:#x} (other windows unchanged)")

    center = o.send(o.cls("NSNotificationCenter"), "defaultCenter")

    def post(name):
        o.send(center, "postNotificationName:object:", o.nsstring(name), nswin,
               restype=None, argtypes=(vp, vp))

    post(fi.NOTE_DID_ENTER)
    tb = br.toolbar(nswin)
    assert tb and ctl.attached, "did-enter did not attach the toolbar"
    ident = o.send(o.send(tb, "identifier"), "UTF8String", restype=ctypes.c_char_p).decode()
    items = o.send(o.send(tb, "items"), "count", restype=ul)
    style = o.send(nswin, "toolbarStyle", restype=lg)
    tdel = o.send(tb, "delegate")
    assert ident == fi.TOOLBAR_ID, ident
    assert items == 0, items
    assert style == fi.TOOLBAR_STYLE_UNIFIED_COMPACT, style
    assert tdel == br._helper, "toolbar delegate is not ours"
    assert br.presentation_options() == opts0, "options changed outside full screen"
    print(f"ok  did-enter: toolbar {ident!r}, {items} items, style {style}, delegate {o.class_name(tdel)}")

    post(fi.NOTE_DID_ENTER)  # idempotent
    assert br.toolbar(nswin) == tb

    post(fi.NOTE_WILL_EXIT)
    assert br.toolbar(nswin) == 0 and not ctl.attached, "will-exit did not detach"
    assert o.send(nswin, "toolbarStyle", restype=lg) == style0
    post(fi.NOTE_DID_EXIT)
    print(f"ok  will-exit: toolbar removed, style restored to {style0}")

    # A second cycle reuses the same toolbar object.
    post(fi.NOTE_DID_ENTER)
    assert br.toolbar(nswin) == tb
    post(fi.NOTE_WILL_EXIT)
    assert br.toolbar(nswin) == 0

    visible = o.send(nswin, "isVisible", restype=bl)
    assert not visible, "window became visible"
    assert not win.isVisible()
    print("ok  window never visible")
    win.destroy()
    print("PASS cocoa_smoke_fullscreen")


if __name__ == "__main__":
    main()
