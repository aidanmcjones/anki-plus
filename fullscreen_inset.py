"""Anki Design: keep the content clear of the macOS fullscreen title bar.

In native macOS full screen the window's title bar (titled "Anki+") is
hidden until the cursor reaches the top edge of the screen. Then it slides
down as an overlay, with macOS's own animation, and retracts once the
cursor leaves it. Qt never hears about that reveal, and it must not try to:
an earlier version of this module polled the cursor and moved the content
down while the bar was out, and that relayout under the cursor made macOS
retract the bar, which removed the inset, which revealed the bar again. The
result was a bar that flickered about twice a second and a page that jumped
with it.

So this module never reacts to the reveal. It reserves a constant strip at
the top of the window for the whole time the window is in full screen:

  * On `WindowStateChange` into full screen, `mw.setContentsMargins(0, h,
    0, 0)` is applied once. Qt's QMainWindow layout lays the central widget
    out inside the contents rect, so the sidebar, the page and every inline
    embed sit below the strip. The strip is painted in the page's paper
    colour, so it reads as part of the page when the bar is hidden.
  * On `WindowStateChange` out of full screen the margin and palette are
    restored, once.
  * Nothing else: no timer, no cursor sampling, no layout change while the
    window stays in the same state. The title bar reveal then behaves like
    every other macOS app: it slides over the reserved strip and away again.

The strip height is the height the revealed bars cover:

  * On a notched MacBook the fullscreen window sits *below* the menu bar
    strip beside the notch; only the title bar (about 28 pt) drops over the
    content.
  * On a display without a notch the window spans the whole screen, so menu
    bar plus title bar (about 24 + 28 pt) come down over it.

Both heights are measured while the window is in its normal state (title
bar from the windowed frame-vs-client difference, menu bar from the
screen's available geometry) and fixed at the moment full screen is
entered.

Off with the `fullscreen_bar_inset` config key. Pure logic first so
`tests/test_fullscreen_inset.py` can drive it with a fake Qt.
"""

from __future__ import annotations

from typing import Any

DEFAULT_TITLEBAR_H = 28
DEFAULT_MENUBAR_H = 24

# Sanity bounds for measured bar heights (pt). Anything outside is a
# measurement taken at the wrong moment (mid-transition frame, hidden
# window), so the previous good value is kept instead.
_MAX_BAR_H = 120


def fullscreen_inset(titlebar_h: int, menubar_h: int, menubar_over_content: bool) -> int:
    """Constant top inset (pt) reserved while the window is full screen."""
    h = max(0, int(titlebar_h))
    if menubar_over_content:
        h += max(0, int(menubar_h))
    return h


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
    """The page paper as a QColor, so the reserved strip paints in the
    theme's own colour."""
    try:
        from aqt.qt import QColor
        from . import addcard as _addcard
        palette, _ = _addcard._resolve_palette()
        return QColor(palette["paper"])
    except Exception:
        return None


def _make_controller_class() -> Any:
    from aqt.qt import QEvent, QObject, QPalette

    class _FullscreenInset(QObject):
        def __init__(self, win: Any) -> None:
            super().__init__(win)
            self._win = win
            self._titlebar_h = DEFAULT_TITLEBAR_H
            self._menubar_h = DEFAULT_MENUBAR_H
            self._inset = 0
            self._saved_palette: Any = None
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
                    # Measurement only; never touches the layout.
                    if not self._win.isFullScreen():
                        self._measure_windowed()
            except Exception:
                pass
            return False

        def _sync(self) -> None:
            """Runs only on a window state change: apply the constant inset
            on entering full screen, remove it on leaving. Re-entrant calls
            in the same state are no-ops."""
            fs = False
            try:
                fs = bool(self._win.isFullScreen())
            except Exception:
                pass
            if fs:
                if self._inset == 0:
                    self._apply(fullscreen_inset(
                        self._titlebar_h,
                        self._menubar_h,
                        self._menubar_over_content(),
                    ))
            else:
                self._apply(0)

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
