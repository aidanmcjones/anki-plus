"""Regression tests for fullscreen_inset.py (Safari-style full screen reveal).

History:
  * 20260922-232122: in macOS full screen the revealed "Anki+" title bar
    slid over the sidebar wordmark and the top of the page.
  * 20260923-102004: the first fix polled the cursor every 40 ms and set a
    top margin while the bar was out: a flicker loop.
  * 20260923-105203: the second fix reserved a static strip for all of full
    screen, painted in the page colour: a dark band, app pushed down.

  * 539a2af attached an auto-hiding NSToolbar and expected AppKit to push
    the content; on the real display AppKit only moved its own bar window.
  * 20260923-115841: that toolbar made the bar 38 pt instead of 28 and, where
    AutoHideToolbar did not take, parked it open for all of full screen.

The fix: no toolbar, no presentation options. From AppKit's
NSWindowDidEnterFullScreenNotification to its will-exit, the content view
follows AppKit's own bar window (the plain title bar): every move/resize
AppKit makes to it (each step of its reveal/hide animation) places the
content's top on the bar's bottom. These tests drive the controller with a
fake Qt and a fake AppKit bridge and assert:
  * following starts only on AppKit's did-enter and stops on will-exit;
  * the content offset is set only from the bar window's moves, equals the
    bar's reveal at every step, and is back at 0 after will-exit;
  * the module has no toolbar or presentation option code left;
  * nothing polls (no QTimer, no QCursor) and nothing changes layout or
    paint (no setContentsMargins, no setPalette), whatever the cursor does;
  * no bridge (offscreen, other platforms) means install() does nothing.

Runs standalone: `python3 tests/test_fullscreen_inset.py`.
"""

import os
import sys
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


# --------------------------------------------------------------------------- #
# Fake Qt
# --------------------------------------------------------------------------- #

class _EventType:
    WindowStateChange = 105
    WinIdChange = 203
    Resize = 14
    Move = 13
    Show = 17
    Enter = 10
    MouseMove = 5


class QEvent:
    Type = _EventType

    def __init__(self, t):
        self._t = t

    def type(self):
        return self._t


class QObject:
    def __init__(self, parent=None):
        self._parent = parent


class QTimer:
    """Records every construction or start; the module must never use it."""
    created = 0
    started = 0

    def __init__(self, *a, **k):
        QTimer.created += 1

    def start(self, *a):
        QTimer.started += 1

    @staticmethod
    def singleShot(*a, **k):
        QTimer.started += 1


class QCursor:
    reads = 0

    @classmethod
    def pos(cls):
        QCursor.reads += 1
        return None


class _Signal:
    def __init__(self):
        self.slots = []

    def connect(self, fn):
        self.slots.append(fn)

    def emit(self, *a):
        for s in list(self.slots):
            s(*a)


class FakeMainWindow:
    def __init__(self, wid=0x1000):
        self._filters = []
        self._fullscreen = False
        self._wid = wid
        self.destroyed = _Signal()
        self.layout_calls = []

    def installEventFilter(self, f):
        self._filters.append(f)

    def winId(self):
        return self._wid

    def isFullScreen(self):
        return self._fullscreen

    # Anything that would change layout or paint is recorded, so a test can
    # assert that nothing did.
    def setContentsMargins(self, *a):
        self.layout_calls.append(("setContentsMargins", a))

    def setPalette(self, *a):
        self.layout_calls.append(("setPalette", a))

    def move(self, *a):
        self.layout_calls.append(("move", a))

    def resize(self, *a):
        self.layout_calls.append(("resize", a))

    def event(self, ev):
        for f in list(self._filters):
            f.eventFilter(self, ev)


class FakeBridge:
    """Stands in for CocoaBridge: an NSWindow, AppKit's bar window, and a way
    to post AppKit's notifications."""

    def __init__(self, nswin=0xBEEF, fullscreen=False):
        self._nswin = nswin
        self.fullscreen = fullscreen
        self.handlers = {}
        self.calls = []
        self.bar_handler = None
        self.bar_bottom_y = None   # None: the bar window is not shown
        self.top = 1084.0
        self.offsets = []

    # -- the bar window ------------------------------------------------------
    def observe_bars(self, nswin, handler):
        self.calls.append(("observe_bars", handler is not None))
        self.bar_handler = handler

    def find_bar(self, nswin):
        return 0x5A5A if self.bar_bottom_y is not None else 0

    def content_top(self, nswin):
        return self.top

    def bar_bottom(self, barwin):
        return self.bar_bottom_y

    def set_content_offset(self, nswin, offset):
        self.offsets.append(offset)

    def move_bar(self, bottom):
        """AppKit moves its bar window (one animation step)."""
        self.bar_bottom_y = bottom
        if self.bar_handler:
            self.bar_handler(0x5A5A)

    def nswindow(self, win):
        return self._nswin if win.winId() else 0

    def is_fullscreen(self, nswin):
        return self.fullscreen

    def observe(self, nswin, handler):
        self.calls.append(("observe", nswin))
        self.handlers[nswin] = handler

    def unobserve(self, nswin):
        self.calls.append(("unobserve", nswin))
        self.handlers.pop(nswin, None)

    def post(self, name):
        h = self.handlers.get(self._nswin)
        if h:
            h(name)

    def count(self, what):
        return sum(1 for c in self.calls if c[0] == what)


def _stub(name, **attrs):
    mod = sys.modules.get(name) or types.ModuleType(name)
    for k, v in attrs.items():
        setattr(mod, k, v)
    sys.modules[name] = mod
    return mod


class _AddonManager:
    cfg = {}

    def getConfig(self, *a, **k):
        return dict(self.cfg)


_mw = types.SimpleNamespace(addonManager=_AddonManager())
_stub("aqt", mw=_mw)
_stub("aqt.qt", QObject=QObject, QEvent=QEvent, QTimer=QTimer, QCursor=QCursor)

import fullscreen_inset as fi  # noqa: E402


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _boot(**bridge_kw):
    QTimer.created = QTimer.started = 0
    QCursor.reads = 0
    _AddonManager.cfg = {}
    fi._controller = None
    win = FakeMainWindow()
    br = FakeBridge(**bridge_kw)
    ctl = fi.install(win, bridge=br)
    assert ctl is not None, "install() returned nothing"
    return win, br, ctl


def _assert_untouched(win):
    assert QTimer.created == 0, "created a QTimer"
    assert QTimer.started == 0, "started a timer"
    assert QCursor.reads == 0, "read the cursor position"
    assert win.layout_calls == [], f"changed layout/paint: {win.layout_calls}"


def _cursor_churn(win):
    """What happens while the cursor sits at the top edge and the bar comes
    and goes: AppKit moves/resizes the content, Qt sends Move/Resize/Enter/
    MouseMove. None of it may make the module act."""
    for _ in range(20):
        for t in (QEvent.Type.Move, QEvent.Type.Resize, QEvent.Type.Enter,
                  QEvent.Type.MouseMove, QEvent.Type.Show):
            win.event(QEvent(t))


# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #

def test_install_observes_the_nswindow_and_does_nothing_else():
    win, br, ctl = _boot()
    assert br.calls == [("observe", 0xBEEF)], br.calls
    assert not ctl.following and br.bar_handler is None
    _assert_untouched(win)


def test_did_enter_starts_following_will_exit_stops():
    win, br, ctl = _boot()
    win._fullscreen = True
    br.fullscreen = True
    br.post(fi.NOTE_DID_ENTER)
    # Qt's own state change follows AppKit's notification; no second start.
    win.event(QEvent(QEvent.Type.WindowStateChange))
    assert ctl.following and br.count("observe_bars") == 1, br.calls
    _cursor_churn(win)
    br.post(fi.NOTE_WILL_EXIT)
    assert not ctl.following and br.bar_handler is None, br.calls
    br.post(fi.NOTE_DID_EXIT)
    win._fullscreen = False
    br.fullscreen = False
    win.event(QEvent(QEvent.Type.WindowStateChange))
    assert br.count("observe_bars") == 2, "exit handled more than once"
    _assert_untouched(win)


def test_qt_state_change_alone_never_starts():
    """Entering is AppKit's did-enter only (the bar window exists only in
    full screen)."""
    win, br, ctl = _boot()
    win._fullscreen = True
    win.event(QEvent(QEvent.Type.WindowStateChange))
    assert not ctl.following and br.count("observe_bars") == 0, br.calls
    _assert_untouched(win)


def test_qt_state_change_is_the_exit_fallback():
    win, br, ctl = _boot()
    br.post(fi.NOTE_DID_ENTER)
    br.move_bar(1050.0)
    win._fullscreen = False
    win.event(QEvent(QEvent.Type.WindowStateChange))
    assert not ctl.following and br.offsets[-1] == 0.0, br.offsets


def test_repeated_cycles_stay_balanced():
    win, br, ctl = _boot()
    for _ in range(5):
        br.post(fi.NOTE_DID_ENTER)
        _cursor_churn(win)
        br.move_bar(1056.0)
        br.move_bar(1084.0)
        br.post(fi.NOTE_WILL_EXIT)
        br.post(fi.NOTE_DID_EXIT)
    assert br.count("observe_bars") == 10, br.calls
    assert br.bar_handler is None and ctl.offset == 0.0
    _assert_untouched(win)


def test_install_while_already_full_screen_follows():
    win, br, ctl = _boot(fullscreen=True)
    assert ctl.following and br.bar_handler is not None, br.calls


def test_winid_change_rebinds_to_the_new_nswindow():
    win, br, ctl = _boot()
    br._nswin = 0xCAFE
    win.event(QEvent(QEvent.Type.WinIdChange))
    assert ("unobserve", 0xBEEF) in br.calls and ("observe", 0xCAFE) in br.calls


def test_destroyed_unobserves():
    win, br, ctl = _boot()
    br.post(fi.NOTE_DID_ENTER)
    win.destroyed.emit()
    assert not ctl.following and ("unobserve", 0xBEEF) in br.calls


def test_no_bridge_means_no_controller():
    fi._controller = None
    win = FakeMainWindow()
    assert fi.install(win, bridge=None) is None
    # And on this (non-cocoa, fake) platform the automatic bridge is None.
    fi._controller = None
    assert fi.install(win) is None
    assert win._filters == []


def test_config_switch_disables():
    fi._controller = None
    _AddonManager.cfg = {"fullscreen_bar_inset": False}
    try:
        assert fi.install(FakeMainWindow(), bridge=FakeBridge()) is None
    finally:
        _AddonManager.cfg = {}


def test_content_follows_the_bar_window_step_by_step():
    win, br, ctl = _boot()
    br.move_bar(1000.0)  # not full screen: not followed
    assert br.offsets == [], br.offsets
    br.bar_bottom_y = None
    br.post(fi.NOTE_DID_ENTER)
    assert ("observe_bars", True) in br.calls
    assert br.offsets == [], "moved the content with the bar hidden"
    _cursor_churn(win)
    assert br.offsets == [], "Qt events moved the content"
    # AppKit's reveal animation, then the bar sits out, then it hides.
    steps = [1090.0, 1068.0, 1057.0, 1044.0, 1030.0, 1030.0, 1030.0,
             1050.0, 1075.0, 1084.0, 1100.0]
    for b in steps:
        br.move_bar(b)
        assert ctl.offset == fi.reveal_amount(br.top, b), (b, ctl.offset)
    assert br.offsets == [16.0, 27.0, 40.0, 54.0, 34.0, 9.0, 0.0], br.offsets
    br.move_bar(1030.0)
    br.post(fi.NOTE_WILL_EXIT)
    assert br.offsets[-1] == 0.0 and ctl.offset == 0.0, br.offsets
    assert ("observe_bars", False) in br.calls and br.bar_handler is None
    n = len(br.offsets)
    br.move_bar(1040.0)
    assert len(br.offsets) == n, "followed the bar after will-exit"
    _assert_untouched(win)


def test_did_enter_with_the_bar_already_out_places_the_content():
    win, br, ctl = _boot()
    br.bar_bottom_y = 1050.0
    br.post(fi.NOTE_DID_ENTER)
    assert br.offsets == [34.0], br.offsets


def test_reveal_amount_is_clamped():
    f = fi.reveal_amount
    assert f(1084.0, None) == 0.0
    assert f(1084.0, 1100.0) == 0.0     # bar above the window top: hidden
    assert f(1084.0, 1084.2) == 0.0
    assert f(1084.0, 1030.0) == 54.0
    assert f(1084.0, -500.0) == fi.MAX_REVEAL


def test_source_has_no_polling_margin_or_paint():
    src = open(os.path.join(ROOT, "fullscreen_inset.py")).read()
    for bad in ("QTimer", "startTimer", "QCursor", "setContentsMargins",
                "QPalette", "setPalette", "singleShot"):
        assert bad not in src, f"{bad} is back in fullscreen_inset.py"
    # 20260923-115841: no toolbar, no presentation options, no delegate hook.
    for bad in ("setToolbar:", "NSToolbar\"", "setPresentationOptions:",
                "willUseFullScreenPresentationOptions", "setToolbarStyle:"):
        assert bad not in src, f"{bad} is back in fullscreen_inset.py"


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
        print(f"ok  {t.__name__}")
    print(f"PASS test_fullscreen_inset ({len(tests)} tests)")
