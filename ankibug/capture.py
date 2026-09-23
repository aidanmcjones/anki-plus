"""Capture bug context from a running Anki+ instance over raw CDP.

Anki+ (anki-design) exposes a Chromium remote-debugging port
(default 127.0.0.1:8080). `GET /json` lists the open pages ("main
webview", "top toolbar", "bottom toolbar", and "editor" when Browse
is open); each has a `webSocketDebuggerUrl` for the CDP websocket.

Playwright's connectOverCDP does NOT work against QtWebEngine, so we
speak raw CDP JSON messages over a websocket ourselves (matching the
reference probe at
~/.claude/jobs/01418429/tmp/cdp_probe.mjs).

Everything here must degrade gracefully: if Anki+ isn't running, or
the expected page/globals aren't there, functions return None /
partial data rather than raising, so a ticket can always be filed.
"""

from __future__ import annotations

import base64
import datetime
import itertools
import json
import subprocess
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

DEFAULT_CDP_HOST = "127.0.0.1"
DEFAULT_CDP_PORT = 8080
ANKI_VERSION_FILE = "~/dev/anki/.version"
ADDON_REPO = "~/dev/anki-design"
CONSOLE_COLLECT_SECONDS = 1.0


# ---------------------------------------------------------------------------
# Environment info (app.*)
# ---------------------------------------------------------------------------

def get_anki_version(version_file: str = ANKI_VERSION_FILE) -> Optional[str]:
    try:
        with open(version_file, "r", encoding="utf-8") as f:
            return f.read().strip() or None
    except OSError:
        return None


def _git(repo: str, *args: str) -> Optional[str]:
    try:
        out = subprocess.run(
            ["git", "-C", repo, *args],
            capture_output=True, text=True, timeout=5, check=False,
        )
        if out.returncode != 0:
            return None
        return out.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None


def get_addon_git_info(repo: str = ADDON_REPO) -> Dict[str, Any]:
    branch = _git(repo, "rev-parse", "--abbrev-ref", "HEAD")
    commit = _git(repo, "rev-parse", "HEAD")
    dirty = None
    status = _git(repo, "status", "--porcelain")
    if status is not None:
        dirty = len(status) > 0
    return {"branch": branch, "commit": commit, "dirty": dirty}


def get_app_info(
    addon_repo: str = ADDON_REPO,
    version_file: str = ANKI_VERSION_FILE,
) -> Dict[str, Any]:
    info = {
        "anki_version": get_anki_version(version_file),
        "addon_repo": addon_repo,
    }
    info.update(get_addon_git_info(addon_repo))
    return info


# ---------------------------------------------------------------------------
# CDP transport
# ---------------------------------------------------------------------------

def fetch_pages(
    host: str = DEFAULT_CDP_HOST,
    port: int = DEFAULT_CDP_PORT,
    timeout: float = 2.0,
) -> Optional[List[Dict[str, Any]]]:
    """GET http://host:port/json. Returns None if unreachable."""
    url = "http://{}:{}/json".format(host, port)
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return data if isinstance(data, list) else None
    except (urllib.error.URLError, OSError, ValueError, TimeoutError):
        return None


class CDPError(RuntimeError):
    pass


class CDPSession:
    """Minimal synchronous CDP client over one page's websocket."""

    def __init__(self, ws_url: str, open_timeout: float = 5.0):
        from websockets.sync.client import connect  # local import: optional dep
        self.ws = connect(ws_url, open_timeout=open_timeout, max_size=None)
        self._id_counter = itertools.count(1)
        self.events: List[Dict[str, Any]] = []

    def send(self, method: str, params: Optional[Dict[str, Any]] = None,
              timeout: float = 10.0) -> Dict[str, Any]:
        msg_id = next(self._id_counter)
        self.ws.send(json.dumps({"id": msg_id, "method": method, "params": params or {}}))
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise CDPError("no response to {} within {}s".format(method, timeout))
            raw = self.ws.recv(timeout=remaining)
            data = json.loads(raw)
            if data.get("id") == msg_id:
                return data
            self.events.append(data)

    def evaluate(self, expression: str, timeout: float = 10.0) -> Any:
        result = self.send(
            "Runtime.evaluate",
            {"expression": expression, "awaitPromise": True, "returnByValue": True},
            timeout=timeout,
        )
        res = result.get("result", {})
        if res.get("exceptionDetails"):
            exc = res["exceptionDetails"].get("exception", {})
            return {"__exception__": exc.get("description") or res["exceptionDetails"].get("text")}
        return res.get("result", {}).get("value")

    def collect_events(self, duration: float) -> None:
        deadline = time.monotonic() + duration
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            try:
                raw = self.ws.recv(timeout=remaining)
            except TimeoutError:
                return
            except Exception:  # noqa: BLE001 - recv() raises various closed/timeout errors
                return
            try:
                self.events.append(json.loads(raw))
            except ValueError:
                continue

    def close(self) -> None:
        try:
            self.ws.close()
        except Exception:  # noqa: BLE001
            pass


def _extract_console_errors(events: List[Dict[str, Any]]) -> List[str]:
    errors: List[str] = []
    for ev in events:
        method = ev.get("method")
        params = ev.get("params", {})
        if method == "Runtime.exceptionThrown":
            details = params.get("exceptionDetails", {})
            exc = details.get("exception", {})
            errors.append(exc.get("description") or details.get("text") or "exception")
        elif method == "Runtime.consoleAPICalled" and params.get("type") in ("error", "assert"):
            args = params.get("args", [])
            text = " ".join(
                str(a.get("value", a.get("description", ""))) for a in args
            ).strip()
            errors.append(text or "console.error")
        elif method == "Log.entryAdded":
            entry = params.get("entry", {})
            if entry.get("level") == "error":
                errors.append(entry.get("text", "log error"))
    return errors


def find_page(pages: List[Dict[str, Any]], title: str) -> Optional[Dict[str, Any]]:
    for p in pages:
        if p.get("title") == title:
            return p
    return None


# ---------------------------------------------------------------------------
# High-level capture
# ---------------------------------------------------------------------------

def _normalize_reviewer_state(raw: Any) -> Optional[str]:
    if isinstance(raw, str) and raw in ("question", "answer"):
        return raw
    if isinstance(raw, dict):
        s = raw.get("state")
        if s in ("question", "answer"):
            return s
    return None


def capture(
    host: str = DEFAULT_CDP_HOST,
    port: int = DEFAULT_CDP_PORT,
    page_title: str = "main webview",
    console_seconds: float = CONSOLE_COLLECT_SECONDS,
    log: Optional[List[str]] = None,
) -> Tuple[Optional[Dict[str, Any]], Optional[bytes], Optional[str], Optional[str]]:
    """Attempt a full capture from a running Anki+.

    Returns (capture_dict_or_None, screenshot_png_bytes_or_None, qa_html_or_None,
    reviewer_state_or_None). `log` if given is a list that human-readable
    progress/error lines get appended to.
    """
    def _log(msg: str) -> None:
        if log is not None:
            log.append(msg)

    pages = fetch_pages(host, port)
    if pages is None:
        _log("capture: could not reach CDP endpoint at http://{}:{}/json "
             "(Anki+ likely not running)".format(host, port))
        return None, None, None, None

    page_summaries = [{"title": p.get("title"), "url": p.get("url")} for p in pages]
    _log("capture: found {} page(s): {}".format(
        len(pages), ", ".join(p.get("title", "?") for p in pages)))

    target = find_page(pages, page_title)
    if target is None:
        _log("capture: no page titled {!r}; falling back to first page".format(page_title))
        if not pages:
            return None, None, None, None
        target = pages[0]

    ws_url = target.get("webSocketDebuggerUrl")
    if not ws_url:
        _log("capture: target page has no webSocketDebuggerUrl")
        return {
            "pages": page_summaries,
            "qa_html_path": None,
            "visible_text": None,
            "console_errors": [],
            "screenshot_path": None,
            "captured_at": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
        }, None, None, None

    session = None
    try:
        session = CDPSession(ws_url)
        session.send("Runtime.enable")
        session.send("Log.enable")
        try:
            session.send("Page.enable")
        except CDPError:
            pass

        session.collect_events(console_seconds)

        qa_html = session.evaluate("document.getElementById('qa')?.outerHTML || null")
        if isinstance(qa_html, dict) and "__exception__" in qa_html:
            _log("capture: qa outerHTML eval failed: {}".format(qa_html["__exception__"]))
            qa_html = None

        visible_text = session.evaluate(
            "document.body ? document.body.innerText : null"
        )
        if isinstance(visible_text, dict) and "__exception__" in visible_text:
            _log("capture: visible text eval failed: {}".format(visible_text["__exception__"]))
            visible_text = None

        reviewer_state_raw = session.evaluate("window.__baReviewerState || null")
        if isinstance(reviewer_state_raw, dict) and "__exception__" in reviewer_state_raw:
            reviewer_state_raw = None
        reviewer_state = _normalize_reviewer_state(reviewer_state_raw)

        ba_errors = session.evaluate("window.__baErrors || null")
        if isinstance(ba_errors, dict) and "__exception__" in ba_errors:
            ba_errors = None

        console_errors = _extract_console_errors(session.events)
        if isinstance(ba_errors, list):
            console_errors.extend(str(e) for e in ba_errors)

        screenshot_bytes: Optional[bytes] = None
        try:
            shot = session.send("Page.captureScreenshot", {"format": "png"}, timeout=10.0)
            b64 = shot.get("result", {}).get("data")
            if b64:
                screenshot_bytes = base64.b64decode(b64)
        except CDPError as exc:
            _log("capture: screenshot failed: {}".format(exc))

        capture_dict = {
            "pages": page_summaries,
            "qa_html_path": "qa.html" if qa_html else None,
            "visible_text": visible_text,
            "console_errors": console_errors,
            "screenshot_path": "screenshot.png" if screenshot_bytes else None,
            "captured_at": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
        }
        return capture_dict, screenshot_bytes, qa_html, reviewer_state
    except Exception as exc:  # noqa: BLE001 - capture must never raise into the caller
        _log("capture: failed: {}: {}".format(type(exc).__name__, exc))
        return {
            "pages": page_summaries,
            "qa_html_path": None,
            "visible_text": None,
            "console_errors": [],
            "screenshot_path": None,
            "captured_at": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
        }, None, None, None
    finally:
        if session is not None:
            session.close()
