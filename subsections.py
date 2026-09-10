"""Anki Design — group a course's decks under a subsection.

"Easily add a subsection within each course on browse and decks, for
example weeks 1&2&3 of microbiology are the Exam 1 Content."

A course accumulates decks in the order you make them — Week 1, Week 2,
Week 3, … — and the shape that matters afterwards is a different one: the
weeks that Exam 1 covers, the weeks that Exam 2 covers. Anki can already
express that, because deck names are paths and `::` is a real level of
hierarchy. What it can't do is *rearrange* into one: you'd rename each
week by hand, retyping the whole path each time, and a typo in the middle
segment silently forks a second subsection.

So: pick a name, tick the decks that belong in it, done.

Mechanism
---------
Renaming, which is all a subsection is:

    Microbiology::Week 1  →  Microbiology::Exam 1 Content::Week 1

The deck id is *not* touched by a rename — rslib's `rename_deck` /
`reparent_decks` update the name column of the same row — so everything
keyed by deck id survives: this add-on's deadlines (`deck_deadlines` is
keyed by did), the deck's preset, its per-day limits, its collapse state,
and every card in it. Nothing is created, moved or rescheduled.

Two ops rather than one, and deliberately: `add_deck` for the subsection
itself, then `reparent_decks` for the decks going into it — Anki's own
operations, which means correct merging behaviour when a name collides,
and an undo entry the user recognises. A subsection with nothing ticked
is just the first op, which is a legitimate thing to want (make the
heading now, fill it later).
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from aqt import mw


ADDON = __name__.split(".")[0]


def _config() -> Dict[str, Any]:
    try:
        return mw.addonManager.getConfig(ADDON) or {}
    except Exception:
        return {}


def enabled() -> bool:
    return bool(_config().get("deck_subsections", True))


# --------------------------------------------------------------------------- #
# Deck queries
# --------------------------------------------------------------------------- #
def deck_name(did: int) -> str:
    try:
        return str(mw.col.decks.name(did) or "")
    except Exception:
        return ""


def is_filtered(did: int) -> bool:
    try:
        deck = mw.col.decks.get(did)
        return bool(deck and deck.get("dyn"))
    except Exception:
        return False


def child_decks(did: int) -> List[Tuple[int, str]]:
    """`(id, leaf name)` for the deck's *direct* children, in Anki's order.

    Direct children only. A subsection groups the things you see listed
    under the course, and offering "Week 1" and "Week 1::Lecture" as two
    independent tick boxes would let you move a deck and its own child
    apart — which the reparent then quietly undoes anyway, because moving
    a deck takes its subtree with it.
    """
    parent = deck_name(did)
    if not parent:
        return []
    prefix = parent + "::"
    out: List[Tuple[int, str]] = []
    try:
        for entry in mw.col.decks.all_names_and_ids():
            name = entry.name
            if not name.startswith(prefix):
                continue
            tail = name[len(prefix):]
            if "::" in tail:
                continue  # a grandchild — it travels with its parent
            out.append((int(entry.id), tail))
    except Exception:
        return []
    out.sort(key=lambda row: row[1].lower())
    return out


def _clean_name(raw: str) -> str:
    """A subsection is one level, so `::` in the typed name is a mistake.

    Turning it into a space rather than rejecting it keeps the dialog from
    lecturing anyone: "Exam 1::Content" plainly means one heading.
    """
    name = (raw or "").replace("::", " ").strip()
    return " ".join(name.split())


# --------------------------------------------------------------------------- #
# The operation
# --------------------------------------------------------------------------- #
def create(did: int, raw_name: str, move_ids: List[int],
           on_done: Optional[Any] = None) -> None:
    """Create `<deck>::<raw_name>` and move `move_ids` into it."""
    name = _clean_name(raw_name)
    parent = deck_name(did)
    if not name or not parent:
        return
    full = f"{parent}::{name}"
    try:
        from anki.decks import DeckId
        from aqt.operations.deck import add_deck, reparent_decks
    except Exception:
        return

    # Never move the subsection into itself: if a child deck already has
    # this name, the tick box for it is the same deck we're about to
    # create, and reparenting it under itself is not a thing.
    targets = [int(x) for x in move_ids if int(x) != did]

    def finish() -> None:
        _tooltip(
            f"“{name}” created"
            + (f" — {len(targets)} deck{'s' if len(targets) != 1 else ''} moved in"
               if targets else "")
        )
        _refresh()
        if on_done:
            try:
                on_done()
            except Exception:
                pass

    def on_added(out: Any) -> None:
        sub_id = int(getattr(out, "id", 0) or 0)
        if not sub_id or not targets:
            finish()
            return
        reparent_decks(
            parent=mw,
            deck_ids=[DeckId(x) for x in targets if x != sub_id],
            new_parent=DeckId(sub_id),
        ).success(lambda _o: finish()).run_in_background()

    add_deck(parent=mw, name=full).success(on_added).run_in_background()


def _refresh() -> None:
    try:
        if getattr(mw, "state", "") == "deckBrowser":
            mw.deckBrowser.refresh()
    except Exception:
        pass
    # The Browser's sidebar tree caches its deck rows; tell any open one to
    # rebuild so a subsection made from Browse appears in Browse.
    try:
        import aqt

        browser = aqt.dialogs._dialogs["Browser"][1]  # type: ignore[attr-defined]
        if browser is not None:
            browser.sidebar.refresh()
    except Exception:
        pass


def _tooltip(msg: str) -> None:
    try:
        from aqt.utils import tooltip

        tooltip(msg, parent=mw, period=4000)
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# Browser sidebar context menu
# --------------------------------------------------------------------------- #
def on_browser_sidebar_context_menu(sidebar: Any, menu: Any, item: Any,
                                    index: Any) -> None:
    """Put "New subsection…" on a deck's right-click menu in Browse too.

    The Browse sidebar *is* the deck tree for anyone who lives in the
    Browser, and reorganising a course is exactly the sort of thing you
    reach for while looking at its cards. Anki's own menu is left intact;
    this is one more entry on the end.
    """
    if not enabled():
        return
    try:
        from aqt.browser.sidebar.item import SidebarItemType

        if item is None or item.item_type is not SidebarItemType.DECK:
            return
        did = int(item.id)
        if not did or is_filtered(did):
            return
        menu.addSeparator()
        menu.addAction(
            "New subsection…",
            lambda: show_dialog(did, parent_widget=sidebar),
        )
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# Dialog
# --------------------------------------------------------------------------- #
def show_dialog(did: int, parent_widget: Any = None) -> None:
    """Name a subsection and tick the decks that go in it."""
    if not enabled():
        return
    from aqt.qt import (
        QCheckBox,
        QDialog,
        QHBoxLayout,
        QLabel,
        QLineEdit,
        QPushButton,
        QScrollArea,
        QVBoxLayout,
        Qt,
        QWidget,
    )

    from . import addcard as _addcard

    course = deck_name(did)
    if not course:
        return
    if is_filtered(did):
        _tooltip("A filtered deck can't hold subsections")
        return
    palette, _dark = _addcard._resolve_palette()
    accent = _config().get("accent", "#6c8cff")
    kids = child_decks(did)

    dlg = QDialog(parent_widget or mw)
    dlg.setWindowTitle("New subsection")
    dlg.setObjectName("ba-subsection")
    dlg.setStyleSheet(_dialog_qss(palette, accent))
    dlg.setMinimumWidth(430)

    root = QVBoxLayout(dlg)
    root.setContentsMargins(22, 20, 22, 18)
    root.setSpacing(12)

    title = QLabel(course.split("::")[-1])
    title.setProperty("role", "title")
    root.addWidget(title)

    sub = QLabel("Gather some of this deck's sub-decks under a new heading.")
    sub.setProperty("role", "sub")
    sub.setWordWrap(True)
    root.addWidget(sub)

    field = QLineEdit()
    field.setPlaceholderText("Exam 1 Content")
    root.addWidget(field)

    boxes: List[Tuple[QCheckBox, int]] = []
    if kids:
        pick = QLabel("Move into it:")
        pick.setProperty("role", "sub")
        root.addWidget(pick)

        holder = QWidget()
        col = QVBoxLayout(holder)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(2)
        for kid_id, leaf in kids:
            # QCheckBox reads "&" as a mnemonic, so "Pathology & Clinical"
            # renders as "Pathology  Clinical" with an underlined C.
            box = QCheckBox(leaf.replace("&", "&&"))
            col.addWidget(box)
            boxes.append((box, kid_id))
        col.addStretch(1)

        scroll = QScrollArea()
        scroll.setWidget(holder)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        # Tall enough for six rows; past that it scrolls rather than
        # growing the dialog off the bottom of a laptop screen.
        scroll.setMaximumHeight(190)
        root.addWidget(scroll)
    else:
        empty = QLabel(
            "This deck has no sub-decks yet, so there's nothing to move in — "
            "the subsection will be created empty."
        )
        empty.setProperty("role", "note")
        empty.setWordWrap(True)
        root.addWidget(empty)

    note = QLabel(
        "Moving a deck renames it — “Week 1” becomes “"
        + course.split("::")[-1]
        + "::<name>::Week 1”. Its cards, scheduling, preset and deadline are "
        "untouched; the deck itself is the same deck."
    )
    note.setProperty("role", "note")
    note.setWordWrap(True)
    root.addWidget(note)

    row = QHBoxLayout()
    row.setSpacing(8)
    row.addStretch(1)
    cancel = QPushButton("Cancel")
    cancel.setProperty("role", "quiet")
    row.addWidget(cancel)
    create_btn = QPushButton("Create subsection")
    create_btn.setProperty("role", "primary")
    create_btn.setDefault(True)
    row.addWidget(create_btn)
    root.addLayout(row)

    def refresh_note() -> None:
        typed = _clean_name(field.text())
        create_btn.setEnabled(bool(typed))
        shown = typed or "<name>"
        leaf = kids[0][1] if kids else "Week 1"
        note.setText(
            f"Moving a deck renames it — “{leaf}” becomes "
            f"“{course}::{shown}::{leaf}”. Its cards, scheduling, preset and "
            "deadline are untouched; the deck itself is the same deck."
        )

    def on_create() -> None:
        typed = _clean_name(field.text())
        if not typed:
            return
        picked = [kid_id for box, kid_id in boxes if box.isChecked()]
        dlg.accept()
        create(did, typed, picked)

    field.textChanged.connect(lambda _t: refresh_note())
    field.returnPressed.connect(on_create)
    cancel.clicked.connect(dlg.reject)
    create_btn.clicked.connect(on_create)
    refresh_note()

    dlg.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
    field.setFocus()
    dlg.show()
    # Parked on the module so a dialog opened from the embedded Browser —
    # whose own window is hidden — isn't garbage-collected the moment this
    # function returns.
    globals()["_open_dialog"] = dlg


def _dialog_qss(p: Dict[str, str], accent: str) -> str:
    from . import addcard as _addcard

    return f"""
QDialog#ba-subsection {{ background: {p['paper']}; }}
QDialog#ba-subsection QWidget {{ background: transparent; }}
QDialog#ba-subsection QLabel {{
    color: {p['ink']};
    font-family: {_addcard.SANS};
    font-size: 11pt;
    background: transparent;
}}
QDialog#ba-subsection QLabel[role="title"] {{
    font-family: {_addcard.SERIF};
    font-size: 19pt;
    font-weight: 500;
}}
QDialog#ba-subsection QLabel[role="sub"] {{ color: {p['ink_dim']}; }}
QDialog#ba-subsection QLabel[role="note"] {{
    color: {p['ink_dim']};
    font-size: 10pt;
    padding: 2px 0;
}}
QDialog#ba-subsection QLineEdit {{
    background: {p['field_bg']};
    color: {p['ink']};
    border: 1px solid {p['line']};
    border-radius: 8px;
    padding: 8px 12px;
    font-family: {_addcard.SANS};
    font-size: 11pt;
    selection-background-color: {accent};
    selection-color: {p['paper']};
}}
QDialog#ba-subsection QLineEdit:focus {{ border-color: {accent}; }}
QDialog#ba-subsection QCheckBox {{
    color: {p['ink']};
    font-family: {_addcard.SANS};
    font-size: 11pt;
    padding: 4px 2px;
    spacing: 9px;
}}
QDialog#ba-subsection QCheckBox::indicator {{
    width: 15px;
    height: 15px;
    border: 1px solid {p['line2']};
    border-radius: 4px;
    background: {p['field_bg']};
}}
QDialog#ba-subsection QCheckBox::indicator:checked {{
    background: {accent};
    border-color: {accent};
}}
QDialog#ba-subsection QScrollArea {{ background: transparent; border: 0; }}
QDialog#ba-subsection QPushButton[role="primary"] {{
    background: {p['ink']};
    color: {p['paper']};
    border: 1px solid {p['ink']};
    border-radius: 6px;
    padding: 7px 16px;
    font-family: {_addcard.SANS};
    font-size: 10.5pt;
    font-weight: 500;
}}
QDialog#ba-subsection QPushButton[role="primary"]:hover {{
    background: {p['ink_dim']}; border-color: {p['ink_dim']};
}}
QDialog#ba-subsection QPushButton[role="primary"]:disabled {{
    background: {p['line']}; border-color: {p['line']}; color: {p['ink_faint']};
}}
QDialog#ba-subsection QPushButton[role="quiet"] {{
    background: transparent;
    color: {p['ink_dim']};
    border: 1px solid {p['line']};
    border-radius: 6px;
    padding: 7px 14px;
    font-family: {_addcard.SANS};
    font-size: 10.5pt;
}}
QDialog#ba-subsection QPushButton[role="quiet"]:hover {{
    color: {p['ink']}; border-color: {p['line2']};
}}
{_scrollbar_qss(p)}
"""


def _scrollbar_qss(p: Dict[str, str]) -> str:
    return f"""
QDialog#ba-subsection QScrollBar:vertical {{
    background: transparent; width: 10px; margin: 0;
}}
QDialog#ba-subsection QScrollBar::handle:vertical {{
    background: {p['line2']}; border-radius: 5px; min-height: 28px;
}}
QDialog#ba-subsection QScrollBar::add-line:vertical,
QDialog#ba-subsection QScrollBar::sub-line:vertical {{ height: 0; }}
QDialog#ba-subsection QScrollBar::add-page:vertical,
QDialog#ba-subsection QScrollBar::sub-page:vertical {{ background: transparent; }}
"""
