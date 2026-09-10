"""Anki Design — the Browse sidebar selects several decks without a mode.

Anki ships the sidebar's multi-select as a *tool*: a two-button toolbar
above the tree flips it between "Search" (single selection, click to
search) and "Select" (ExtendedSelection + InternalMove, ⌘/⇧-click to pick
several, drag to move them, context menu acts on the lot).

Reported as: "the select feature is unnecessary, just allow me to drag and
select multiple decks, drag or right-click them into wherever I want."

That's the right reading. The tool is a mode you have to know exists,
enter, and leave, guarding behaviour that costs nothing to leave switched
on — and the mode's own button was already the subject of a bug report
("the marquee button does nothing") because a checked QToolButton under
the cursor that put it there is invisible. So the toolbar goes, the tree
sits permanently in the Select tool's mode, and the one thing the Search
tool did that Select didn't — a plain click searches the row — is put
back by hand, since Anki gates it on `tool == SEARCH`.

What that leaves:

  - plain click        — search this deck (as before)
  - ⌘-click / ⇧-click  — add to / extend the selection, no search
  - drag from a row    — move every selected deck (Anki's own drop handler
                         already reparents `_selected_items()` in one op)
  - drag from empty    — rubber-band lasso across rows
  - right-click        — the menu acts on the whole selection

The rubber band only *starts* from empty space (below the last row). A
QTreeView row spans the full viewport width, so any press on a row is the
start of an item drag; a band that could also begin there would have to
guess which one the user meant, and guessing wrong either eats the drag
or lassos when they meant to move. Empty space is unambiguous, and in a
deck tree there is always some under the last deck.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from aqt import mw
from aqt.qt import (
    QEvent,
    QItemSelection,
    QItemSelectionModel,
    QMenu,
    QModelIndex,
    QObject,
    QPoint,
    QRect,
    QRubberBand,
    QSize,
    Qt,
)

ADDON = __name__.split(".")[0]

# Below this the press reads as a click, not a lasso — a few stray pixels
# between press and release is a hand, not an intention.
_DRAG_SLOP = 4


def _config() -> Dict[str, Any]:
    return mw.addonManager.getConfig(ADDON) or {}


def enabled() -> bool:
    return bool(_config().get("browse_sidebar_multiselect", True))


# --------------------------------------------------------------------------- #
# Deck helpers
# --------------------------------------------------------------------------- #
def _selected_deck_ids(sidebar: Any) -> List[int]:
    """Deck ids in the sidebar's current selection, in tree order."""
    try:
        from aqt.browser.sidebar.item import SidebarItemType

        out: List[int] = []
        for item in sidebar._selected_items():
            if item.item_type is SidebarItemType.DECK and int(item.id):
                did = int(item.id)
                if did not in out:
                    out.append(did)
        return out
    except Exception:
        return []


def _deck_name(did: int) -> str:
    try:
        return mw.col.decks.name(did) or ""
    except Exception:
        return ""


def _common_parent(deck_ids: List[int]) -> int:
    """The deepest deck every one of these is inside, or 0 for top level.

    "Medicine::Pathology" and "Medicine::Anatomy" share "Medicine"; add
    "Science::Chemistry" and they share nothing, which is the top level.
    """
    if not deck_ids:
        return 0
    paths = [_deck_name(d).split("::") for d in deck_ids]
    if not all(paths):
        return 0
    shared: List[str] = []
    for parts in zip(*[p[:-1] for p in paths]):
        if len(set(parts)) != 1:
            break
        shared.append(parts[0])
    if not shared:
        return 0
    try:
        return int(mw.col.decks.id_for_name("::".join(shared)) or 0)
    except Exception:
        return 0


def _move_targets(deck_ids: List[int]) -> List[tuple]:
    """(did, name) for every deck these could move into.

    A deck can't move inside itself or inside its own subtree, and moving
    it to where it already is would be a no-op, so those are left out
    rather than offered and then silently ignored.
    """
    moving = {int(d) for d in deck_ids}
    prefixes = tuple(f"{_deck_name(d)}::" for d in deck_ids if _deck_name(d))
    current_parents = {_common_parent([d]) for d in deck_ids}
    out: List[tuple] = []
    try:
        for row in mw.col.decks.all_names_and_ids(skip_empty_default=False):
            did, name = int(row.id), row.name
            if did in moving or not name:
                continue
            if any(name == p[:-2] or name.startswith(p) for p in prefixes):
                continue
            try:
                if mw.col.decks.is_filtered(did):
                    continue
            except Exception:
                pass
            out.append((did, name))
    except Exception:
        return []
    out.sort(key=lambda r: r[1].lower())
    # A single deck already sitting under the only candidate parent gains
    # nothing from seeing it listed.
    if len(current_parents) == 1:
        only = next(iter(current_parents))
        out = [r for r in out if r[0] != only]
    return out


def _reparent(browser: Any, deck_ids: List[int], new_parent: int) -> None:
    """One `reparent_decks` for the whole selection — one op, one undo."""
    try:
        from anki.decks import DeckId
        from aqt.operations.deck import reparent_decks

        reparent_decks(
            parent=browser,
            deck_ids=[DeckId(d) for d in deck_ids],
            new_parent=DeckId(new_parent),
        ).run_in_background()
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# Mouse handling on the tree's viewport
# --------------------------------------------------------------------------- #
class _SidebarMouse(QObject):
    """Click-to-search and empty-space rubber-band, as a viewport filter.

    An event filter rather than overridden virtuals: the tree is Anki's
    class and other add-ons subclass or wrap it, and a filter composes
    with all of that instead of racing it.
    """

    def __init__(self, sidebar: Any) -> None:
        super().__init__(sidebar)
        self.sidebar = sidebar
        self.band: Optional[QRubberBand] = None
        self.origin: Optional[QPoint] = None
        self.press_pos: Optional[QPoint] = None

    # -- helpers ----------------------------------------------------------
    def _pos(self, ev: Any) -> QPoint:
        try:
            return ev.position().toPoint()
        except Exception:  # Qt5 shape, kept for safety
            return ev.pos()

    def _extending(self, ev: Any) -> bool:
        mods = ev.modifiers()
        return bool(
            mods
            & (
                Qt.KeyboardModifier.ControlModifier
                | Qt.KeyboardModifier.MetaModifier
                | Qt.KeyboardModifier.ShiftModifier
            )
        )

    def _rows_touching(self, rect: QRect) -> List[QModelIndex]:
        """Every visible row the band's rectangle crosses.

        Walks with `indexBelow` from the topmost visible row, which
        follows what the user can actually see — collapsed subtrees are
        skipped, exactly as they are for the eye.
        """
        view = self.sidebar
        out: List[QModelIndex] = []
        idx = view.indexAt(QPoint(2, 2))
        if not idx.isValid():
            return out
        height = view.viewport().height()
        guard = 0
        while idx.isValid() and guard < 5000:
            guard += 1
            vr = view.visualRect(idx)
            if vr.isValid() and rect.intersects(vr):
                out.append(idx)
            if vr.top() > height:
                break
            idx = view.indexBelow(idx)
        return out

    def _apply_band(self, rect: QRect) -> None:
        rows = self._rows_touching(rect)
        sel = QItemSelection()
        for idx in rows:
            sel.select(idx, idx)
        flags = (
            QItemSelectionModel.SelectionFlag.Select
            | QItemSelectionModel.SelectionFlag.Rows
        )
        self.sidebar.selectionModel().select(sel, flags)

    def _end_band(self) -> None:
        if self.band is not None:
            self.band.hide()
        self.origin = None

    # -- filter -----------------------------------------------------------
    def eventFilter(self, obj: Any, ev: Any) -> bool:  # noqa: N802 — Qt
        try:
            etype = ev.type()
            if etype == QEvent.Type.MouseButtonPress:
                return self._on_press(ev)
            if etype == QEvent.Type.MouseMove:
                return self._on_move(ev)
            if etype == QEvent.Type.MouseButtonRelease:
                return self._on_release(ev)
        except Exception:
            self._end_band()
        return False

    def _on_press(self, ev: Any) -> bool:
        if ev.button() != Qt.MouseButton.LeftButton:
            return False
        pos = self._pos(ev)
        self.press_pos = QPoint(pos)
        if self.sidebar.indexAt(pos).isValid():
            # On a row: leave it to Qt, so item drag and ⌘/⇧ extension
            # keep working untouched.
            return False
        if not self._extending(ev):
            self.sidebar.clearSelection()
        if self.band is None:
            self.band = QRubberBand(
                QRubberBand.Shape.Rectangle, self.sidebar.viewport()
            )
        self.origin = QPoint(pos)
        self.band.setGeometry(QRect(self.origin, QSize()))
        self.band.show()
        return True

    def _on_move(self, ev: Any) -> bool:
        if self.origin is None or self.band is None:
            return False
        rect = QRect(self.origin, self._pos(ev)).normalized()
        self.band.setGeometry(rect)
        self._apply_band(rect)
        return True

    def _on_release(self, ev: Any) -> bool:
        if ev.button() != Qt.MouseButton.LeftButton:
            return False
        if self.origin is not None:
            self._end_band()
            self.press_pos = None
            return True
        # Not a band — this may be the click that should run a search.
        pos = self._pos(ev)
        press, self.press_pos = self.press_pos, None
        if self._extending(ev):
            return False
        if press is not None and (
            abs(press.x() - pos.x()) > _DRAG_SLOP
            or abs(press.y() - pos.y()) > _DRAG_SLOP
        ):
            # A drag, not a click. Anki's drop handler has already dealt
            # with it; searching now would fight the move.
            return False
        index = self.sidebar.indexAt(pos)
        if index.isValid() and index == self.sidebar.currentIndex():
            # Qt has finished collapsing the selection onto this row by
            # now, so this searches exactly what is highlighted.
            self.sidebar._on_search(index)
        return False


# --------------------------------------------------------------------------- #
# Install
# --------------------------------------------------------------------------- #
def install(browser: Any) -> None:
    """Make the Browse sidebar modelessly multi-selectable."""
    if not enabled():
        return
    sidebar = getattr(browser, "sidebar", None)
    if sidebar is None:
        return
    if getattr(sidebar, "_ba_multiselect", None) is not None:
        return
    try:
        from aqt.browser.sidebar.toolbar import SidebarTool

        # The mode's behaviour, permanently: ExtendedSelection +
        # InternalMove. Going through the property (rather than calling
        # setSelectionMode ourselves) keeps us honest if Anki ever adds
        # something else to the setter.
        sidebar.tool = SidebarTool.SELECT
    except Exception:
        return
    try:
        # And the mode's *switch* goes away. Hidden rather than deleted:
        # `SidebarTreeView.cleanup` calls `toolbar.cleanup()` to unhook a
        # theme handler, and a deleted toolbar would take that down with
        # it on every Browse close.
        toolbar = getattr(sidebar, "toolbar", None)
        if toolbar is not None:
            toolbar.setVisible(False)
            toolbar.setFixedHeight(0)
    except Exception:
        pass
    try:
        watcher = _SidebarMouse(sidebar)
        sidebar.viewport().installEventFilter(watcher)
        # Park it on the tree: an event filter Qt doesn't own is only
        # alive as long as something Python-side holds it.
        sidebar._ba_multiselect = watcher
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# Context menu — act on the whole selection
# --------------------------------------------------------------------------- #
def on_sidebar_context_menu(sidebar: Any, menu: QMenu, item: Any, index: Any) -> None:
    """Add "Move to…" for the selected decks.

    Anki's own menu already acts on `_selected_items()` where it makes
    sense (tags on selected notes, delete, the search actions). What it
    has no entry for at all is moving decks — that was drag-only, which
    is fine until the deck you want is scrolled off the top of the tree.
    """
    if not enabled():
        return
    try:
        from aqt.browser.sidebar.item import SidebarItemType

        if item is None or item.item_type is not SidebarItemType.DECK:
            return
        deck_ids = _selected_deck_ids(sidebar)
        if not deck_ids:
            return
        targets = _move_targets(deck_ids)
        if not targets:
            return
        browser = getattr(sidebar, "browser", None) or mw

        if len(deck_ids) == 1:
            label = "Move to"
        else:
            label = f"Move {len(deck_ids)} decks to"

        menu.addSeparator()
        sub = menu.addMenu(label)

        def _add(did: int, text: str) -> None:
            sub.addAction(
                text.replace("&", "&&"),
                lambda _checked=False, d=did: _reparent(browser, deck_ids, d),
            )

        _add(0, "Top level")
        sub.addSeparator()
        for did, name in targets:
            _add(did, name)
    except Exception:
        pass
