"""Anki Design: Safari-style title bar reveal in macOS full screen.

In macOS full screen the "Anki+" title bar is hidden until the cursor
reaches the top edge. Anki+ keeps the standard macOS title bar (a plain
titled QNSWindow: styleMask 0xf, no full-size content view, no toolbar), and
for a window like that AppKit slides the revealed bar OVER the content. That
is what covered the sidebar wordmark (ticket 20260922-232122).

Two earlier fixes pushed the content by hand and both were wrong:
  * e248729 polled the cursor every 40 ms and set a top margin while the bar
    was out; the relayout under the cursor made macOS retract the bar, and
    the bar flickered twice a second (ticket 20260923-102004).
  * 8954331 reserved a static strip for the whole of full screen and painted
    it in the page colour: a dark band over the scene backdrop and the whole
    app pushed down (ticket 20260923-105203).

A third fix (539a2af) attached an auto-hiding NSToolbar and expected
AppKit to move the content with it. It does not: in macOS 26 AppKit's full
screen reveal (_NSFullScreenToolbarRevealAnimation driving
_NSFullScreenMenuBarCompanionController) only reshapes the separate
NSToolbarFullScreenWindow that hosts the title bar, one setFrame:display:
per animation step; it never moves or re-lays out the app's own window.
The toolbar also made the bar 38 pt instead of the 28 pt title bar, and
where AutoHideToolbar did not take it stayed parked open (ticket
20260923-115841). It is gone: no toolbar, no presentation options, no
delegate hook. The title bar is the plain system one, auto-hidden by AppKit
exactly as before any of these fixes.

What this module does, driven only by AppKit:

  * NSWindowDidEnterFullScreenNotification: start following AppKit's bar
    window (the NSToolbarFullScreenWindow AppKit makes a child of the full
    screen window; for a plain window it holds just the title bar).
  * NSWindowDidMove/DidResizeNotification of that window (every step of
    AppKit's own reveal and hide animation posts one): the content view is
    placed so its top edge sits on the bar's visible bottom (the title bar
    container, measured each time), in the same run loop turn as the bar's
    frame change, so both land in the same frame. The view keeps its size
    (nothing re-lays out or re-renders; the bottom slides off screen for as
    long as the bar is out).
  * NSWindowWillExitFullScreenNotification: put the content view back and
    stop following, so the exit animation and the windowed title bar are
    exactly as before.

Nothing else. No timer, no cursor read, no Qt contents margin, no painted
strip. The content only moves when AppKit moves the bar, by exactly as much.

The AppKit calls go through the Objective-C runtime with ctypes (PyObjC is
not in the Anki pyenv). Every call has an explicit signature, and every step
is guarded: if anything cannot be resolved the module does nothing and the
window keeps plain macOS behaviour. Off with the `fullscreen_bar_inset`
config key (name kept so existing configs still switch it).
"""

from __future__ import annotations

import sys
from typing import Any, Callable, Dict, Optional

# NSWindowStyleMask
STYLE_FULLSCREEN = 1 << 14

NOTE_DID_ENTER = "NSWindowDidEnterFullScreenNotification"
NOTE_WILL_EXIT = "NSWindowWillExitFullScreenNotification"
NOTE_DID_EXIT = "NSWindowDidExitFullScreenNotification"
NOTE_DID_MOVE = "NSWindowDidMoveNotification"
NOTE_DID_RESIZE = "NSWindowDidResizeNotification"

# AppKit's full screen title bar / toolbar window (the "companion" window
# that slides down with the menu bar). Matched by class name.
BAR_WINDOW_CLASS = "ToolbarFullScreenWindow"
BAR_VIEW_CLASS = "TitlebarContainerView"
MAX_REVEAL = 200.0  # pt: anything larger is not a title bar


class _Rect:
    """NSRect for ctypes, built lazily (ctypes is imported on demand)."""
    _type: Any = None

    @classmethod
    def get(cls) -> Any:
        if cls._type is None:
            import ctypes

            class NSPoint(ctypes.Structure):
                _fields_ = [("x", ctypes.c_double), ("y", ctypes.c_double)]

            class NSSize(ctypes.Structure):
                _fields_ = [("w", ctypes.c_double), ("h", ctypes.c_double)]

            class NSRect(ctypes.Structure):
                _fields_ = [("origin", NSPoint), ("size", NSSize)]

            cls._type = NSRect
        return cls._type


def reveal_amount(content_top: float, bar_bottom: Optional[float]) -> float:
    """How far the revealed bar reaches below the content window's top edge
    (pt), clamped to [0, MAX_REVEAL]. None (bar not on screen) is 0."""
    if bar_bottom is None:
        return 0.0
    r = float(content_top) - float(bar_bottom)
    if r <= 0.5:
        return 0.0
    return min(r, MAX_REVEAL)


# --------------------------------------------------------------------------- #
# Objective-C runtime through ctypes
# --------------------------------------------------------------------------- #

class _ObjC:
    """Minimal typed objc_msgSend. Loaded lazily; raises on any failure so
    the caller can fall back to doing nothing."""

    def __init__(self) -> None:
        import ctypes
        import ctypes.util

        self.ct = ctypes
        lib = ctypes.util.find_library("objc")
        if not lib:
            raise OSError("libobjc not found")
        self.lib = ctypes.cdll.LoadLibrary(lib)
        ctypes.cdll.LoadLibrary(
            "/System/Library/Frameworks/AppKit.framework/AppKit"
        )
        L = self.lib
        vp, cp = ctypes.c_void_p, ctypes.c_char_p
        L.objc_getClass.restype = vp
        L.objc_getClass.argtypes = [cp]
        L.sel_registerName.restype = vp
        L.sel_registerName.argtypes = [cp]
        L.objc_allocateClassPair.restype = vp
        L.objc_allocateClassPair.argtypes = [vp, cp, ctypes.c_size_t]
        L.objc_registerClassPair.restype = None
        L.objc_registerClassPair.argtypes = [vp]
        L.class_addMethod.restype = ctypes.c_bool
        L.class_addMethod.argtypes = [vp, vp, vp, cp]
        L.object_getClass.restype = vp
        L.object_getClass.argtypes = [vp]
        L.class_getName.restype = cp
        L.class_getName.argtypes = [vp]
        L.class_getInstanceMethod.restype = vp
        L.class_getInstanceMethod.argtypes = [vp, vp]
        self._msg = ctypes.cast(L.objc_msgSend, vp).value
        self._protos: Dict[Any, Any] = {}

    def cls(self, name: str) -> int:
        return self.lib.objc_getClass(name.encode()) or 0

    def sel(self, name: str) -> int:
        return self.lib.sel_registerName(name.encode())

    def send(self, obj: int, sel: str, *args: Any,
             restype: Any = "p", argtypes: tuple = ()) -> Any:
        ct = self.ct
        rt = ct.c_void_p if restype == "p" else restype
        key = (rt, argtypes)
        fn = self._protos.get(key)
        if fn is None:
            fn = ct.CFUNCTYPE(rt, ct.c_void_p, ct.c_void_p, *argtypes)(self._msg)
            self._protos[key] = fn
        return fn(obj, self.sel(sel), *args)

    def responds(self, obj: int, sel: str) -> bool:
        return bool(self.send(obj, "respondsToSelector:", self.sel(sel),
                              restype=self.ct.c_bool,
                              argtypes=(self.ct.c_void_p,)))

    def nsstring(self, s: str) -> int:
        return self.send(self.cls("NSString"), "stringWithUTF8String:",
                         s.encode(), argtypes=(self.ct.c_char_p,))

    def class_name(self, obj: int) -> str:
        return (self.lib.class_getName(self.lib.object_getClass(obj)) or b"").decode()


class CocoaBridge:
    """The AppKit side: one runtime class that observes the window's full
    screen notifications and the moves of AppKit's bar window."""

    CLASS_NAME = "AnkiPlusFullscreenReveal"

    def __init__(self) -> None:
        self.o = _ObjC()
        self._handlers: Dict[int, Callable[[str], None]] = {}
        self._helper_cls = self._make_class()
        self._helper = self.o.send(
            self.o.send(self._helper_cls, "alloc"), "init")
        if not self._helper:
            raise RuntimeError("helper init failed")
        self._bar_handlers: Dict[int, Callable[[int], None]] = {}
        self._bars_observed = False
        self._content_rest: Dict[int, float] = {}

    # -- runtime class -------------------------------------------------------
    def _make_class(self) -> int:
        o = self.o
        existing = o.cls(self.CLASS_NAME)
        if existing:
            # Same process, second bridge (tests): reuse the class. Its IMPs
            # dispatch to whichever bridge is current in _SHARED.
            _SHARED["bridge"] = self
            return existing
        cls = o.lib.objc_allocateClassPair(o.cls("NSObject"),
                                           self.CLASS_NAME.encode(), 0)
        if not cls:
            raise RuntimeError("objc_allocateClassPair failed")
        ct = o.ct
        vp = ct.c_void_p
        # The IMPs live in the module-level _SHARED for the life of the
        # process: the class keeps raw pointers to them.
        imps = _SHARED["imps"] = (
            ct.CFUNCTYPE(None, vp, vp, vp)(CocoaBridge._on_note),
            ct.CFUNCTYPE(None, vp, vp, vp)(CocoaBridge._on_bar_note),
        )
        note_imp, bar_imp = (ct.cast(f, vp) for f in imps)
        add = o.lib.class_addMethod
        ok = add(cls, o.sel("fullscreenNote:"), note_imp, b"v@:@")
        ok &= add(cls, o.sel("barNote:"), bar_imp, b"v@:@")
        if not ok:
            raise RuntimeError("class_addMethod failed")
        o.lib.objc_registerClassPair(cls)
        _SHARED["bridge"] = self
        return cls

    # IMPs. Called by AppKit on the main thread; never raise.
    @staticmethod
    def _on_note(_self: int, _cmd: int, note: int) -> None:
        br = _SHARED.get("bridge")
        if br is None:
            return
        try:
            o = br.o
            name_ns = o.send(note, "name")
            name = o.send(name_ns, "UTF8String", restype=o.ct.c_char_p)
            win = o.send(note, "object") or 0
            h = br._handlers.get(int(win))
            if h is not None and name:
                h(name.decode())
        except Exception:
            pass

    @staticmethod
    def _on_bar_note(_self: int, _cmd: int, note: int) -> None:
        """A window moved or resized. If it is AppKit's full screen bar
        window of one of our windows, hand it to that window's handler."""
        br = _SHARED.get("bridge")
        if br is None or not br._bar_handlers:
            return
        try:
            win = int(br.o.send(note, "object") or 0)
            if not win or BAR_WINDOW_CLASS not in br.o.class_name(win):
                return
            h = br._bar_handlers.get(br.bar_owner(win))
            if h is not None:
                h(win)
        except Exception:
            pass

    # -- window --------------------------------------------------------------
    def nswindow(self, win: Any) -> int:
        """The NSWindow behind a top-level QWidget (winId() is its NSView)."""
        view = int(win.winId())
        if not view:
            return 0
        o = self.o
        if not o.responds(view, "window"):
            return 0
        return o.send(view, "window") or 0

    def style_mask(self, nswin: int) -> int:
        return int(self.o.send(nswin, "styleMask", restype=self.o.ct.c_ulong))

    def is_fullscreen(self, nswin: int) -> bool:
        return bool(self.style_mask(nswin) & STYLE_FULLSCREEN)

    def observe(self, nswin: int, handler: Callable[[str], None]) -> None:
        o = self.o
        vp = o.ct.c_void_p
        self._handlers[int(nswin)] = handler
        center = o.send(o.cls("NSNotificationCenter"), "defaultCenter")
        for name in (NOTE_DID_ENTER, NOTE_WILL_EXIT, NOTE_DID_EXIT):
            o.send(center, "addObserver:selector:name:object:",
                   self._helper, o.sel("fullscreenNote:"), o.nsstring(name), nswin,
                   restype=None, argtypes=(vp, vp, vp, vp))

    def unobserve(self, nswin: int) -> None:
        o = self.o
        vp = o.ct.c_void_p
        self._handlers.pop(int(nswin), None)
        center = o.send(o.cls("NSNotificationCenter"), "defaultCenter")
        o.send(center, "removeObserver:name:object:", self._helper, None, nswin,
               restype=None, argtypes=(vp, vp, vp))

    # -- the full screen bar window and the content that follows it ---------
    def observe_bars(self, nswin: int, handler: Optional[Callable[[int], None]]) -> None:
        """Start (handler) or stop (None) following the bar window of
        `nswin`. One app-wide observer for window moves/resizes, filtered to
        AppKit's bar windows, so a bar window AppKit creates or replaces
        later is followed too."""
        o = self.o
        vp = o.ct.c_void_p
        if handler is None:
            self._bar_handlers.pop(int(nswin), None)
        else:
            self._bar_handlers[int(nswin)] = handler
        want = bool(self._bar_handlers)
        if want == self._bars_observed:
            return
        center = o.send(o.cls("NSNotificationCenter"), "defaultCenter")
        for name in (NOTE_DID_MOVE, NOTE_DID_RESIZE):
            if want:
                o.send(center, "addObserver:selector:name:object:",
                       self._helper, o.sel("barNote:"), o.nsstring(name), None,
                       restype=None, argtypes=(vp, vp, vp, vp))
            else:
                o.send(center, "removeObserver:name:object:",
                       self._helper, o.nsstring(name), None,
                       restype=None, argtypes=(vp, vp, vp))
        self._bars_observed = want

    def bar_owner(self, barwin: int) -> int:
        """The app window a bar window belongs to (AppKit makes it a child
        of the full screen window)."""
        o = self.o
        parent = int(o.send(barwin, "parentWindow") or 0)
        if parent in self._bar_handlers:
            return parent
        if o.responds(barwin, "_originalWindow"):
            return int(o.send(barwin, "_originalWindow") or 0)
        return parent

    def _app(self) -> int:
        o = self.o
        return o.send(o.cls("NSApplication"), "sharedApplication") or 0

    def find_bar(self, nswin: int) -> int:
        o = self.o
        ul = o.ct.c_ulong
        wins = o.send(self._app(), "windows")
        for i in range(o.send(wins, "count", restype=ul) if wins else 0):
            w = int(o.send(wins, "objectAtIndex:", i, argtypes=(ul,)) or 0)
            if w and BAR_WINDOW_CLASS in o.class_name(w) and self.bar_owner(w) == int(nswin):
                return w
        return 0

    def _screen_rect(self, view: int) -> Any:
        o = self.o
        R = _Rect.get()
        vp = o.ct.c_void_p
        b = o.send(view, "bounds", restype=R)
        r = o.send(view, "convertRect:toView:", b, None, restype=R, argtypes=(R, vp))
        win = o.send(view, "window")
        return o.send(win, "convertRectToScreen:", r, restype=R, argtypes=(R,))

    def _find_view(self, view: int, cls_part: str, depth: int = 0) -> int:
        o = self.o
        ul = o.ct.c_ulong
        if not view or depth > 5:
            return 0
        if cls_part in o.class_name(view):
            return view
        subs = o.send(view, "subviews")
        for i in range(o.send(subs, "count", restype=ul) if subs else 0):
            hit = self._find_view(o.send(subs, "objectAtIndex:", i, argtypes=(ul,)),
                                  cls_part, depth + 1)
            if hit:
                return hit
        return 0

    def bar_bottom(self, barwin: int) -> Optional[float]:
        """Screen y (Cocoa, bottom-left origin) of the bar's visible bottom
        edge: the title bar container AppKit moved into the bar window, or
        the window's own frame. None while the bar window is not shown."""
        o = self.o
        ct = o.ct
        if not barwin or not o.send(barwin, "isVisible", restype=ct.c_bool):
            return None
        if float(o.send(barwin, "alphaValue", restype=ct.c_double)) <= 0.01:
            return None
        v = self._find_view(o.send(barwin, "contentView") or 0, BAR_VIEW_CLASS)
        if v:
            return float(self._screen_rect(v).origin.y)
        return float(o.send(barwin, "frame", restype=_Rect.get()).origin.y)

    def content_top(self, nswin: int) -> float:
        f = self.o.send(nswin, "frame", restype=_Rect.get())
        return float(f.origin.y + f.size.h)

    def set_content_offset(self, nswin: int, offset: float) -> None:
        """Place the window's content view `offset` pt below where AppKit
        laid it out (same size, so nothing inside re-lays out). 0 puts it
        back exactly."""
        o = self.o
        R = _Rect.get()
        key = int(nswin)
        cv = o.send(nswin, "contentView")
        if not cv:
            return
        f = o.send(cv, "frame", restype=R)
        if key not in self._content_rest:
            if offset <= 0:
                return
            self._content_rest[key] = float(f.origin.y)
        rest = self._content_rest[key]
        y = rest - float(offset)
        if abs(f.origin.y - y) >= 0.25:
            o.send(cv, "setFrameOrigin:", type(f.origin)(f.origin.x, y),
                   restype=None, argtypes=(type(f.origin),))
        if offset <= 0:
            self._content_rest.pop(key, None)

_SHARED: Dict[str, Any] = {}


def _make_bridge() -> Optional[CocoaBridge]:
    """The AppKit bridge when running on the real macOS (cocoa) platform,
    else None (offscreen, Linux, Windows, tests)."""
    if sys.platform != "darwin":
        return None
    try:
        from aqt.qt import QGuiApplication
        if QGuiApplication.platformName() != "cocoa":
            return None
    except Exception:
        return None
    try:
        return CocoaBridge()
    except Exception as e:  # pragma: no cover - depends on the OS
        try:
            print(f"[anki-design] fullscreen reveal unavailable: {e}", flush=True)
        except Exception:
            pass
        return None


# --------------------------------------------------------------------------- #
# Qt glue
# --------------------------------------------------------------------------- #

def _config() -> dict:
    try:
        from aqt import mw
        return mw.addonManager.getConfig(__name__.split(".")[0]) or {}
    except Exception:
        return {}


def _make_controller_class() -> Any:
    from aqt.qt import QEvent, QObject

    class _FullscreenReveal(QObject):
        """Follows AppKit's bar window while full screen. Reacts only to
        AppKit's notifications (and, as a fallback for the exit, to Qt's
        WindowStateChange after them)."""

        def __init__(self, win: Any, bridge: Any) -> None:
            super().__init__(win)
            self._win = win
            self._bridge = bridge
            self._nswin = 0
            self.following = False
            self.offset = 0.0
            win.installEventFilter(self)
            self._bind()
            try:
                win.destroyed.connect(self._unbind)
            except Exception:
                pass

        # -- binding to the NSWindow ------------------------------------------
        def _bind(self) -> None:
            br = self._bridge
            if br is None:
                return
            try:
                nswin = br.nswindow(self._win)
            except Exception:
                nswin = 0
            if not nswin or nswin == self._nswin:
                return
            self._unbind()
            try:
                br.observe(nswin, self._on_note)
                self._nswin = nswin
                if br.is_fullscreen(nswin):
                    self._enter()
            except Exception:
                self._nswin = 0

        def _unbind(self, *_: Any) -> None:
            if self._nswin and self._bridge is not None:
                try:
                    self._exit()
                    self._bridge.unobserve(self._nswin)
                except Exception:
                    pass
            self._nswin = 0

        # -- AppKit notifications -------------------------------------------
        def _on_note(self, name: str) -> None:
            if name == NOTE_DID_ENTER:
                self._enter()
            elif name in (NOTE_WILL_EXIT, NOTE_DID_EXIT):
                self._exit()

        def _enter(self) -> None:
            if self.following or not self._nswin:
                return
            self.following = True
            try:
                self._bridge.observe_bars(self._nswin, self._on_bar)
                bar = self._bridge.find_bar(self._nswin)
                if bar:
                    self._on_bar(bar)
            except Exception:
                pass

        def _on_bar(self, barwin: int) -> None:
            """AppKit moved its bar window (a step of its own reveal or hide
            animation): put the content's top edge on the bar's bottom."""
            if not self.following or not self._nswin:
                return
            br = self._bridge
            try:
                r = reveal_amount(br.content_top(self._nswin), br.bar_bottom(barwin))
                if abs(r - self.offset) < 0.25:
                    return
                br.set_content_offset(self._nswin, r)
                self.offset = r
            except Exception:
                pass

        def _exit(self) -> None:
            if not self.following or not self._nswin:
                return
            self.following = False
            try:
                self._bridge.observe_bars(self._nswin, None)
                self._bridge.set_content_offset(self._nswin, 0.0)
            except Exception:
                pass
            self.offset = 0.0

        # -- Qt events --------------------------------------------------------
        def eventFilter(self, obj: Any, event: Any) -> bool:
            try:
                t = event.type()
                if t == QEvent.Type.WinIdChange:
                    self._bind()
                elif t == QEvent.Type.WindowStateChange:
                    # Normally a no-op: AppKit's will-exit came first. Only
                    # the exit is mirrored here; entering waits for AppKit's
                    # did-enter (the bar window exists only in full screen).
                    if not self._win.isFullScreen():
                        self._exit()
            except Exception:
                pass
            return False

    return _FullscreenReveal


_controller: Any = None


def install(win: Any = None, bridge: Any = "auto") -> Any:
    """Attach the full screen reveal to the main window. Returns the
    controller, or None when disabled by config or when there is no AppKit
    (every other platform keeps its own full screen behaviour)."""
    global _controller
    if not _config().get("fullscreen_bar_inset", True):
        return None
    if win is None:
        from aqt import mw
        win = mw
    if _controller is not None and getattr(_controller, "_win", None) is win:
        return _controller
    if bridge == "auto":
        bridge = _make_bridge()
    if bridge is None:
        return None
    try:
        _controller = _make_controller_class()(win, bridge)
    except Exception:
        _controller = None
    return _controller
