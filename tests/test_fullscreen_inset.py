"""Regression tests for fullscreen_inset.py: in macOS full screen, the
window's title bar ("Anki+") slides down over the content whenever the
cursor reaches the top edge. Stock Qt leaves the content where it is, so the
bar covers the sidebar wordmark and whatever sits at the top of the page.
The add-on must push the content down by the bar's height while it is
revealed, and pull it back up once the bar retracts.

Runs standalone (no Anki or PyQt install needed):
`python3 tests/test_fullscreen_inset.py`. `aqt` is stubbed with a small
fake Qt: a main window with geometry, a screen, a cursor, and a timer that
fires on demand.
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
    """Manual timer: the test fires it by hand."""
    instances = []

    def __init__(self, parent=None):
        self._interval = 0
        self._cb = None
        self.active = False
        self.timeout = self
        QTimer.instances.append(self)

    def setInterval(self, ms):
        self._interval = ms

    def setTimerType(self, *a):
        pass

    def connect(self, fn):
        self._cb = fn

    def start(self):
        self.active = True

    def stop(self):
        self.active = False

    def isActive(self):
        return self.active

    def fire(self):
        assert self.active, "timer fired while stopped"
        self._cb()


class QCursor:
    _pos = _Point(500, 500)

    @classmethod
    def pos(cls):
        return cls._pos


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


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _boot(screen_w=1728, screen_h=1117, menubar_h=24, titlebar_h=28):
    QTimer.instances.clear()
    QCursor._pos = _Point(500, 500)
    screen = _Screen(screen_w, screen_h, menubar_h)
    win = FakeMainWindow(screen, titlebar_h=titlebar_h)
    ctl = fullscreen_inset.install(win)
    assert ctl is not None, "install() returned nothing"
    # A normal windowed session first, so the title bar height is known.
    win.event(QEvent(QEvent.Type.Show))
    win.event(QEvent(QEvent.Type.Resize))
    return win, ctl


def _poll(win, x, y):
    QCursor._pos = _Point(x, y)
    timers = [t for t in QTimer.instances if t.active]
    assert timers, "no active poll timer while fullscreen"
    for t in timers:
        t.fire()
    return win.margins[1]


# --------------------------------------------------------------------------- #
# The reported bug: cursor to the top edge in full screen, content stays put
# --------------------------------------------------------------------------- #

def test_fullscreen_reveal_pushes_content_down_by_bar_height():
    win, _ = _boot(menubar_h=24, titlebar_h=28)
    win.enter_fullscreen(content_h=1117)  # no notch: content spans the screen
    assert win.margins[1] == 0
    # Cursor hits the very top edge: macOS slides menu bar + title bar down.
    assert _poll(win, 800, 0) == 24 + 28
    # It stays down while the cursor sits anywhere on the revealed bars...
    assert _poll(win, 800, 40) == 52
    assert _poll(win, 800, 51) == 52
    # ...and retracts once the cursor leaves them.
    assert _poll(win, 800, 52) == 0
    assert _poll(win, 800, 400) == 0


def test_notched_display_only_the_title_bar_covers_content():
    # 16in MacBook Pro: 1117pt tall, 37pt menu-bar strip beside the notch.
    # A fullscreen window sits below the strip, so only the title bar
    # (28pt) drops over the content when the bars reveal.
    win, _ = _boot(screen_h=1117, menubar_h=37, titlebar_h=28)
    win.enter_fullscreen(content_h=1117 - 37)
    assert _poll(win, 800, 0) == 28
    assert _poll(win, 800, 60) == 28   # still over the title bar (37..65)
    assert _poll(win, 800, 65) == 0


def test_approaching_without_touching_the_edge_does_nothing():
    win, _ = _boot()
    win.enter_fullscreen(content_h=1117)
    assert _poll(win, 800, 5) == 0
    assert _poll(win, 800, 1) == 0


def test_cursor_on_another_screen_retracts():
    win, _ = _boot(screen_w=1728)
    win.enter_fullscreen(content_h=1117)
    assert _poll(win, 800, 0) == 52
    assert _poll(win, 2500, 0) == 0   # x outside this screen


def test_leaving_fullscreen_clears_inset_and_stops_polling():
    win, _ = _boot()
    win.enter_fullscreen(content_h=1117)
    assert _poll(win, 800, 0) == 52
    win.leave_fullscreen()
    assert win.margins[1] == 0
    assert not any(t.active for t in QTimer.instances)
    # Windowed: no timer, no margin, whatever the cursor does.
    QCursor._pos = _Point(800, 0)
    assert win.margins[1] == 0


def test_windowed_never_polls():
    win, _ = _boot()
    assert not any(t.active for t in QTimer.instances)
    assert win.margin_calls == []


def test_titlebar_height_is_measured_from_the_windowed_frame():
    # A 22pt title bar (older look / different scale) must be honoured.
    win, _ = _boot(menubar_h=24, titlebar_h=22)
    win.enter_fullscreen(content_h=1117)
    assert _poll(win, 800, 0) == 46


def test_config_off_installs_nothing():
    _AddonManager.cfg = {"fullscreen_bar_inset": False}
    try:
        QTimer.instances.clear()
        screen = _Screen(1728, 1117, 24)
        win = FakeMainWindow(screen)
        ctl = fullscreen_inset.install(win)
        assert ctl is None
        assert win._filters == []
    finally:
        _AddonManager.cfg = {}


def test_reveal_tracker_pure_logic():
    T = fullscreen_inset.RevealTracker
    t = T(titlebar_h=28, menubar_h=24, menubar_over_content=True)
    assert t.step(10) == 0
    assert t.step(0) == 52
    assert t.step(30) == 52
    assert t.step(52) == 0
    t2 = T(titlebar_h=28, menubar_h=37, menubar_over_content=False)
    assert t2.step(0) == 28
    assert t2.step(64) == 28
    assert t2.step(65) == 0
    # A cursor that is nowhere (other screen) always retracts.
    t2.step(0)
    assert t2.step(None) == 0


if __name__ == "__main__":
    names = [n for n in sorted(globals()) if n.startswith("test_")]
    for n in names:
        globals()[n]()
        print("ok", n)
    print("PASS test_fullscreen_inset")
