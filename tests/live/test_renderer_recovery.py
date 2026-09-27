"""Live test: the window comes back when the main webview's renderer dies.

Ticket 20260923-153047: the renderer process behind mw.web died (out of
memory) while the user was reviewing, and the whole window stayed grey
until Anki+ was quit; the next card could never be shown. This kills the
real renderer process under the reviewer (SIGKILL, as the OS OOM path
ends it), then checks what the user would see: a live page again within a
few seconds, still in the reviewer, on the same card (it was never
answered), and the answer can be revealed. It does the same on the deck
list, and checks that a renderer dying over and over stops being reloaded
instead of looping.
"""

import os
import signal
import time


def _pid(t):
    try:
        return int(t.mw.web.page().renderProcessPid())
    except Exception:
        return 0


def _alive(t, expr="!!document.body"):
    try:
        return t.js(expr, timeout=1.5)
    except Exception:
        return None


def _kill_renderer(t):
    pid = _pid(t)
    if pid > 0:
        os.kill(pid, signal.SIGKILL)
    return pid


def run(t):
    mw = t.mw
    col = mw.col
    did = col.decks.id("Solo")
    col.decks.select(did)
    mw.moveToState("review")
    rv = mw.reviewer
    t.check("reviewer opened", t.wait_until(lambda: mw.state == "review" and rv.card is not None, 20), mw.state)
    t.wait_js("!!document.getElementById('qa') && document.getElementById('qa').innerText.indexOf('Solo front') >= 0", 10)
    cid = rv.card.id
    front = t.js("document.getElementById('qa').innerText")

    old = _kill_renderer(t)
    t.check("the renderer was running and was killed", old > 0, old)
    t0 = time.monotonic()
    back = t.wait_until(
        lambda: _pid(t) not in (0, old) and _alive(
            t, "!!document.getElementById('qa') && document.getElementById('qa').innerText.length > 0"),
        timeout=15, step_ms=200)
    t.note(f"page back after {time.monotonic() - t0:.1f}s (renderer {old} -> {_pid(t)})")
    t.check("the reviewer page is live again (not a grey window)", back, _pid(t))
    t.check("still in the reviewer", mw.state == "review", mw.state)
    t.check("on the same, unanswered card", rv.card is not None and rv.card.id == cid,
            rv.card and rv.card.id)
    now = _alive(t, "document.getElementById('qa') ? document.getElementById('qa').innerText : ''") or ""
    t.check("the card's question is on screen", "Solo front" in now, [front, now])
    rv._showAnswer()
    t.check("the answer can be revealed",
            t.wait_until(lambda: "Solo back" in (_alive(t, "document.getElementById('qa').innerText") or ""), 10),
            _alive(t, "document.getElementById('qa').innerText"))

    # Deck list: same recovery.
    mw.moveToState("deckBrowser")
    t.wait_js("document.querySelectorAll('.ad-list-row').length > 0", 20)
    old = _kill_renderer(t)
    back = t.wait_until(lambda: _pid(t) not in (0, old) and _alive(
        t, "document.querySelectorAll('.ad-list-row').length > 0"), timeout=15, step_ms=200)
    t.check("the deck list comes back after its renderer dies", back and mw.state == "deckBrowser", mw.state)

    # A renderer that dies again and again is not reloaded forever.
    kills = 0
    for _ in range(6):
        old = _kill_renderer(t)
        if old <= 0:
            break
        kills += 1
        t.wait_until(lambda: _pid(t) not in (0, old), timeout=5, step_ms=100)
        t.pump(400)
    t.pump(1500)
    last = _pid(t)
    if last > 0:
        os.kill(last, signal.SIGKILL)
    t.pump(2500)
    t.check("repeated deaths stop being reloaded (no crash loop)",
            _pid(t) in (0, last) or not _alive(t), f"{kills} kills, pid now {_pid(t)}")

    # With the page dead for good, the bug-report hotkey still files a
    # ticket (it used to wait forever on the dead webview's callback).
    import importlib
    import sys
    import tempfile
    pkg = next(k for k in sys.modules if k.startswith("anki-design") and "." not in k)
    br = importlib.import_module(pkg + ".bugreport")
    saved_dir = br.TICKETS_DIR
    br.TICKETS_DIR = tempfile.mkdtemp(prefix="ba-live-tickets-")
    try:
        done = []
        t0 = time.monotonic()
        br.create_ticket("grey window", "", True, on_done=done.append)
        t.wait_until(lambda: done, timeout=10, step_ms=100)
        took = time.monotonic() - t0
        cap = done[0]["capture"] if done else {}
        t.check("Cmd+Shift+B files a ticket within 5 s on a dead webview",
                done and took < 5, f"{took:.1f}s")
        t.check("the ticket says the webview was unresponsive and keeps the Qt screenshot",
                "webview unresponsive" in (cap.get("note") or "") and cap.get("screenshot_path") == "screenshot.png",
                cap)
    finally:
        br.TICKETS_DIR = saved_dir
