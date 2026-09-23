"""Anki Design: keep the content clear of the macOS fullscreen title bar.

In native macOS full screen the window's title bar (titled "Anki+") is
hidden until the cursor reaches the top edge of the screen. Then the menu
bar and the title bar slide down together, as an overlay, and retract once
the cursor leaves them. Qt never hears about that reveal: the window's
frame does not change, so the sidebar wordmark and whatever sits at the top
of the page (the Preferences tab strip, the deck list header) end up under
the bar for as long as it is down.

This module mirrors macOS's reveal rule from the cursor position and pushes
the whole main window down by the height the bars cover while they are
revealed:

  * While `mw` is full screen, a short QTimer samples `QCursor.pos()`.
  * `RevealTracker` is the pure state machine: the bars come down when the
    cursor touches the top edge, stay down while it hovers anywhere on
    them, and go back up once it drops below them (or leaves the screen).
  * The inset is applied with `mw.setContentsMargins(0, h, 0, 0)`. Qt's
    QMainWindow layout lays the central widget out inside the contents
    rect, so the sidebar, the page and every inline embed (which size
    themselves to the central widget on its Resize event) all move as one.

How much of the content the bars cover depends on the display:

  * On a display without a notch the fullscreen window spans the whole
    screen, so menu bar plus title bar (about 24 + 28 pt) come down over it.
  * On a notched MacBook the fullscreen window sits *below* the menu bar
    strip beside the notch; the menu bar reveals into that strip, and only
    the title bar (about 28 pt) drops over the content.

Both heights are measured, not assumed: the title bar from the windowed
frame-vs-client difference, the menu bar from the screen's available
geometry, each recorded whenever the window is in its normal state so the
values are ready before full screen is entered.

Off with the `fullscreen_bar_inset` config key. Pure logic first so
`tests/test_fullscreen_inset.py` can drive it with a fake Qt.
"""

from __future__ import annotations

from typing import Any, Optional

DEFAULT_TITLEBAR_H = 28
DEFAULT_MENUBAR_H = 24
POLL_MS = 40

# Sanity bounds for measured bar heights (pt). Anything outside is a
# measurement taken at the wrong moment (mid-transition frame, hidden
# window), so the previous good value is kept instead.
_MAX_BAR_H = 120


class RevealTracker:
    """macOS fullscreen auto-reveal, as a function of the cursor's y offset
    from the top of the window's screen.

    `step(y)` returns the number of pixels the revealed bars currently cover
    at the top of the content (0 while hidden). `y` is None when the cursor
    is not on this screen at all."""

    def __init__(
        self,
        titlebar_h: int,
        menubar_h: int,
        menubar_over_content: bool,
    ) -> None:
        self.titlebar_h = max(0, int(titlebar_h))
        self.menubar_h = max(0, int(menubar_h))
        self.menubar_over_content = bool(menubar_over_content)
        self.revealed = False

    @property
    def reveal_h(self) -> int:
        """Total height of the sliding bars (the hover region)."""
        return self.titlebar_h + self.menubar_h

    @property
    def covered_h(self) -> int:
        """How much of the *content* those bars overlap when revealed."""
        if self.menubar_over_content:
            return self.titlebar_h + self.menubar_h
        return self.titlebar_h

    def step(self, y: Optional[int]) -> int:
        if y is None:
            self.revealed = False
        elif not self.revealed:
            # macOS only reveals when the cursor actually touches the edge;
            # merely approaching it does nothing.
            if y <= 0:
                self.revealed = True
        elif y >= self.reveal_h:
            self.revealed = False
        return self.covered_h if self.revealed else 0


# --------------------------------------------------------------------------- #
# Qt glue
# --------------------------------------------------------------------------- #

def _config() -> dict:
    try:
        from aqt import mw
        return mw.addonManager.getConfig(__name__.split(".")[0]) or {}
    except Exception:
        return {}


def _paper_color() -> Any:
    """The page paper as a QColor, so the strip the content vacates paints
    in the theme's own colour during the bar's slide (it is fully under the
    bar once the slide finishes)."""
    try:
        from aqt.qt import QColor
        from . import addcard as _addcard
        palette, _ = _addcard._resolve_palette()
        return QColor(palette["paper"])
    except Exception:
        return None


def _make_controller_class() -> Any:
    from aqt.qt import QCursor, QEvent, QObject, QPalette, QTimer

    class _FullscreenInset(QObject):
        def __init__(self, win: Any) -> None:
            super().__init__(win)
            self._win = win
            self._titlebar_h = DEFAULT_TITLEBAR_H
            self._menubar_h = DEFAULT_MENUBAR_H
            self._tracker: Optional[RevealTracker] = None
            self._inset = 0
            self._saved_palette: Any = None
            self._timer = QTimer(self)
            self._timer.setInterval(POLL_MS)
            self._timer.timeout.connect(self._poll)
            win.installEventFilter(self)
            self._measure_windowed()

        # -- measurements ------------------------------------------------
        def _measure_windowed(self) -> None:
            """Record the title bar and menu bar heights while the window is
            in its normal state; both are gone (or lying) in full screen."""
            win = self._win
            try:
                if win.isFullScreen():
                    return
                th = win.frameGeometry().height() - win.geometry().height()
                if 0 < th < _MAX_BAR_H:
                    self._titlebar_h = th
            except Exception:
                pass
            try:
                scr = win.screen()
                mh = scr.availableGeometry().top() - scr.geometry().top()
                if 0 < mh < _MAX_BAR_H:
                    self._menubar_h = mh
            except Exception:
                pass

        def _menubar_over_content(self) -> bool:
            """True when the fullscreen window spans the whole screen (no
            notch strip), so the revealed menu bar overlaps content too."""
            try:
                return self._win.height() >= self._win.screen().geometry().height()
            except Exception:
                return True

        # -- state -------------------------------------------------------
        def eventFilter(self, obj: Any, event: Any) -> bool:
            try:
                t = event.type()
                if t == QEvent.Type.WindowStateChange:
                    self._sync()
                elif t in (QEvent.Type.Resize, QEvent.Type.Move, QEvent.Type.Show):
                    if not self._win.isFullScreen():
                        self._measure_windowed()
            except Exception:
                pass
            return False

        def _sync(self) -> None:
            fs = False
            try:
                fs = bool(self._win.isFullScreen())
            except Exception:
                pass
            if fs:
                if self._tracker is None:
                    self._tracker = RevealTracker(
                        self._titlebar_h,
                        self._menubar_h,
                        self._menubar_over_content(),
                    )
                if not self._timer.isActive():
                    self._timer.start()
            else:
                self._timer.stop()
                self._tracker = None
                self._apply(0)

        def _poll(self) -> None:
            tr = self._tracker
            if tr is None:
                return
            try:
                pos = QCursor.pos()
                geo = self._win.screen().geometry()
                y: Optional[int] = pos.y() - geo.top() if geo.contains(pos) else None
                tr.menubar_over_content = self._menubar_over_content()
                self._apply(tr.step(y))
            except Exception:
                pass

        def _apply(self, inset: int) -> None:
            inset = max(0, int(inset))
            if inset == self._inset:
                return
            self._inset = inset
            win = self._win
            try:
                win.setContentsMargins(0, inset, 0, 0)
            except Exception:
                pass
            # Paint the vacated strip in the page's paper for the ~200ms the
            # bar takes to slide; restore Anki's palette once it is gone.
            try:
                if inset and self._saved_palette is None:
                    paper = _paper_color()
                    if paper is not None:
                        self._saved_palette = QPalette(win.palette())
                        pal = QPalette(win.palette())
                        pal.setColor(QPalette.ColorRole.Window, paper)
                        win.setPalette(pal)
                elif not inset and self._saved_palette is not None:
                    win.setPalette(self._saved_palette)
                    self._saved_palette = None
            except Exception:
                pass

    return _FullscreenInset


_controller: Any = None


def install(win: Any = None) -> Any:
    """Attach the fullscreen inset to the main window. Returns the
    controller, or None when disabled by config or already installed."""
    global _controller
    if not _config().get("fullscreen_bar_inset", True):
        return None
    if win is None:
        from aqt import mw
        win = mw
    if _controller is not None and getattr(_controller, "_win", None) is win:
        return _controller
    try:
        _controller = _make_controller_class()(win)
    except Exception:
        _controller = None
    return _controller
