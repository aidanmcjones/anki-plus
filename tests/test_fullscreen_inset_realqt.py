"""fullscreen_inset.py on a REAL QMainWindow (PyQt6, offscreen).

The unit test drives a fake Qt. This one runs the real module on a real
QMainWindow under QT_QPA_PLATFORM=offscreen:

  * with no AppKit (offscreen), install() returns None and leaves the window
    alone: no event filter, no timer, no margin, same palette;
  * with a recording stand-in for the AppKit bridge, real full screen
    enter/exit through setWindowState(): Qt's own WindowStateChange never
    starts following the bar (AppKit's did-enter does), the exit mirrors
    it, no toolbar is ever attached, and the window's contents margins,
    palette and child timers are untouched throughout.
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, ROOT)
from _realqt import ensure_real_aqt, spin  # noqa: E402

ensure_real_aqt(__file__)

from aqt.qt import QApplication, QMainWindow, QTimer, QWidget, Qt  # noqa: E402

import fullscreen_inset as fi  # noqa: E402


class RecordingBridge:
    def __init__(self):
        self.calls = []
        self.handler = None

    def nswindow(self, win):
        return int(win.winId()) or 1

    def is_fullscreen(self, nswin):
        return False

    def observe(self, nswin, handler):
        self.calls.append("observe")
        self.handler = handler

    def unobserve(self, nswin):
        self.calls.append("unobserve")

    def observe_bars(self, nswin, handler):
        self.calls.append("follow" if handler else "unfollow")

    def find_bar(self, nswin):
        return 0

    def set_content_offset(self, nswin, offset):
        self.calls.append(("offset", offset))


def _snapshot(win):
    m = win.contentsMargins()
    return ((m.left(), m.top(), m.right(), m.bottom()),
            win.palette().color(win.backgroundRole()).name(),
            len(win.findChildren(QTimer)))


def main():
    app = QApplication.instance() or QApplication(sys.argv)
    assert QApplication.platformName() == "offscreen", QApplication.platformName()

    # 1. Offscreen, automatic bridge: nothing installed.
    win = QMainWindow()
    win.setCentralWidget(QWidget())
    win.resize(900, 600)
    win.show()
    spin(50)
    before = _snapshot(win)
    fi._controller = None
    assert fi.install(win) is None, "installed without AppKit"
    win.setWindowState(Qt.WindowState.WindowFullScreen)
    spin(100)
    assert win.isFullScreen()
    assert _snapshot(win)[0] == (0, 0, 0, 0)
    win.setWindowState(Qt.WindowState.WindowNoState)
    spin(100)
    assert _snapshot(win) == before, (_snapshot(win), before)
    print("ok  offscreen install is inert")

    # 2. Recording bridge: real state changes through the real controller.
    win2 = QMainWindow()
    win2.setCentralWidget(QWidget())
    win2.resize(900, 600)
    win2.show()
    spin(50)
    before = _snapshot(win2)
    br = RecordingBridge()
    fi._controller = None
    ctl = fi.install(win2, bridge=br)
    assert ctl is not None and br.calls == ["observe"], br.calls

    win2.setWindowState(Qt.WindowState.WindowFullScreen)
    spin(100)
    assert win2.isFullScreen()
    assert "follow" not in br.calls, f"Qt state change started following: {br.calls}"
    br.handler(fi.NOTE_DID_ENTER)  # what AppKit posts after the animation
    assert br.calls.count("follow") == 1 and ctl.following, br.calls
    for _ in range(10):
        win2.resize(win2.width(), win2.height() - 1)  # AppKit reveal frames
        spin(5)
    assert br.calls.count("follow") == 1, br.calls
    assert _snapshot(win2)[0] == (0, 0, 0, 0), "contents margin set"
    assert _snapshot(win2)[2] == 0, "a QTimer child exists"

    br.handler(fi.NOTE_WILL_EXIT)
    win2.setWindowState(Qt.WindowState.WindowNoState)
    spin(100)
    br.handler(fi.NOTE_DID_EXIT)
    assert br.calls.count("unfollow") == 1 and not ctl.following, br.calls
    assert _snapshot(win2) == before, (_snapshot(win2), before)
    assert win2.unifiedTitleAndToolBarOnMac() is False
    print("ok  real QMainWindow: enter/exit only starts/stops following the bar")

    # 3. Exit via Qt only (no will-exit seen): the fallback still stops.
    br.handler(fi.NOTE_DID_ENTER)
    win2.setWindowState(Qt.WindowState.WindowFullScreen)
    spin(100)
    win2.setWindowState(Qt.WindowState.WindowNoState)
    spin(100)
    assert br.calls.count("unfollow") == 2 and not ctl.following, br.calls
    print("ok  Qt exit fallback")

    print("PASS test_fullscreen_inset_realqt")


if __name__ == "__main__":
    main()
