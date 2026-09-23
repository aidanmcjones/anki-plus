"""Land an applied fix in the user's running Anki+ by restarting it, safely.

`ankifix apply` patches the live add-on checkout, but the running app only
loads add-on code at startup. After a fully green apply (unit tests and live
tests), land() restarts Anki+ when, and only when, it is safe:

  * the app's Chromium remote-debugging endpoint (cfg.cdp_url) answers;
  * the main webview shows the deck list or the congrats page (not the
    reviewer, an overview, or anything unknown), read with one read-only
    Runtime.evaluate over CDP;
  * no other Anki webview page is open (an editor, add-card, browser,
    deck options, previewer... window would lose its state).

Safe: notify "ankifix: restarting Anki+ to load <id>", quit it politely
through AppleScript (`tell application "Anki+" to quit`; the -128 error it
returns is benign), wait for `pgrep -f anki-dev` to report it gone within
cfg.restart_quit_timeout_s, then `open -na "Anki+"`. It never force-kills:
if the app does not quit in time, nothing else happens and the fix loads on
the user's next restart.

Not safe: notify "ankifix: <id> fix applied, will load on next restart" and
record a pending restart in <tickets_dir>/.pending_restart.json. The
watcher calls tick() every loop; a pending restart is retried every
cfg.restart_retry_interval_s for up to cfg.restart_retry_window_s, then
dropped with a notification.
"""
from __future__ import annotations

import json
import os
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from .config import Config
from .runner import notify
from .tickets import load_ticket, now_iso, save_ticket

PENDING_NAME = ".pending_restart.json"

# CDP page titles that belong to the main window itself (AnkiWebViewKind
# values for the main webview and the two toolbars). Anything else with a
# real URL is another Anki window.
MAIN_WINDOW_TITLES = {"main webview", "top toolbar", "bottom toolbar"}

# Read-only classification of what the main webview shows.
PROBE_JS = r"""
(function () {
  var u = String(location.href).toLowerCase();
  if (/\/congrats([?#\/]|$)/.test(u)) return 'congrats';
  if (document.getElementById('qa')) return 'reviewer';
  if (document.querySelector('[class*="congrats"]')) return 'congrats';
  if (document.querySelector('.ad-list-row, tr.deck, #decktree')) return 'decklist';
  return 'other';
})()
"""

SAFE_STATES = ("decklist", "congrats")


# ------------------------------------------------------------------ probing
def fetch_pages(url: str, timeout: float = 3.0) -> Optional[List[Dict[str, Any]]]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError, TimeoutError):
        return None
    return data if isinstance(data, list) else None


def _url_state(url: str) -> Optional[str]:
    low = (url or "").lower().split("#", 1)[0].split("?", 1)[0].rstrip("/")
    if low.endswith("/congrats"):
        return "congrats"
    return None


def probe_main(page: Dict[str, Any]) -> str:
    """What the main webview shows: decklist / congrats / reviewer / other /
    unknown (probe failed). One read-only Runtime.evaluate over CDP."""
    ws = page.get("webSocketDebuggerUrl")
    if not ws:
        return "unknown"
    try:
        from ankibug.capture import CDPSession
    except Exception:  # noqa: BLE001
        return "unknown"
    session = None
    try:
        session = CDPSession(ws, open_timeout=5.0)
        val = session.evaluate(PROBE_JS, timeout=5.0)
        return val if isinstance(val, str) else "unknown"
    except Exception:  # noqa: BLE001
        return "unknown"
    finally:
        if session is not None:
            session.close()


def check_safe(cfg: Config, probe: Optional[Callable[[Dict[str, Any]], str]] = None) -> Tuple[bool, str]:
    """(safe, reason). Safe only on the deck list or congrats page with no
    other Anki webview window open."""
    pages = fetch_pages(cfg.cdp_url)
    if pages is None:
        return False, f"Anki+ remote debugging not reachable at {cfg.cdp_url}"
    real = [p for p in pages if p.get("type", "page") == "page"
            and (p.get("url") or "") not in ("", "about:blank")]
    main = next((p for p in real if p.get("title") == "main webview"), None)
    if main is None:
        return False, "no main webview page in CDP list"
    others = [p for p in real if p.get("title") not in MAIN_WINDOW_TITLES]
    if others:
        names = ", ".join(sorted({str(p.get("title") or p.get("url")) for p in others}))
        return False, f"other Anki windows open: {names}"
    state = _url_state(main.get("url", "")) or (probe or probe_main)(main)
    if state not in SAFE_STATES:
        return False, f"main window shows {state}"
    return True, f"main window shows {state}"


# ---------------------------------------------------------------- process
def _env(cfg: Config) -> Dict[str, str]:
    env = dict(os.environ)
    env["PATH"] = cfg.subprocess_path()
    return env


def app_pids(cfg: Config) -> List[int]:
    try:
        r = subprocess.run([cfg.pgrep_bin, "-f", cfg.app_process_pattern],
                           capture_output=True, text=True, timeout=10, env=_env(cfg))
    except (OSError, subprocess.TimeoutExpired):
        return []
    if r.returncode != 0:
        return []
    return [int(x) for x in r.stdout.split() if x.strip().isdigit()]


def app_running(cfg: Config) -> bool:
    return bool(app_pids(cfg))


def app_started_at(cfg: Config) -> Optional[float]:
    """Epoch start time of the running app's newest process, or None."""
    starts = []
    for pid in app_pids(cfg):
        try:
            r = subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)],
                               capture_output=True, text=True, timeout=10)
            if r.returncode == 0 and r.stdout.strip():
                starts.append(time.mktime(time.strptime(r.stdout.strip(), "%a %b %d %H:%M:%S %Y")))
        except (OSError, ValueError, subprocess.TimeoutExpired):
            continue
    return max(starts) if starts else None


def restart_app(cfg: Config, echo=print) -> Tuple[bool, str]:
    """Quit Anki+ politely, wait for it to go, reopen it. Never force-kills."""
    env = _env(cfg)
    script = f'tell application "{cfg.app_name}" to quit'
    try:
        q = subprocess.run([cfg.osascript_bin, "-e", script], capture_output=True, text=True,
                           timeout=cfg.osascript_timeout_s, env=env)
        # AppleScript reports -128 ("User canceled") when the app quits
        # under it; that and any other error are judged by pgrep below.
        if q.returncode != 0:
            echo(f"ankifix: osascript quit returned {q.returncode}: {q.stderr.strip()[:200]}")
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, f"osascript quit failed: {type(e).__name__}: {e}"
    deadline = time.monotonic() + cfg.restart_quit_timeout_s
    while app_running(cfg):
        if time.monotonic() >= deadline:
            return False, (f"{cfg.app_name} still running {cfg.restart_quit_timeout_s:g}s after quit; "
                           "not force-killing it")
        time.sleep(0.5)
    try:
        o = subprocess.run([cfg.open_bin, "-na", cfg.app_name], capture_output=True, text=True,
                           timeout=30, env=env)
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, f"open -na failed: {type(e).__name__}: {e}"
    if o.returncode != 0:
        return False, f"open -na {cfg.app_name} exited {o.returncode}: {o.stderr.strip()[:200]}"
    return True, "restarted"


# ---------------------------------------------------------------- pending
def _pending_path(cfg: Config) -> Path:
    return Path(cfg.tickets_dir) / PENDING_NAME


def load_pending(cfg: Config) -> Optional[Dict[str, Any]]:
    p = _pending_path(cfg)
    try:
        data = json.loads(p.read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and data.get("ids") else None


def _save_pending(cfg: Config, data: Optional[Dict[str, Any]]) -> None:
    p = _pending_path(cfg)
    if data is None:
        try:
            p.unlink()
        except FileNotFoundError:
            pass
        return
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    os.replace(tmp, p)


def _record(cfg: Config, ids: List[str], landed: Dict[str, Any], echo=print) -> None:
    for tid in ids:
        try:
            t = load_ticket(cfg.tickets_dir, tid)
            applied = (t.get("fix") or {}).get("applied")
            if isinstance(applied, dict):
                applied["landed"] = landed
                save_ticket(cfg.tickets_dir, t)
        except Exception as e:  # noqa: BLE001
            echo(f"ankifix: could not record landing on {tid}: {type(e).__name__}: {e}")


def _attempt(cfg: Config, ids: List[str], echo=print,
             probe: Optional[Callable[[Dict[str, Any]], str]] = None,
             applied_at: Optional[float] = None) -> str:
    """One try at landing ids. Returns restarted / loaded / not-running /
    pending / failed."""
    label = ", ".join(ids)
    started = app_started_at(cfg) if applied_at is not None else None
    if started is not None and started > applied_at:
        _record(cfg, ids, {"state": "loaded", "at": now_iso(),
                           "reason": f"{cfg.app_name} was restarted after the apply"}, echo)
        echo(f"ankifix: {cfg.app_name} restarted since the apply; {label} already loaded")
        return "loaded"
    if not app_running(cfg):
        _record(cfg, ids, {"state": "not-running", "at": now_iso(),
                           "reason": f"{cfg.app_name} not running; loads on next launch"}, echo)
        echo(f"ankifix: {cfg.app_name} not running; {label} loads on next launch")
        return "not-running"
    safe, reason = check_safe(cfg, probe)
    if not safe:
        echo(f"ankifix: not restarting {cfg.app_name} for {label}: {reason}")
        _record(cfg, ids, {"state": "pending", "at": now_iso(), "reason": reason}, echo)
        return "pending"
    notify(cfg, f"ankifix: restarting {cfg.app_name} to load {label}")
    echo(f"ankifix: restarting {cfg.app_name} to load {label} ({reason})")
    ok, why = restart_app(cfg, echo=echo)
    if ok:
        _record(cfg, ids, {"state": "restarted", "at": now_iso(), "reason": reason}, echo)
        return "restarted"
    echo(f"ankifix: restart for {label} failed: {why}")
    notify(cfg, f"ankifix: {label} applied, will load on next restart")
    _record(cfg, ids, {"state": "failed", "at": now_iso(), "reason": why}, echo)
    return "failed"


def land(cfg: Config, ticket_id: str, echo=print,
         probe: Optional[Callable[[Dict[str, Any]], str]] = None) -> str:
    """After a green apply: restart Anki+ now if safe, else queue a retry."""
    pending = load_pending(cfg)
    ids = list((pending or {}).get("ids") or [])
    if ticket_id not in ids:
        ids.append(ticket_id)
    state = _attempt(cfg, ids, echo=echo, probe=probe)
    if state == "pending":
        now = time.time()
        _save_pending(cfg, {"ids": ids, "first": (pending or {}).get("first", now), "last_try": now})
        notify(cfg, f"ankifix: {ticket_id} fix applied, will load on next restart")
    else:
        _save_pending(cfg, None)
    return state


def tick(cfg: Config, echo=print, now: Optional[float] = None,
         probe: Optional[Callable[[Dict[str, Any]], str]] = None) -> Optional[str]:
    """Called from the watch loop: retry a pending restart when due."""
    pending = load_pending(cfg)
    if not pending:
        return None
    now = time.time() if now is None else now
    ids = list(pending["ids"])
    if not cfg.auto_restart_app:
        return None
    if now - float(pending.get("first", now)) > cfg.restart_retry_window_s:
        _save_pending(cfg, None)
        _record(cfg, ids, {"state": "gave-up", "at": now_iso(),
                           "reason": f"not safe to restart for {cfg.restart_retry_window_s / 3600:g} h"}, echo)
        notify(cfg, f"ankifix: {', '.join(ids)} will load on next restart of {cfg.app_name}")
        echo(f"ankifix: gave up auto-restarting for {', '.join(ids)}; loads on next restart")
        return "gave-up"
    if now - float(pending.get("last_try", 0)) < cfg.restart_retry_interval_s:
        return None
    state = _attempt(cfg, ids, echo=echo, probe=probe, applied_at=float(pending.get("first", now)))
    if state == "pending":
        pending["last_try"] = now
        _save_pending(cfg, pending)
    else:
        _save_pending(cfg, None)
    return state
