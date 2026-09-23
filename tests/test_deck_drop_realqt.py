"""Deck list drag-to-reorder through the REAL AnkiWebView (PyQt6, offscreen).

The 085108 tests drove decklist.js with synthetic DOM events in plain
Chromium, so they never met AnkiWebView.dropEvent, which swallows every
drop while `allow_drops` is False (setHtml resets it on each render). In
the app the insertion line drew but no `ba:deck:reorder` ever reached
Python. This test renders the deck list inside a real AnkiWebView, starts
the drag in the page, then delivers real Qt QDragEnter/QDragMove/QDrop
events at the target row's top edge and checks the pycmd bridge:

  - stock AnkiWebView: no pycmd (the regression, proven on the real class)
  - after webview_drops.install(): `ba:deck:reorder:<src>:<target>:before`
  - a Finder-style drop carrying file URLs is still blocked
"""

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
from _realqt import ensure_real_aqt, spin  # noqa: E402

ensure_real_aqt(__file__)

from aqt.qt import (  # noqa: E402
    QApplication, QDragEnterEvent, QDragMoveEvent, QDropEvent, QMimeData,
    QPoint, QPointF, Qt, QUrl, QWebEngineView,
)

app = QApplication.instance() or QApplication(sys.argv)

import types  # noqa: E402

import aqt  # noqa: E402
from aqt.webview import AnkiWebView  # noqa: E402

# The bridge only consults mw.col (and only when requiresCol is set).
if getattr(aqt, "mw", None) is None:
    aqt.mw = types.SimpleNamespace(col=None)

import importlib.util  # noqa: E402

spec = importlib.util.spec_from_file_location(
    "webview_drops", os.path.join(ROOT, "webview_drops.py"))
webview_drops = importlib.util.module_from_spec(spec)
spec.loader.exec_module(webview_drops)

DECKS = [
    {"did": 10, "name": "Fundamentals", "depth": 0},
    {"did": 11, "name": "Amino Acids", "depth": 1},
    {"did": 12, "name": "Exam 1", "depth": 1},
    {"did": 13, "name": "00 Learning Goals", "depth": 2},
    {"did": 14, "name": "01 pKa", "depth": 2},
    {"did": 15, "name": "02 Peptides", "depth": 2},
    {"did": 20, "name": "MCAT", "depth": 0},
]


def read(name):
    with open(os.path.join(ROOT, "web", name), encoding="utf-8") as f:
        return f.read()


PAGE = (
    "<html><head><style>" + read("tokens.css") + read("decklist.css")
    + "</style><script>window.__baOpts={deckList:{dragMove:true}};"
    + "window.__baDeckTree=" + json.dumps(DECKS) + ";</script></head>"
    + "<body style='margin:0'><center class='ba-home ba-multi'></center>"
    + "<script>" + read("decklist.js") + "</script>"
    + "<script>" + read("homedeck.js") + "</script></body></html>"
)


def js(web, code):
    box = []
    web.page().runJavaScript(code, lambda r: box.append(r))
    for _ in range(50):
        if box:
            return box[0]
        spin(20)
    raise AssertionError("runJavaScript timed out: " + code)


def make_view():
    web = AnkiWebView()
    web.requiresCol = False
    got = []
    web.set_bridge_command(lambda cmd: got.append(cmd), None)
    web.resize(900, 600)
    web.show()
    QWebEngineView.setHtml(web, PAGE, QUrl("http://127.0.0.1/"))
    for _ in range(100):
        spin(30)
        if js(web, "typeof pycmd === 'function' && "
                   "document.querySelectorAll('.ad-list-row').length") == len(DECKS):
            break
    else:
        raise AssertionError("deck list never rendered in the webview")
    assert web.allow_drops is False, "stock AnkiWebView state on the deck browser"
    return web, got


def drop_on_top_edge(web, src, target, urls=False):
    # The drag starts inside the page (as a real mouse drag would); the
    # drop arrives through Qt exactly as the OS delivers it.
    js(web, "(function(){var r=document.querySelector('.ad-list-row[data-did=\"%d\"]');"
            "var dt=new DataTransfer();r.dispatchEvent(new DragEvent('dragstart',"
            "{bubbles:true,cancelable:true,dataTransfer:dt}));})()" % src)
    spin(50)
    rect = js(web, "(function(){var b=document.querySelector('.ad-list-row[data-did=\"%d\"]')"
                   ".getBoundingClientRect();return [b.left,b.top,b.width,b.height];})()"
                   % target)
    x = int(rect[0] + rect[2] / 2)
    y = int(rect[1] + rect[3] * 0.1)
    md = QMimeData()
    if urls:
        md.setUrls([QUrl.fromLocalFile("/tmp/some.apkg")])
    else:
        md.setText(str(src))
    act = Qt.DropAction.MoveAction
    btn, mod = Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier
    for ev in (QDragEnterEvent(QPoint(x, y), act, md, btn, mod),
               QDragMoveEvent(QPoint(x, y), act, md, btn, mod),
               QDropEvent(QPointF(x, y), act, md, btn, mod)):
        QApplication.sendEvent(web, ev)
        spin(150)
    spin(200)


def main():
    # 1. Stock AnkiWebView: the drop never reaches the page.
    web, got = make_view()
    drop_on_top_edge(web, 15, 13)
    assert not [c for c in got if c.startswith("ba:deck:")], (
        "stock AnkiWebView should swallow the drop (the bug), got %r" % got)
    web.close()
    print("ok  stock AnkiWebView swallows the deck drop (reproduces the bug)")

    # 2. With the add-on's passthrough: the reorder pycmd arrives.
    web, got = make_view()
    assert webview_drops.install(web)
    assert webview_drops.install(web), "idempotent"
    drop_on_top_edge(web, 15, 13)
    cmds = [c for c in got if c.startswith("ba:deck:")]
    assert cmds == ["ba:deck:reorder:15:13:before"], cmds
    print("ok  nested deck drop on a sibling's top edge sends %s" % cmds[0])

    got.clear()
    drop_on_top_edge(web, 20, 10)
    cmds = [c for c in got if c.startswith("ba:deck:")]
    assert cmds == ["ba:deck:reorder:20:10:before"], cmds
    print("ok  top-level deck drop sends %s" % cmds[0])

    # 3. A file dragged in from Finder is still Anki's business (blocked).
    got.clear()
    drop_on_top_edge(web, 14, 13, urls=True)
    assert not [c for c in got if c.startswith("ba:deck:")], got
    print("ok  external file drop is still blocked")
    web.close()
    print("PASS test_deck_drop_realqt")


if __name__ == "__main__":
    main()
