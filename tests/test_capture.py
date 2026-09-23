import base64
import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from ankibug import capture
from ankibug import cli as bug_cli
from ankibug import schema

websockets_server = pytest.importorskip("websockets.sync.server")
from websockets.sync.server import serve  # noqa: E402


FAKE_PNG = base64.b64encode(b"\x89PNG\r\nFAKE").decode("ascii")


def _cdp_handler(ws):
    # Push one ambient console-error event as soon as the client connects,
    # so collect_events() picks it up during the enable phase.
    ws.send(json.dumps({
        "method": "Runtime.consoleAPICalled",
        "params": {"type": "error", "args": [{"value": "boom from console"}]},
    }))
    try:
        for raw in ws:
            msg = json.loads(raw)
            mid = msg.get("id")
            method = msg.get("method")
            params = msg.get("params", {}) or {}
            if method == "Runtime.evaluate":
                expr = params.get("expression", "")
                if "getElementById('qa')" in expr:
                    value = "<div id=\"qa\">broken card</div>"
                elif "innerText" in expr:
                    value = "visible page text"
                elif "__baReviewerState" in expr:
                    value = {"state": "question"}
                elif "__baErrors" in expr:
                    value = ["ring buffer error"]
                else:
                    value = None
                result = {"result": {"value": value}}
            elif method == "Page.captureScreenshot":
                result = {"data": FAKE_PNG}
            else:
                result = {}
            ws.send(json.dumps({"id": mid, "result": result}))
    except Exception:
        pass


@pytest.fixture()
def fake_cdp_server():
    server = serve(_cdp_handler, "127.0.0.1", 0)
    port = server.socket.getsockname()[1]
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        yield port
    finally:
        server.shutdown()


@pytest.fixture()
def fake_json_server(fake_cdp_server):
    ws_port = fake_cdp_server
    pages = [
        {
            "title": "main webview",
            "url": "http://127.0.0.1:40000/congrats",
            "webSocketDebuggerUrl": "ws://127.0.0.1:{}/page/1".format(ws_port),
        },
        {
            "title": "top toolbar",
            "url": "http://127.0.0.1:40000/toolbar",
            "webSocketDebuggerUrl": "ws://127.0.0.1:{}/page/2".format(ws_port),
        },
    ]
    body = json.dumps(pages).encode("utf-8")

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            if self.path == "/json":
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_response(404)
                self.end_headers()

        def log_message(self, *args):  # noqa: D401
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        yield port
    finally:
        server.shutdown()


def test_fetch_pages_unreachable():
    # Nothing listening on this port.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        free_port = s.getsockname()[1]
    result = capture.fetch_pages(host="127.0.0.1", port=free_port, timeout=0.5)
    assert result is None


def test_capture_unreachable_degrades_gracefully():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        free_port = s.getsockname()[1]
    log = []
    cap, shot, qa, reviewer_state = capture.capture(host="127.0.0.1", port=free_port, log=log)
    assert cap is None
    assert shot is None
    assert qa is None
    assert reviewer_state is None
    assert any("not running" in line or "could not reach" in line for line in log)


def test_fetch_pages_lists_expected_titles(fake_json_server):
    pages = capture.fetch_pages(host="127.0.0.1", port=fake_json_server, timeout=2.0)
    assert pages is not None
    titles = {p["title"] for p in pages}
    assert "main webview" in titles
    assert "top toolbar" in titles


def test_full_capture_against_fake_server(fake_json_server):
    log = []
    cap, shot, qa, reviewer_state = capture.capture(
        host="127.0.0.1", port=fake_json_server, console_seconds=0.3, log=log,
    )
    assert cap is not None
    assert cap["qa_html_path"] == "qa.html"
    assert qa == "<div id=\"qa\">broken card</div>"
    assert cap["visible_text"] == "visible page text"
    assert cap["screenshot_path"] == "screenshot.png"
    assert shot == base64.b64decode(FAKE_PNG)
    assert reviewer_state == "question"
    assert any("boom from console" in e for e in cap["console_errors"])
    assert any("ring buffer error" in e for e in cap["console_errors"])
    assert {"title": "main webview", "url": "http://127.0.0.1:40000/congrats"} in cap["pages"]


def test_get_anki_version_missing_file(tmp_path):
    missing = str(tmp_path / "nope" / ".version")
    assert capture.get_anki_version(missing) is None


def test_get_anki_version_present(tmp_path):
    vfile = tmp_path / ".version"
    vfile.write_text("26.08.1")
    assert capture.get_anki_version(str(vfile)) == "26.08.1"


def test_get_addon_git_info_nonexistent_repo(tmp_path):
    info = capture.get_addon_git_info(str(tmp_path / "not-a-repo"))
    assert info == {"branch": None, "commit": None, "dirty": None}


def test_get_app_info_shape(tmp_path):
    info = capture.get_app_info(addon_repo=str(tmp_path), version_file=str(tmp_path / "missing"))
    assert set(info.keys()) == {"anki_version", "addon_repo", "branch", "commit", "dirty"}


# --------------------------------------------------- capture-time classification
# ankibug/cli.py's _classify_kind() reuses ankifix's own keyword classifier
# (ankifix.classify.classify) so hotkey/chat tickets aren't stuck at
# kind=unknown in `ankibug list` until ankifix runs. These call it directly
# (no disk I/O) rather than driving cmd_file end to end, since cmd_file's
# default write path is the real ~/AnkiTickets, which tests must never touch.

def test_classify_kind_deck_note():
    t = schema.new_ticket("the answer on this card is wrong", kind="unknown",
                           reviewer={"notetype": None}, capture=None)
    assert bug_cli._classify_kind(t) == "deck"


def test_classify_kind_app_note():
    t = schema.new_ticket("editor crashes when I paste", kind="unknown",
                           reviewer={"notetype": None}, capture=None)
    assert bug_cli._classify_kind(t) == "app"


def test_classify_kind_course_notetype():
    t = schema.new_ticket("something weird", kind="unknown",
                           reviewer={"notetype": "CourseB-Basic"}, capture=None)
    assert bug_cli._classify_kind(t) == "deck"


def test_new_ticket_kind_promoted_from_unknown_by_cmd_file_logic():
    # Mirrors the "if ticket['kind'] == 'unknown': ..." block in cmd_file:
    # unknown gets promoted to app/deck; an explicit kind is left alone.
    t = schema.new_ticket("the answer on this card is wrong", kind="unknown",
                           reviewer={"notetype": None}, capture=None)
    guessed = bug_cli._classify_kind(t)
    if guessed:
        t["kind"] = guessed
        schema.validate(t)
    assert t["kind"] == "deck"

    t2 = schema.new_ticket("the answer on this card is wrong", kind="app",
                            reviewer={"notetype": None}, capture=None)
    assert t2["kind"] == "app"  # an explicit --kind is never overridden


def test_classify_kind_returns_none_if_ankifix_not_importable(monkeypatch):
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "ankifix.classify" or name.startswith("ankifix"):
            raise ImportError("simulated: ankifix not installed")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    t = schema.new_ticket("editor crashes when I paste", kind="unknown")
    assert bug_cli._classify_kind(t) is None
