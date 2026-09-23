"""Bring the main window back when its web renderer dies.

Ticket 20260923-153047: after about 2.5 hours of reviewing, the renderer
process behind mw.web (deck list, overview and reviewer all live in it)
died of an out-of-memory, and the whole window stayed grey: Anki never
reloads a webview whose renderer is gone, so the only way back was to quit.

QWebEnginePage reports the death through `renderProcessTerminated`. On an
abnormal termination (crash, kill, OOM) this re-shows the current screen,
which loads a fresh page in a new renderer: the reviewer comes back on the
card that was showing (it was never answered), the deck list and overview
simply redraw. The same applies to the top and bottom toolbar webviews.

Re-showing alone is not enough. QtWebEngine does not hand the profile's
user scripts to a renderer that replaced a crashed one, and Anki's JS
bridge (qwebchannel.js plus the `pycmd` definition that reports
"domDone") is such a script. Without it the new page never says domDone,
so AnkiWebView queues every later eval forever and the page stays dead to
Python (and to clicks that call pycmd). So the bridge script is also put on
the page itself, once, before the re-show. A card that kills the renderer every time would otherwise
loop, so at most MAX_RECOVERIES happen within WINDOW_S seconds; past that
it falls back to the deck list once and then stops trying.
"""

from __future__ import annotations

import time
from typing import Any, Callable, List, Optional

MAX_RECOVERIES = 3
WINDOW_S = 120.0
DELAY_MS = 250


class Recovery:
    def __init__(
        self,
        mw: Any,
        schedule: Optional[Callable[[int, Callable[[], None]], None]] = None,
        clock: Callable[[], float] = time.monotonic,
        log: Optional[Callable[[str], None]] = None,
        notify: Optional[Callable[[str], None]] = None,
    ) -> None:
        self.mw = mw
        self._schedule = schedule or _single_shot
        self._clock = clock
        self._log = log or (lambda _m: None)
        self._notify = notify or (lambda _m: None)
        self.recent: List[float] = []
        self.gave_up = False
        self.fell_back = False

    def on_terminated(self, status: Any, exit_code: int, page: Any = None) -> None:
        if _is_normal(status):
            return
        if page is not None:
            try:
                _restore_bridge(page)
            except Exception as e:
                self._log(f"could not restore the JS bridge: {e!r}")
        now = self._clock()
        self.recent = [t for t in self.recent if now - t < WINDOW_S]
        self.recent.append(now)
        state = getattr(self.mw, "state", "")
        self._log(
            f"renderer died (status {_name(status)}, exit {exit_code}) in state "
            f"{state!r}; recovery {len(self.recent)} of {MAX_RECOVERIES} in {WINDOW_S:.0f}s"
        )
        if self.gave_up:
            return
        if len(self.recent) > MAX_RECOVERIES:
            if self.fell_back:
                self.gave_up = True
                self._log("renderer keeps dying; not reloading again")
                return
            self.fell_back = True
            self._schedule(DELAY_MS, lambda: self._reshow("deckBrowser"))
            return
        self._schedule(DELAY_MS, lambda: self._reshow(state))

    def _reshow(self, state: str) -> None:
        mw = self.mw
        try:
            if getattr(mw, "col", None) is None:
                return  # profile closing: nothing to show
            if state in ("deckBrowser", "overview", "review"):
                mw.moveToState(state)
            else:
                mw.moveToState("deckBrowser")
            self._notify("The card view crashed and was reloaded.")
        except Exception as e:  # never raise out of a Qt signal
            self._log(f"renderer recovery failed: {e!r}")


def _restore_bridge(page: Any) -> bool:
    """Put Anki's bridge script on `page` itself (once). True if added."""
    import aqt.webview as _wv

    script = getattr(_wv, "_bridge_script", None)
    if script is None:
        return False
    scripts = page.scripts()
    for existing in scripts.toList():
        if existing.sourceCode() == script.sourceCode():
            return False
    scripts.insert(script)
    return True


def _single_shot(ms: int, fn: Callable[[], None]) -> None:
    from aqt.qt import QTimer

    QTimer.singleShot(ms, fn)


def _is_normal(status: Any) -> bool:
    try:
        from aqt.qt import QWebEnginePage

        return status == QWebEnginePage.RenderProcessTerminationStatus.NormalTerminationStatus
    except Exception:
        return _name(status) == "NormalTerminationStatus"


def _name(status: Any) -> str:
    return getattr(status, "name", None) or str(status)


_installed: dict = {"pages": [], "rec": None}


def install(mw: Any) -> Optional[Recovery]:
    """Watch the main, top-toolbar and bottom-toolbar webviews' renderers.
    Safe to call more than once (each page is connected once)."""

    def log(msg: str) -> None:
        try:
            from . import _dev_cmd_log

            _dev_cmd_log(f"renderer_recovery: {msg}")
        except Exception:
            pass
        try:
            print(f"anki-design renderer_recovery: {msg}")
        except Exception:
            pass

    def notify(msg: str) -> None:
        try:
            from aqt.utils import tooltip

            tooltip(msg, parent=mw)
        except Exception:
            pass

    rec = _installed["rec"] or Recovery(mw, log=log, notify=notify)
    _installed["rec"] = rec
    for attr in ("web", "toolbarWeb", "bottomWeb"):
        web = getattr(mw, attr, None)
        page = web.page() if web is not None else None
        if page is None or any(p is page for p in _installed["pages"]):
            continue
        page.renderProcessTerminated.connect(
            lambda status, code, _p=page: rec.on_terminated(status, code, _p))
        _installed["pages"].append(page)
    return rec
