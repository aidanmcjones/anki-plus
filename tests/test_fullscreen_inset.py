"""Regression tests for fullscreen_inset.py.

Ticket 20260922-232122: in macOS full screen the hidden "Anki+" title bar
slides down over the sidebar wordmark and the top of the page when the
cursor touches the top edge.

Ticket 20260923-102004: the first fix polled the cursor every 40 ms and
moved the content down while the bar was out. That relayout under the
cursor made macOS retract the bar, the poll removed the inset, the bar
revealed again: a flicker loop about twice a second.

The fix reserves a constant strip in full screen instead: applied once on
entering, removed once on leaving, and nothing in between. These tests
assert there is no timer and no cursor read at all, and that the margin
changes exactly once per state change, whatever the cursor does.

Runs standalone (no Anki or PyQt install needed):
`python3 tests/test_fullscreen_inset.py`. `aqt` is stubbed with a small
fake Qt whose QTimer and QCursor record any use.
"""

import os
import sys
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


# --------------------------------------------------------------------------- #
# Fake Qt
# --------------------------------------------------------------------------- #

class _Rect:
    def __init__(self, x, y, w, h):
        self._x, self._y, self._w, self._h = x, y, w, h

    def top(self):
        return self._y

    def left(self):
        return self._x

    def height(self):
        return self._h

    def width(self):
        return self._w

    def contains(self, pt):
        return (self._x <= pt.x() < self._x + self._w
                and self._y <= pt.y() < self._y + self._h)


class _Point:
    def __init__(self, x, y):
        self._x, self._y = x, y

    def x(self):
        return self._x

    def y(self):
        return self._y


class _Screen:
    def __init__(self, w, h, menubar_h):
        self._geo = _Rect(0, 0, w, h)
        self._avail = _Rect(0, menubar_h, w, h - menubar_h)

    def geometry(self):
        return self._geo

    def availableGeometry(self):
        return self._avail


class _WindowState:
    WindowNoState = 0
    WindowFullScreen = 4


class _QtNS:
    WindowState = _WindowState


class _EventType:
    WindowStateChange = 105
    Resize = 14
    Move = 13
    Show = 17
    Paint = 12


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
        self.timeout = self

    def connect(self, fn):
        pass

    def setInterval(self, ms):
        pass

    def start(self, *a):
        QTimer.started += 1

    @staticmethod
    def singleShot(*a, **k):
        QTimer.started += 1


class QCursor:
    """Records every read of the cursor position."""
    reads = 0

    @classmethod
    def pos(cls):
        QCursor.reads += 1
        return _Point(800, 0)


class QColor:
    def __init__(self, s):
        self.s = s


class QPalette:
    class ColorRole:
        Window = 10

    def __init__(self, other=None):
        self.colors = dict(other.colors) if other is not None else {}

    def setColor(self, role, color):
        self.colors[role] = color

    def color(self, role):
        return self.colors.get(role)


class _Widget:
    def __init__(self):
        self._filters = []
        self._palette = QPalette()

    def installEventFilter(self, f):
        self._filters.append(f)

    def removeEventFilter(self, f):
        self._filters.remove(f)

    def palette(self):
        return self._palette

    def setPalette(self, p):
        self._palette = p

    def event(self, ev):
        for f in list(self._filters):
            f.eventFilter(self, ev)


class FakeMainWindow(_Widget):
    """Only the pieces fullscreen_inset reads: frame vs. client geometry,
    the screen, fullscreen state, and the contents margins it sets."""

    def __init__(self, screen, titlebar_h=28):
        super().__init__()
        self._screen = screen
        self._titlebar_h = titlebar_h
        self._fullscreen = False
        self._geo = _Rect(100, 100 + titlebar_h, 1000, 700)
        self.margins = (0, 0, 0, 0)
        self.margin_calls = []

    def screen(self):
        return self._screen

    def geometry(self):
        return self._geo

    def frameGeometry(self):
        if self._fullscreen:
            return self._geo
        return _Rect(self._geo.left(), self._geo.top() - self._titlebar_h,
                     self._geo.width(), self._geo.height() + self._titlebar_h)

    def height(self):
        return self._geo.height()

    def isFullScreen(self):
        return self._fullscreen

    def windowState(self):
        return _WindowState.WindowFullScreen if self._fullscreen else 0

    def setContentsMargins(self, l, t, r, b):
        self.margins = (l, t, r, b)
        self.margin_calls.append((l, t, r, b))

    def contentsMargins(self):
        m = self.margins

        class _M:
            def top(self_inner):
                return m[1]
        return _M()

    # test drivers ------------------------------------------------------
    def enter_fullscreen(self, content_h):
        self._fullscreen = True
        sg = self._screen.geometry()
        self._geo = _Rect(0, sg.height() - content_h, sg.width(), content_h)
        self.event(QEvent(QEvent.Type.Resize))
        self.event(QEvent(QEvent.Type.WindowStateChange))

    def leave_fullscreen(self):
        self._fullscreen = False
        self._geo = _Rect(100, 100 + self._titlebar_h, 1000, 700)
        self.event(QEvent(QEvent.Type.WindowStateChange))
        self.event(QEvent(QEvent.Type.Resize))


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

    def addonFromModule(self, m):
        return "anki-design"


_mw = types.SimpleNamespace(addonManager=_AddonManager())
_stub("aqt", mw=_mw)
_stub(
    "aqt.qt",
    QObject=QObject, QEvent=QEvent, QTimer=QTimer, QCursor=QCursor,
    Qt=_QtNS, QPalette=QPalette, QColor=QColor,
)

import fullscreen_inset  # noqa: E402

_orig_paper = fullscreen_inset._paper_color


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _boot(screen_w=1728, screen_h=1117, menubar_h=24, titlebar_h=28):
    QTimer.created = QTimer.started = 0
    QCursor.reads = 0
    fullscreen_inset._controller = None
    screen = _Screen(screen_w, screen_h, menubar_h)
    win = FakeMainWindow(screen, titlebar_h=titlebar_h)
    ctl = fullscreen_inset.install(win)
    assert ctl is not None, "install() returned nothing"
    # A normal windowed session first, so the title bar height is known.
    win.event(QEvent(QEvent.Type.Show))
    win.event(QEvent(QEvent.Type.Resize))
    return win, ctl


def _assert_no_polling():
    assert QTimer.created == 0, "fullscreen_inset created a QTimer"
    assert QTimer.started == 0, "fullscreen_inset started a timer"
    assert QCursor.reads == 0, "fullscreen_inset read the cursor position"


def _churn(win):
    """Everything the old loop reacted to: the window keeps getting Resize,
    Move, Paint and Show events while the bar reveals and retracts."""
    for t in (QEvent.Type.Resize, QEvent.Type.Move, QEvent.Type.Paint,
              QEvent.Type.Show, QEvent.Type.Resize):
        win.event(QEvent(t))


# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #

def test_module_has_no_timer_or_cursor_poll():
    src = open(os.path.join(ROOT, "fullscreen_inset.py")).read()
    for bad in ("QTimer", "startTimer", "QCursor", "POLL_MS", "RevealTracker"):
        assert bad not in src, f"{bad} is back in fullscreen_inset.py"


def test_notched_display_inset_applied_once_on_enter_removed_once_on_exit():
    # 16in MacBook Pro: 37pt menu-bar strip beside the notch, so only the
    # 28pt title bar can drop over the content.
    win, _ = _boot(screen_h=1117, menubar_h=37, titlebar_h=28)
    assert win.margin_calls == []
    win.enter_fullscreen(content_h=1117 - 37)
    assert win.margin_calls == [(0, 28, 0, 0)]
    _churn(win)
    # A repeated state-change notification in the same state is a no-op.
    win.event(QEvent(QEvent.Type.WindowStateChange))
    assert win.margin_calls == [(0, 28, 0, 0)]
    win.leave_fullscreen()
    assert win.margin_calls == [(0, 28, 0, 0), (0, 0, 0, 0)]
    _churn(win)
    win.event(QEvent(QEvent.Type.WindowStateChange))
    assert win.margin_calls == [(0, 28, 0, 0), (0, 0, 0, 0)]
    _assert_no_polling()


def test_full_screen_without_notch_reserves_menu_and_title_bar():
    win, _ = _boot(menubar_h=24, titlebar_h=28)
    win.enter_fullscreen(content_h=1117)
    assert win.margin_calls == [(0, 52, 0, 0)]
    _churn(win)
    assert win.margin_calls == [(0, 52, 0, 0)]
    _assert_no_polling()


def test_reveal_cannot_move_content():
    # Simulate many reveal/retract cycles: whatever Qt events the reveal
    # produces, the margin never changes while the window stays full screen.
    win, _ = _boot(menubar_h=37)
    win.enter_fullscreen(content_h=1080)
    for _ in range(50):
        _churn(win)
    assert win.margin_calls == [(0, 28, 0, 0)]
    _assert_no_polling()


def test_strip_painted_in_paper_then_palette_restored():
    win, _ = _boot(menubar_h=37)
    orig = dict(win.palette().colors)
    fullscreen_inset._paper_color = lambda: QColor("#123456")
    try:
        win.enter_fullscreen(content_h=1080)
        assert win.palette().color(QPalette.ColorRole.Window).s == "#123456"
        win.leave_fullscreen()
        assert win.palette().colors == orig
    finally:
        fullscreen_inset._paper_color = _orig_paper


def test_windowed_never_touches_margins():
    win, _ = _boot()
    _churn(win)
    assert win.margin_calls == []
    _assert_no_polling()


def test_titlebar_height_is_measured_from_the_windowed_frame():
    win, _ = _boot(menubar_h=37, titlebar_h=22)
    win.enter_fullscreen(content_h=1080)
    assert win.margin_calls == [(0, 22, 0, 0)]


def test_config_off_installs_nothing():
    _AddonManager.cfg = {"fullscreen_bar_inset": False}
    try:
        fullscreen_inset._controller = None
        screen = _Screen(1728, 1117, 24)
        win = FakeMainWindow(screen)
        ctl = fullscreen_inset.install(win)
        assert ctl is None
        assert win._filters == []
    finally:
        _AddonManager.cfg = {}


def test_inset_pure_logic():
    f = fullscreen_inset.fullscreen_inset
    assert f(28, 37, False) == 28
    assert f(28, 24, True) == 52
    assert f(-5, 24, False) == 0


if __name__ == "__main__":
    names = [n for n in sorted(globals()) if n.startswith("test_")]
    for n in names:
        globals()[n]()
        print("ok", n)
    print("PASS test_fullscreen_inset")
