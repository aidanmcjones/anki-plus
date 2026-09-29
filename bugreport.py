"""Anki Design — in-app bug reporting.

Cmd+Shift+B (configurable via config.json's `bug_report_shortcut`) and the
"Report a bug" command-palette entry (cmdk.py) both land on `open_dialog()`
below: a small QDialog, styled like deadlines.py's, for a one-line note, an
optional "what did you expect" line, and a screenshot toggle. Save writes a
ticket under ~/AnkiTickets/<id>/ — ticket.json, screenshot.png, qa.html —
and appends a row to ~/AnkiTickets/index.md.

Cmd+Shift+F (`feature_request_shortcut`), the sidebar's "Request feature"
row and the "Request a feature" palette entry open the same dialog in
feature mode (`open_feature_dialog()`): a free-text description of what
Anki+ should do. The ticket is filed as `kind: "app"` with
`request: "feature"`, so the ankifix watcher builds it on a branch, proves
it with a live test in a throwaway app, and lands it like a bug fix.

The ticket shape follows the schema documented at
/Users/aidanjones/dev/ankibug/SCHEMA.md. We prefer importing the sibling
`ankibug` package (schema construction + validation) when it's on disk, so
the two repos share one definition instead of two copies drifting apart;
when it isn't importable we build the identical shape by hand.
"""

from __future__ import annotations

import datetime
import json
import os
import re
import subprocess
import sys
from typing import Any, Dict, Optional

from aqt import mw

ADDON = __name__.split(".")[0]
ADDON_SRC = os.path.dirname(os.path.abspath(__file__))

TICKETS_DIR = os.path.expanduser("~/AnkiTickets")
ANKIBUG_SRC = "/Users/aidanjones/dev/ankibug"

# Prefer the sibling `ankibug` package for schema construction when it's
# checked out — see the module docstring. Degrades to a hand-built dict of
# the identical shape when it isn't importable (e.g. this add-on shipped
# standalone, without ~/dev/ankibug present).
_schema = None
try:
    if ANKIBUG_SRC not in sys.path:
        sys.path.insert(0, ANKIBUG_SRC)
    from ankibug import schema as _schema  # type: ignore
except Exception:
    _schema = None


def _config() -> Dict[str, Any]:
    return mw.addonManager.getConfig(ADDON) or {}


# --------------------------------------------------------------------------- #
# Ticket id / environment gathering
# --------------------------------------------------------------------------- #
def _slugify(text: str, max_len: int = 40) -> str:
    text = (text or "").strip().lower()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    text = text.strip("-")
    if not text:
        return "ticket"
    return text[:max_len].strip("-") or "ticket"


def _make_id(note: str, when: datetime.datetime) -> str:
    if _schema is not None:
        try:
            return _schema.make_id(note, when)
        except Exception:
            pass
    return "{}-{}".format(when.strftime("%Y%m%d-%H%M%S"), _slugify(note))


def _git_info(repo_dir: str) -> Dict[str, Optional[Any]]:
    """branch/commit/dirty for the running add-on's own checkout. Any field
    that can't be determined (git missing, not a repo) comes back None."""
    def _run(args):
        try:
            out = subprocess.run(
                args, cwd=repo_dir, capture_output=True, text=True, timeout=3,
            )
            if out.returncode != 0:
                return None
            return out.stdout.strip()
        except Exception:
            return None

    branch = _run(["git", "rev-parse", "--abbrev-ref", "HEAD"])
    commit = _run(["git", "rev-parse", "HEAD"])
    status = _run(["git", "status", "--porcelain"])
    return {
        "branch": branch or None,
        "commit": commit or None,
        "dirty": (bool(status) if status is not None else None),
    }


def _anki_version() -> Optional[str]:
    try:
        import anki  # type: ignore

        v = getattr(anki, "version", None)
        if v:
            return str(v)
    except Exception:
        pass
    try:
        with open(os.path.expanduser("~/dev/anki/.version"), "r", encoding="utf-8") as fh:
            v = fh.read().strip()
            if v:
                return v
    except Exception:
        pass
    return None


def _app_info() -> Dict[str, Any]:
    git = _git_info(ADDON_SRC)
    return {
        "anki_version": _anki_version(),
        "addon_repo": ADDON_SRC,
        "branch": git["branch"],
        "commit": git["commit"],
        "dirty": git["dirty"],
    }


def _reviewer_info() -> Dict[str, Any]:
    out: Dict[str, Any] = {
        "state": None,
        "card_id": None,
        "note_id": None,
        "notetype": None,
        "deck": None,
        "template_ord": None,
    }
    rv = getattr(mw, "reviewer", None)
    if rv is None:
        return out
    state = getattr(rv, "state", None)
    if state in ("question", "answer"):
        out["state"] = state
    card = getattr(rv, "card", None)
    if card is None:
        return out
    try:
        out["card_id"] = int(card.id)
    except Exception:
        pass
    try:
        out["note_id"] = int(card.nid)
    except Exception:
        pass
    try:
        nt = card.note_type()
        if nt:
            out["notetype"] = nt.get("name")
    except Exception:
        pass
    try:
        out["deck"] = mw.col.decks.name(card.did)
    except Exception:
        pass
    try:
        out["template_ord"] = int(card.ord)
    except Exception:
        pass
    return out


def _active_webview():
    """Best-effort pick of "the webview the user is actually looking at".

    Editor overlays (Add Cards / Browse embeds — see cmdk.py's
    `_any_embed_open` docstring) are their own QWebEngineViews, not mw.web,
    so the most reliable signal is Qt focus. Falls back to the reviewer's
    webview during review, then mw.web (deck list / overview)."""
    try:
        from aqt.qt import QApplication
        from aqt.webview import AnkiWebView

        node = QApplication.focusWidget()
        while node is not None:
            if isinstance(node, AnkiWebView):
                return node
            node = node.parentWidget()
    except Exception:
        pass
    try:
        if getattr(mw, "state", "") == "review":
            rv = getattr(mw, "reviewer", None)
            rv_web = getattr(rv, "web", None) if rv is not None else None
            if rv_web is not None:
                return rv_web
    except Exception:
        pass
    return getattr(mw, "web", None)


def _capture_screenshot(dest_path: str) -> bool:
    """QWidget.grab() of the main window — same mechanism CLAUDE.md's
    dev-only snap.sh uses: renders through Qt's backing store, no raise/
    activate/focus-steal."""
    try:
        pix = mw.grab()
        return bool(pix.save(dest_path, "PNG"))
    except Exception:
        return False


# How long the page capture may take before the ticket is filed without it.
# A live page answers in milliseconds; a renderer that has crashed (the
# window goes grey) or is stuck in a loop never calls back at all, and the
# report must still land.
EVAL_TIMEOUT_MS = 3000
UNRESPONSIVE_NOTE = (
    "webview unresponsive: the page did not answer the capture within 3 s "
    "(crashed or hung renderer); the ticket has the Qt screenshot and the "
    "app/reviewer info only"
)


def _schedule(ms: int, fn) -> None:
    """QTimer.singleShot, swappable in tests."""
    from aqt.qt import QTimer

    QTimer.singleShot(ms, fn)


_CAPTURE_JS = (
    "(function(){try{"
    "var qa=document.getElementById('qa');"
    "return {"
    "qa_html: qa ? qa.outerHTML : null,"
    "visible_text: (document.body && document.body.innerText) || '',"
    "console_errors: window.__baErrors || []"
    "};"
    "}catch(e){return {qa_html:null,visible_text:'',console_errors:[]};}"
    "})()"
)


def _write_index_row(ticket: Dict[str, Any]) -> None:
    os.makedirs(TICKETS_DIR, exist_ok=True)
    index_path = os.path.join(TICKETS_DIR, "index.md")
    header = (
        "| id | created | source | kind | status | note |\n"
        "| --- | --- | --- | --- | --- | --- |\n"
    )
    if not os.path.exists(index_path):
        with open(index_path, "w", encoding="utf-8") as fh:
            fh.write("# Anki Tickets\n\n" + header)
    note = (ticket.get("note") or "").replace("|", "\\|").replace("\n", " ")
    if len(note) > 60:
        note = note[:57] + "..."
    row = "| {id} | {created} | {source} | {kind} | {status} | {note} |\n".format(
        id=ticket["id"],
        created=ticket["created"],
        source=ticket["source"],
        kind=ticket["kind"],
        status=ticket["status"],
        note=note,
    )
    with open(index_path, "a", encoding="utf-8") as fh:
        fh.write(row)


REQUESTS = ("bug", "feature")


def create_ticket(
    note: str, expected: str, include_screenshot: bool, on_done=None, request: str = "bug",
) -> None:
    """Build and write a ticket. `request="feature"` files a feature
    request: kind is set to "app" up front (a new feature is always add-on
    code, so ankifix skips its bug-vs-deck keyword classifier) and
    `ticket["request"]` tells ankifix to build it instead of debugging it.

    Async: the #qa/console-errors capture goes
    through evalWithCallback, so `on_done(ticket_dict)` — if given — only
    fires once everything has actually landed on disk.

    Never blocks on the page: the Qt screenshot is taken first, and the
    page capture gets EVAL_TIMEOUT_MS to answer. When it does not (a dead
    or hung renderer never calls back), the ticket is filed with what was
    captured and `capture.note` says the webview was unresponsive."""
    if request not in REQUESTS:
        request = "bug"
    kind = "app" if request == "feature" else "unknown"
    when = datetime.datetime.now().astimezone()
    ticket_id = _make_id(note, when)
    ticket_dir = os.path.join(TICKETS_DIR, ticket_id)
    try:
        os.makedirs(ticket_dir, exist_ok=True)
    except Exception:
        pass

    screenshot_path = None
    if include_screenshot:
        dest = os.path.join(ticket_dir, "screenshot.png")
        if _capture_screenshot(dest):
            screenshot_path = "screenshot.png"

    app = _app_info()
    reviewer = _reviewer_info()

    state = {"done": False}

    def _finish(payload: Optional[Dict[str, Any]], unresponsive: bool = False) -> None:
        # Exactly once: the timeout and a late callback may both arrive.
        if state["done"]:
            return
        state["done"] = True
        if not isinstance(payload, dict):
            payload = {}
        qa_html = payload.get("qa_html")
        visible_text = payload.get("visible_text") or ""
        console_errors = payload.get("console_errors") or []
        if not isinstance(console_errors, list):
            console_errors = []

        qa_html_path = None
        if qa_html:
            qa_html_path = "qa.html"
            try:
                with open(os.path.join(ticket_dir, "qa.html"), "w", encoding="utf-8") as fh:
                    fh.write(qa_html)
            except Exception:
                qa_html_path = None

        capture = {
            "pages": [],
            "qa_html_path": qa_html_path,
            "visible_text": visible_text,
            "console_errors": console_errors,
            "screenshot_path": screenshot_path,
            "captured_at": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
        }
        if unresponsive:
            capture["note"] = UNRESPONSIVE_NOTE

        ticket = None
        if _schema is not None:
            try:
                ticket = _schema.new_ticket(
                    note=note,
                    source="hotkey",
                    kind=kind,
                    app=app,
                    reviewer=reviewer,
                    capture=capture,
                    when=when,
                    ticket_id=ticket_id,
                )
            except Exception:
                ticket = None
        if ticket is None:
            ticket = {
                "schema": 1,
                "id": ticket_id,
                "created": when.isoformat(timespec="seconds"),
                "source": "hotkey",
                "note": note,
                "status": "new",
                "kind": kind,
                "app": app,
                "reviewer": reviewer,
                "deck_build": None,
                "capture": capture,
                "fix": None,
            }
        # `expected` isn't part of ankibug's schema v1 — an extra field is
        # harmless (schema.validate() only checks required fields exist),
        # but the dialog asks for it, so it rides along on every ticket.
        ticket["expected"] = expected or ""
        # Optional field (ankibug SCHEMA.md): "feature" routes the ticket to
        # ankifix's build-a-feature prompt; "bug" (or absent) is a bug fix.
        ticket["request"] = request

        try:
            with open(os.path.join(ticket_dir, "ticket.json"), "w", encoding="utf-8") as fh:
                json.dump(ticket, fh, indent=2, sort_keys=False)
                fh.write("\n")
        except Exception:
            pass
        try:
            _write_index_row(ticket)
        except Exception:
            pass

        if on_done:
            try:
                on_done(ticket)
            except Exception:
                pass

    webview = _active_webview()
    if webview is None:
        _finish(None)
        return
    try:
        _schedule(EVAL_TIMEOUT_MS, lambda: _finish(None, unresponsive=True))
    except Exception:
        pass
    try:
        webview.evalWithCallback(_CAPTURE_JS, _finish)
    except Exception:
        _finish(None)


# --------------------------------------------------------------------------- #
# Dialog — styled like deadlines.py's show_dialog().
# --------------------------------------------------------------------------- #
def _dialog_qss(p: Dict[str, str], accent: str) -> str:
    from . import addcard as _addcard

    return f"""
QDialog#ba-bugreport {{ background: {p['paper']}; }}
QDialog#ba-bugreport QLabel {{
    color: {p['ink']};
    font-family: {_addcard.SANS};
    font-size: 11pt;
    background: transparent;
}}
QDialog#ba-bugreport QLabel[role="title"] {{
    font-family: {_addcard.SERIF};
    font-size: 19pt;
    font-weight: 500;
}}
QDialog#ba-bugreport QLabel[role="sub"] {{ color: {p['ink_dim']}; }}
QDialog#ba-bugreport QLabel[role="error"] {{
    color: #d1554a;
    font-size: 9.5pt;
}}
QDialog#ba-bugreport QLineEdit, QDialog#ba-bugreport QPlainTextEdit {{
    background: {p['field_bg']};
    color: {p['ink']};
    border: 1px solid {p['line2']};
    border-radius: 8px;
    padding: 7px 10px;
    font-family: {_addcard.SANS};
    font-size: 11pt;
    selection-background-color: {accent};
    selection-color: {p['paper']};
}}
QDialog#ba-bugreport QLineEdit:focus,
QDialog#ba-bugreport QPlainTextEdit:focus {{ border-color: {accent}; }}
QDialog#ba-bugreport QCheckBox {{
    color: {p['ink']};
    font-family: {_addcard.SANS};
    font-size: 10.5pt;
    spacing: 8px;
}}
QDialog#ba-bugreport QPushButton[role="primary"] {{
    background: {p['ink']};
    color: {p['paper']};
    border: 1px solid {p['ink']};
    border-radius: 6px;
    padding: 7px 16px;
    font-family: {_addcard.SANS};
    font-size: 10.5pt;
    font-weight: 500;
}}
QDialog#ba-bugreport QPushButton[role="primary"]:hover {{
    background: {p['ink_dim']}; border-color: {p['ink_dim']};
}}
QDialog#ba-bugreport QPushButton[role="primary"]:disabled {{
    background: {p['ink_faint']}; border-color: {p['ink_faint']};
}}
QDialog#ba-bugreport QPushButton[role="quiet"] {{
    background: transparent;
    color: {p['ink_dim']};
    border: 1px solid {p['line']};
    border-radius: 6px;
    padding: 7px 14px;
    font-family: {_addcard.SANS};
    font-size: 10.5pt;
}}
QDialog#ba-bugreport QPushButton[role="quiet"]:hover {{
    color: {p['ink']}; border-color: {p['line2']};
}}
"""


# Per-mode copy. "bug" is the original Cmd+Shift+B dialog, unchanged.
_MODES: Dict[str, Dict[str, str]] = {
    "bug": {
        "title": "Report a bug",
        "sub": "What went wrong? One line is enough.",
        "placeholder": "e.g. Answer buttons vanished after Undo",
        "required": "A one-line note is required.",
        "expected_label": "What did you expect? (optional)",
        "expected_placeholder": "e.g. The Again/Good/Easy row should stay visible",
        "save": "Save ticket",
        "saved": "Bug ticket saved — {id}",
    },
    "feature": {
        "title": "Request a feature",
        "sub": (
            "Describe what Anki+ should do. Claude builds it on a branch, "
            "proves it in a throwaway copy of the app, and lands it here."
        ),
        "placeholder": (
            "e.g. Add a \"Suspend\" item to the right-click menu on cards "
            "in the Browse list"
        ),
        "required": "Describe the feature first.",
        "expected_label": "Where should it live, or how should it behave? (optional)",
        "expected_placeholder": "e.g. Right below \"Reposition\", with a ⌘J shortcut",
        "save": "Request feature",
        "saved": "Feature requested, Claude is building it: {id}",
    },
}

_open_dialogs: Dict[str, Any] = {}


def open_feature_dialog() -> None:
    open_dialog("feature")


def open_dialog(mode: str = "bug") -> None:
    from aqt.qt import (
        QCheckBox,
        QDialog,
        QHBoxLayout,
        QKeySequence,
        QLabel,
        QLineEdit,
        QPlainTextEdit,
        QPushButton,
        QShortcut,
        QVBoxLayout,
        Qt,
    )

    from . import addcard as _addcard

    if mode not in _MODES:
        mode = "bug"
    text = _MODES[mode]
    feature = mode == "feature"

    # A second press of the hotkey raises the open dialog instead of
    # stacking another one on top of it.
    existing = _open_dialogs.get(mode)
    if existing is not None:
        try:
            if existing.isVisible():
                existing.raise_()
                existing.activateWindow()
                return
        except Exception:
            pass
        _open_dialogs.pop(mode, None)

    palette, _dark = _addcard._resolve_palette()
    accent = _config().get("accent", "#6c8cff")

    dlg = QDialog(mw)
    dlg.setWindowTitle(text["title"])
    dlg.setObjectName("ba-bugreport")
    dlg.setProperty("ba_mode", mode)
    dlg.setStyleSheet(_dialog_qss(palette, accent))
    _open_dialogs[mode] = dlg
    dlg.finished.connect(lambda *_: _open_dialogs.pop(mode, None))

    root = QVBoxLayout(dlg)
    root.setContentsMargins(22, 20, 22, 18)
    root.setSpacing(10)

    title = QLabel(text["title"])
    title.setProperty("role", "title")
    root.addWidget(title)

    sub = QLabel(text["sub"])
    sub.setProperty("role", "sub")
    sub.setWordWrap(True)
    root.addWidget(sub)

    # A feature takes a few sentences; a bug is one line.
    if feature:
        note_edit = QPlainTextEdit()
        note_edit.setObjectName("ba-feature-note")
        note_edit.setPlaceholderText(text["placeholder"])
        note_edit.setTabChangesFocus(True)
        note_edit.setMinimumHeight(96)
        note_text = lambda: note_edit.toPlainText()
    else:
        note_edit = QLineEdit()
        note_edit.setPlaceholderText(text["placeholder"])
        note_text = lambda: note_edit.text()
    root.addWidget(note_edit)

    error_label = QLabel("")
    error_label.setProperty("role", "error")
    error_label.setWordWrap(True)
    error_label.hide()
    root.addWidget(error_label)

    expected_label = QLabel(text["expected_label"])
    expected_label.setProperty("role", "sub")
    root.addWidget(expected_label)
    expected_edit = QLineEdit()
    expected_edit.setPlaceholderText(text["expected_placeholder"])
    root.addWidget(expected_edit)

    screenshot_cb = QCheckBox("Include screenshot")
    screenshot_cb.setChecked(True)
    root.addWidget(screenshot_cb)

    row = QHBoxLayout()
    row.setSpacing(8)
    row.addStretch(1)
    cancel = QPushButton("Cancel")
    cancel.setProperty("role", "quiet")
    row.addWidget(cancel)
    save = QPushButton(text["save"])
    save.setProperty("role", "primary")
    save.setDefault(True)
    row.addWidget(save)
    root.addLayout(row)

    def on_save() -> None:
        # A multi-line description is kept verbatim: the id slug and the
        # index.md row already flatten it, and ankifix prompts from it whole.
        note = note_text().strip()
        if not note:
            error_label.setText(text["required"])
            error_label.show()
            note_edit.setFocus()
            return
        if not save.isEnabled():
            return
        expected = expected_edit.text().strip()
        include_shot = screenshot_cb.isChecked()
        save.setEnabled(False)
        save.setText("Saving…")

        def _done(ticket: Dict[str, Any]) -> None:
            dlg.accept()
            try:
                from aqt.utils import tooltip

                tooltip(text["saved"].format(id=ticket.get("id", "")))
            except Exception:
                pass

        create_ticket(note, expected, include_shot, on_done=_done, request=mode)

    save.clicked.connect(on_save)
    cancel.clicked.connect(dlg.reject)
    if feature:
        # Return inserts a newline in the description; Cmd+Return submits.
        for seq in ("Ctrl+Return", "Ctrl+Enter"):
            sc = QShortcut(QKeySequence(seq), dlg)
            sc.activated.connect(on_save)
        expected_edit.returnPressed.connect(on_save)
    else:
        note_edit.returnPressed.connect(on_save)

    dlg.setWindowModality(Qt.WindowModality.ApplicationModal)
    dlg.resize(460, 380 if feature else 300)
    dlg.show()
    note_edit.setFocus()
