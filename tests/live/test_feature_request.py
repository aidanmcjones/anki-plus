"""Live test: Cmd+Shift+F and the sidebar's "Request feature" row open the
feature request dialog, and saving it files a ticket ankifix will build.

Drives the real app the way the user does: presses Cmd+Shift+F through the
main window's QWindow (the window-system path QShortcut matching uses),
types a description into the dialog, clicks "Request feature", then reads
the ticket.json that landed on disk. Also clicks the sidebar row in the real
home webview, presses the hotkey twice (one dialog, not two), and checks
Cmd+Shift+B still opens the bug dialog. Tickets go to a temp folder, never
~/AnkiTickets.
"""

import importlib
import json
import os
import shutil
import sys
import tempfile


def _dialogs(t, mode=None):
    from aqt.qt import QApplication, QDialog

    out = []
    for w in QApplication.topLevelWidgets():
        if isinstance(w, QDialog) and w.objectName() == "ba-bugreport" and w.isVisible():
            if mode is None or w.property("ba_mode") == mode:
                out.append(w)
    return out


def _press(t, key, mods):
    win = t.mw.windowHandle()
    t.mw.activateWindow()
    t.pump(100)
    t.QTest.keyClick(win, key, mods)
    t.pump(300)


def _close_all(t):
    for d in _dialogs(t):
        d.reject()
    t.pump(200)


def run(t):
    from aqt.qt import QPlainTextEdit, QPushButton, Qt

    pkg = next(k for k in sys.modules if k.startswith("anki-design") and "." not in k)
    br = importlib.import_module(pkg + ".bugreport")
    saved_dir = br.TICKETS_DIR
    tdir = tempfile.mkdtemp(prefix="ba-live-feature-")
    br.TICKETS_DIR = tdir
    CMD_SHIFT = Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier
    try:
        # 1. The hotkey opens the feature dialog.
        _press(t, Qt.Key.Key_F, CMD_SHIFT)
        ok = t.wait_until(lambda: _dialogs(t, "feature"), timeout=5, step_ms=100)
        dlgs = _dialogs(t, "feature")
        t.check("Cmd+Shift+F opens the Request a feature dialog",
                bool(ok) and len(dlgs) == 1 and dlgs[0].windowTitle() == "Request a feature",
                [d.windowTitle() for d in _dialogs(t)])
        if not dlgs:
            return
        dlg = dlgs[0]

        # 2. A second press raises the same dialog instead of stacking one.
        _press(t, Qt.Key.Key_F, CMD_SHIFT)
        t.check("pressing it again does not open a second dialog",
                len(_dialogs(t, "feature")) == 1, len(_dialogs(t, "feature")))

        # 3. Saving with an empty description is refused, nothing is filed.
        save = next(b for b in dlg.findChildren(QPushButton) if b.text() == "Request feature")
        t.QTest.mouseClick(save, Qt.MouseButton.LeftButton)
        t.pump(300)
        t.check("an empty description is refused and nothing is filed",
                dlg.isVisible() and not os.listdir(tdir), os.listdir(tdir))

        # 4. Type a multi-line description, click Save, a ticket lands.
        box = dlg.findChild(QPlainTextEdit, "ba-feature-note")
        box.setFocus()
        t.QTest.keyClicks(box, "Add a Suspend item to the Browse menu")
        t.QTest.keyClick(box, Qt.Key.Key_Return)
        t.QTest.keyClicks(box, "with a shortcut")
        t.pump(100)
        t.QTest.mouseClick(save, Qt.MouseButton.LeftButton)
        t.wait_until(lambda: not dlg.isVisible(), timeout=8, step_ms=100)
        t.check("saving closes the dialog", not dlg.isVisible(), "")
        ids = [d for d in os.listdir(tdir) if os.path.isdir(os.path.join(tdir, d))]
        ticket = {}
        if ids:
            with open(os.path.join(tdir, ids[0], "ticket.json")) as fh:
                ticket = json.load(fh)
        t.check("one ticket was filed", len(ids) == 1, ids)
        t.check("the ticket is an app build: kind app, request feature, status new",
                ticket.get("kind") == "app" and ticket.get("request") == "feature"
                and ticket.get("status") == "new",
                {k: ticket.get(k) for k in ("kind", "request", "status")})
        t.check("the description is kept verbatim, newline included",
                ticket.get("note") == "Add a Suspend item to the Browse menu\nwith a shortcut",
                ticket.get("note"))
        t.check("the screenshot was captured",
                (ticket.get("capture") or {}).get("screenshot_path") == "screenshot.png"
                and os.path.isfile(os.path.join(tdir, ids[0], "screenshot.png")) if ids else False,
                (ticket.get("capture") or {}).get("screenshot_path"))

        # 5. The sidebar row is on screen and opens the same dialog.
        t.wait_js("!!document.querySelector('.ba-side-item[data-cmd=\"feature\"]')", timeout=10)
        label = t.js(
            "(function(){var b=document.querySelector('.ba-side-item[data-cmd=\"feature\"]');"
            "return b ? b.textContent : null;})()"
        )
        t.check("the sidebar shows a Request feature row", bool(label) and "Request feature" in label, label)
        t.js("document.querySelector('.ba-side-item[data-cmd=\"feature\"]').click(); true")
        ok = t.wait_until(lambda: _dialogs(t, "feature"), timeout=5, step_ms=100)
        t.check("clicking the sidebar row opens the feature dialog", bool(ok), "")
        _close_all(t)

        # 6. The bug hotkey is unchanged.
        _press(t, Qt.Key.Key_B, CMD_SHIFT)
        ok = t.wait_until(lambda: _dialogs(t, "bug"), timeout=5, step_ms=100)
        bug = _dialogs(t, "bug")
        t.check("Cmd+Shift+B still opens Report a bug",
                bool(ok) and bug and bug[0].windowTitle() == "Report a bug",
                [d.windowTitle() for d in _dialogs(t)])
        _close_all(t)
    finally:
        _close_all(t)
        br.TICKETS_DIR = saved_dir
        shutil.rmtree(tdir, ignore_errors=True)
