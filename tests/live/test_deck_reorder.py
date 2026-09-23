"""Live: drag a deck onto the top edge of a sibling on the home deck list
and it moves there, in the list the user sees after the re-render.

Fixture tree: Parent::{A,B,C}. Drag C onto A's top edge -> A's group
should read C, A, B.

Two drags, each checked on its own:
  * "js drag": the pointer + HTML5 drag events a real drag produces
    (mousedown, dragstart, dragenter/dragover at the edge, drop, dragend,
    mouseup) dispatched in the real home webview, so the add-on's page
    script, pycmd bridge, Python handler and re-render all run for real;
  * "native drag": a real mouse press / move / release entering Qt through
    QWindowSystemInterface (QTest's QWindow overloads), the way a user's
    mouse reaches QtWebEngine. When the page uses HTML5 drag-and-drop the
    OS drag session cannot run offscreen, so the test then checks the Qt
    hand-off that session depends on: the home webview must pass drag
    enter / drop events to the web engine.
"""

DRAG_JS = r"""
(function (srcName, dstName, frac) {
  function row(name) {
    return Array.from(document.querySelectorAll('.ad-list-row')).find(function (r) {
      var n = r.querySelector('.ad-list-name');
      return n && n.textContent.trim() === name;
    });
  }
  var src = row(srcName), dst = row(dstName);
  if (!src || !dst) return 'missing row ' + (!src ? srcName : dstName);
  var s = src.getBoundingClientRect(), d = dst.getBoundingClientRect();
  var sx = s.left + s.width / 2, sy = s.top + s.height / 2;
  var dx = d.left + d.width / 2, dy = d.top + d.height * frac;
  var dt = new DataTransfer();
  function mouse(type, el, x, y, buttons) {
    el.dispatchEvent(new MouseEvent(type, {bubbles: true, cancelable: true,
      clientX: x, clientY: y, button: 0, buttons: buttons, view: window}));
  }
  function drag(type, el, x, y) {
    var ev = new DragEvent(type, {bubbles: true, cancelable: true,
      clientX: x, clientY: y, dataTransfer: dt});
    el.dispatchEvent(ev);
    return ev.defaultPrevented;
  }
  var nameEl = src.querySelector('.ad-list-name') || src;
  mouse('mousedown', nameEl, sx, sy, 1);
  mouse('mousemove', nameEl, sx, sy + 4, 1);
  drag('dragstart', nameEl, sx, sy + 4);
  var dstName_ = dst.querySelector('.ad-list-name') || dst;
  drag('dragenter', dstName_, dx, dy);
  var accepted = drag('dragover', dstName_, dx, dy);
  var marks = ['drop', 'before', 'after'].filter(function (k) {
    return dst.classList.contains('ad-list-row--' + k); });
  drag('drop', dstName_, dx, dy);
  drag('dragend', nameEl, dx, dy);
  mouse('mouseup', dstName_, dx, dy, 0);
  return JSON.stringify({accepted: accepted, marks: marks});
})(%r, %r, %s)
"""

LOG_JS = r"""
(function () {
  window.__liveEvents = [];
  ['mousedown', 'dragstart', 'dragover', 'drop', 'dragend', 'mouseup'].forEach(function (k) {
    document.addEventListener(k, function () {
      var l = window.__liveEvents;
      if (l[l.length - 1] !== k) l.push(k);
    }, true);
  });
  return true;
})()
"""



EXPAND_JS = r"""
(function () {
  // Click the chevron of every collapsed parent, as a user would, so the
  // children have real boxes to drag and drop on.
  var n = 0;
  document.querySelectorAll('.ad-list-chev--collapsed').forEach(function (c) {
    if (c.tagName === 'BUTTON') { c.click(); n++; }
  });
  return n;
})()
"""

VISIBLE_JS = r"""
Array.from(document.querySelectorAll('.ad-list-row'))
  .filter(function (r) { return r.getBoundingClientRect().height > 0; })
  .map(function (r) { return r.querySelector('.ad-list-name').textContent.trim(); })
"""


def row_css(t, name):
    did = t.js(
        "(function(n){var r=Array.from(document.querySelectorAll('.ad-list-row'))"
        ".find(function(r){return r.querySelector('.ad-list-name').textContent.trim()===n});"
        "return r?r.getAttribute('data-did'):null})(%r)" % name)
    return '.ad-list-row[data-did="%s"]' % did


def expand(t):
    t.wait_js("document.querySelectorAll('.ad-list-row').length >= 5", timeout=20)
    t.js(EXPAND_JS)
    t.pump(200)
    return t.js(VISIBLE_JS) or []


def group(names):
    i = names.index("Parent")
    return names[i + 1:i + 4]


def run(t):
    import json

    visible = expand(t)
    before = t.deck_list_names()
    t.check("fixture group starts A, B, C and is expanded",
            group(before) == ["A", "B", "C"] and all(n in visible for n in "ABC"),
            f"list {before}, visible {visible}")

    # 1. The drag, as page events.
    info = t.js(DRAG_JS % ("C", "A", 0.1))
    t.note(f"js drag events: {info}")
    try:
        marks = json.loads(info)
    except Exception:
        marks = {"accepted": False, "marks": [], "raw": info}
    t.check("js drag: the top edge of A accepts the drop (insertion line)",
            marks.get("accepted") and marks.get("marks") == ["before"], marks)
    # The drop goes pycmd -> Python -> config -> deckBrowser.refresh().
    t.pump(1500)
    expand(t)
    after_js = t.deck_list_names()
    t.check("js drag: after re-render the group reads C, A, B",
            group(after_js) == ["C", "A", "B"],
            f"expected ['C', 'A', 'B'], saw {group(after_js)} (full list {after_js})")

    # 2. A real mouse drag through Qt, on the top level so it does not
    # depend on what the first drag did: Solo onto Parent's top edge.
    mw = t.mw
    expand(t)
    start = t.deck_list_names()
    p0 = t.element_center(row_css(t, "Solo") + " .ad-list-name")
    p1 = t.element_center(row_css(t, "Parent"), 0.1)
    if not (p0 and p1) or t.QTest is None:
        t.check("native drag: rows have boxes", False, (p0, p1))
        return
    t.js(LOG_JS)
    t.native_drag(mw.web, p0, p1)
    t.pump(300)
    events = t.js("JSON.stringify(window.__liveEvents || [])") or "[]"
    t.note(f"native drag page events: {events}")
    t.pump(1200)
    expand(t)
    after_native = t.deck_list_names()
    top = [n for n in after_native if n in ("Parent", "Solo")]
    html5 = "dragstart" in events
    if not html5:
        # A pointer-driven drag (no HTML5 DnD) is fully exercised offscreen.
        t.check("native drag: after re-render Solo sits above Parent",
                top == ["Solo", "Parent"],
                f"expected top level ['Solo', 'Parent'], saw {top} (full list "
                f"{after_native}, before {start})")
        return

    # 3. HTML5 drag-and-drop is in use. Offscreen Qt cannot run the OS drag
    # session (QOffscreenDrag cancels it at once: dragstart then dragend),
    # so check the Qt layer a native drag goes through instead: the home
    # webview must hand drag-enter / drag-move / drop to the web engine.
    # Anki's MainWebView treats every drag on the deck browser as a file
    # import and never passes a non-file drag on; that is invisible to
    # page-level tests and fatal to a real drag.
    t.note("HTML5 drag in use: the OS drag loop cannot run offscreen; "
           "checking the Qt drag/drop hand-off instead")
    from aqt.qt import (QDragEnterEvent, QDragMoveEvent, QDropEvent,
                        QMimeData, QPointF, Qt)
    import aqt.webview as wv

    base = wv.QWebEngineView
    hits = {"enter": 0, "move": 0, "drop": 0}
    saved = {n: getattr(base, n) for n in ("dragEnterEvent", "dragMoveEvent", "dropEvent")}

    def spy(key, name):
        def f(self, ev):
            hits[key] += 1
            return saved[name](self, ev)
        return f

    base.dragEnterEvent = spy("enter", "dragEnterEvent")
    base.dragMoveEvent = spy("move", "dragMoveEvent")
    base.dropEvent = spy("drop", "dropEvent")
    try:
        md = QMimeData()
        md.setText("deck drag")  # what Chromium puts on an in-page drag
        acts = Qt.DropAction.MoveAction | Qt.DropAction.CopyAction
        btn, mod = Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier
        mw.web.dragEnterEvent(QDragEnterEvent(p1, acts, md, btn, mod))
        mw.web.dragMoveEvent(QDragMoveEvent(p1, acts, md, btn, mod))
        mw.web.dropEvent(QDropEvent(QPointF(p1), acts, md, btn, mod))
    finally:
        for n, f in saved.items():
            setattr(base, n, f)
    t.check("Qt: a drag entering the home deck list reaches the web engine",
            hits["enter"] > 0,
            ("" if hits["enter"] else
             f"MainWebView.dragEnterEvent swallowed it (state {mw.state}); ")
            + f"hand-offs {hits}")
    t.check("Qt: a drop on the home deck list reaches the web engine",
            hits["drop"] > 0,
            ("" if hits["drop"] else
             f"MainWebView.dropEvent swallowed it (state {mw.state}); ")
            + f"hand-offs {hits}")

    # 4. A file dragged in from Finder is still Anki's: MainWebView accepts
    # it for import and it is NOT handed to the web engine (which would
    # navigate the view to the file).
    import os
    import tempfile

    fd, path = tempfile.mkstemp(suffix=".apkg")
    os.close(fd)
    hits = {"enter": 0}
    saved_enter = base.dragEnterEvent

    def spy_enter(self, ev):
        hits["enter"] += 1
        return saved_enter(self, ev)

    base.dragEnterEvent = spy_enter
    try:
        from aqt.qt import QUrl

        md = QMimeData()
        md.setUrls([QUrl.fromLocalFile(path)])
        ev = QDragEnterEvent(p1, Qt.DropAction.CopyAction, md,
                             Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
        ev.ignore()
        mw.web.dragEnterEvent(ev)
    finally:
        base.dragEnterEvent = saved_enter
        os.unlink(path)
    t.check("Qt: a file dragged in from Finder still goes to Anki's import",
            ev.isAccepted() and hits["enter"] == 0,
            f"accepted {ev.isAccepted()}, handed to web engine {hits['enter']}x")
