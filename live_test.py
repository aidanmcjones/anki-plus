"""In-app live test runner (dev only, inert unless ANKI_DESIGN_LIVE_TEST is set).

tests/live/run_in_app.py launches the real Anki app against a throwaway base
folder with ANKI_DESIGN_LIVE_TEST=<script> and ANKI_DESIGN_LIVE_RESULT=<json>.
When the profile opens, this module imports the script, calls its
`run(t)` with a LiveTest context (the real `mw`, QTest, webview helpers),
writes every check to the result JSON, and closes the app.

Nothing here runs in a normal session: install() returns immediately when
the env var is absent.
"""

from __future__ import annotations

import importlib.util
import json
import os
import time
import traceback
from typing import Any, Callable, List, Optional

ENV_SCRIPT = "ANKI_DESIGN_LIVE_TEST"
ENV_RESULT = "ANKI_DESIGN_LIVE_RESULT"

_started = {"v": False}


class LiveTestError(Exception):
    pass


class LiveTest:
    """What a live test script gets as `t`."""

    def __init__(self, mw: Any) -> None:
        from aqt.qt import QApplication

        self.mw = mw
        self.app = QApplication.instance()
        try:
            from PyQt6.QtTest import QTest  # type: ignore
        except Exception:  # pragma: no cover
            QTest = None  # type: ignore
        self.QTest = QTest
        self.checks: List[dict] = []
        self.notes: List[str] = []

    # -- results ---------------------------------------------------------
    def check(self, name: str, ok: Any, detail: Any = "") -> bool:
        """Record one PASS/FAIL line. Returns the boolean so tests can
        branch on it; never raises."""
        ok = bool(ok)
        self.checks.append({"name": name, "ok": ok, "detail": _short(detail)})
        return ok

    def note(self, msg: Any) -> None:
        self.notes.append(_short(msg, 2000))

    # -- event loop ------------------------------------------------------
    def pump(self, ms: int = 50) -> None:
        """Let the real event loop run for `ms` milliseconds."""
        from aqt.qt import QEventLoop, QTimer

        loop = QEventLoop()
        QTimer.singleShot(max(0, int(ms)), loop.quit)
        loop.exec()

    def wait_until(
        self, pred: Callable[[], Any], timeout: float = 10.0, step_ms: int = 100
    ) -> Any:
        """Pump events until pred() is truthy; returns its last value
        (falsy on timeout)."""
        end = time.monotonic() + timeout
        val = None
        while True:
            try:
                val = pred()
            except Exception as e:  # keep waiting through transient errors
                val = None
                self.note(f"wait_until predicate raised {e!r}")
            if val or time.monotonic() >= end:
                return val
            self.pump(step_ms)

    # -- webviews --------------------------------------------------------
    def js(self, code: str, web: Any = None, timeout: float = 10.0) -> Any:
        """Evaluate `code` (an expression, or a statement block wrapped in
        an IIFE by the caller) in a real webview and return its JSON-able
        result. Default webview: mw.web (deck list / overview / reviewer)."""
        web = web or self.mw.web
        box: dict = {}
        web.evalWithCallback(code, lambda r: box.setdefault("r", r))
        self.wait_until(lambda: "r" in box, timeout=timeout, step_ms=20)
        if "r" not in box:
            raise LiveTestError(f"js timed out after {timeout}s: {code[:120]}")
        return box["r"]

    def wait_js(
        self, code: str, timeout: float = 15.0, web: Any = None
    ) -> Any:
        """Poll a JS expression until it is truthy."""
        return self.wait_until(
            lambda: self.js(code, web=web, timeout=5.0), timeout=timeout, step_ms=200
        )

    def element_center(self, css: str, frac_y: float = 0.5, web: Any = None):
        """Widget coordinates of a point inside the first element matching
        `css` (frac_y 0 = top edge, 1 = bottom), or None."""
        web = web or self.mw.web
        g = self.js(
            "(function(){var e=document.querySelector(%s);if(!e)return null;"
            "var b=e.getBoundingClientRect();return [b.left,b.top,b.width,b.height];})()"
            % json.dumps(css),
            web=web,
        )
        if not g or not g[3]:
            return None
        from aqt.qt import QPoint

        z = web.zoomFactor() or 1.0
        return QPoint(int((g[0] + g[2] / 2) * z), int((g[1] + g[3] * frac_y) * z))

    def native_drag(self, web: Any, p0: Any, p1: Any, steps: int = 16,
                    step_ms: int = 40, timeout: float = 10.0) -> bool:
        """A real mouse drag from p0 to p1 (webview widget coordinates).

        Uses QTest's QWindow overloads, which enter Qt through
        QWindowSystemInterface exactly where OS mouse events do. Once
        Chromium starts an HTML5 drag, Qt runs a nested drag loop; the moves
        and the release are queued on timers so they are delivered inside
        that loop, as a hand keeps moving while the drag is live."""
        from aqt.qt import QPoint, Qt, QTimer

        Q = self.QTest
        L = Qt.MouseButton.LeftButton
        NM = Qt.KeyboardModifier.NoModifier
        top = web.window()
        win = top.windowHandle()
        src = web.focusProxy() or web
        w0 = src.mapTo(top, p0)
        w1 = src.mapTo(top, p1)
        path = [QPoint(w0.x() + (w1.x() - w0.x()) * i // steps,
                       w0.y() + (w1.y() - w0.y()) * i // steps)
                for i in range(1, steps + 1)]
        done: list = []

        def step(i: int = 0) -> None:
            if i < len(path):
                Q.mouseMove(win, path[i])
                QTimer.singleShot(step_ms, lambda: step(i + 1))
            else:
                Q.mouseRelease(win, L, NM, w1)
                done.append(True)

        Q.mouseMove(win, w0)
        Q.mousePress(win, L, NM, w0)
        QTimer.singleShot(60, step)
        return bool(self.wait_until(lambda: done, timeout=timeout))

    def deck_list_names(self) -> List[str]:
        """Deck names in the order the home deck list shows them."""
        return self.js(
            "Array.from(document.querySelectorAll('.ad-list-row'))"
            ".map(r => (r.querySelector('.ad-list-name')||r).textContent.trim())"
        ) or []


def _short(v: Any, n: int = 600) -> str:
    s = v if isinstance(v, str) else repr(v)
    return s if len(s) <= n else s[:n] + "..."


def _load_script(path: str):
    spec = importlib.util.spec_from_file_location("anki_design_live_test", path)
    if spec is None or spec.loader is None:
        raise LiveTestError(f"cannot import {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    if not hasattr(mod, "run"):
        raise LiveTestError(f"{path} has no run(t)")
    return mod


def _write(result_path: str, payload: dict) -> None:
    tmp = result_path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(payload, fh, indent=2)
    os.replace(tmp, result_path)


def _run_now(mw: Any) -> None:
    script = os.environ.get(ENV_SCRIPT, "")
    result_path = os.environ.get(ENV_RESULT, "") or script + ".result.json"
    t0 = time.monotonic()
    t: Optional[LiveTest] = None
    error = None
    try:
        t = LiveTest(mw)
        # The home screen renders asynchronously after profile open: wait
        # for the deck browser state and a painted list before the test.
        t.wait_until(lambda: getattr(mw, "state", "") == "deckBrowser", timeout=30)
        t.wait_js("document.readyState === 'complete'", timeout=30)
        mod = _load_script(script)
        mod.run(t)
    except Exception:
        error = traceback.format_exc()
    payload = {
        "script": script,
        "checks": t.checks if t else [],
        "notes": t.notes if t else [],
        "error": error,
        "elapsed_s": round(time.monotonic() - t0, 2),
    }
    try:
        _write(result_path, payload)
    except Exception:
        traceback.print_exc()
    _quit(mw)


def _quit(mw: Any) -> None:
    from aqt.qt import QTimer

    def hard_exit() -> None:  # the app did not close on its own
        os._exit(0)

    QTimer.singleShot(20000, hard_exit)
    try:
        mw.close()
    except Exception:
        hard_exit()


def _on_profile_open(*_a: Any) -> None:
    if _started["v"]:
        return
    _started["v"] = True
    from aqt import mw
    from aqt.qt import QTimer

    QTimer.singleShot(500, lambda: _run_now(mw))


def install() -> None:
    """Hook the runner in when (and only when) the env var names a script."""
    if not os.environ.get(ENV_SCRIPT):
        return
    from aqt import gui_hooks

    gui_hooks.profile_did_open.append(_on_profile_open)
