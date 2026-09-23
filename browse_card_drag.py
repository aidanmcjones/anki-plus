"""Anki Design: drag cards out of the Browse table.

Reported as: "When selecting one card or multiple, I should be able to
select one or a group and drag and drop it to another deck or a different
place in the deck."

Stock Anki's card table is not a drag source. Moving cards to a deck means
the "Change Deck" dialog (Ctrl+D) and a picker; repositioning new cards
means the "Reposition" dialog and a number. Both stay available. This
module adds the direct-manipulation route on top:

  - drag any selected row(s)      : every selected card travels along, in
                                    Notes mode every card of every note
  - pull an unselected row aside  : just that card travels; a sweep up or
                                    down the list from an unselected row
                                    is left to the table, which selects
                                    the rows the cursor crosses, so a group
                                    can be gathered by hand and then dragged
  - drop on a deck in the sidebar : `set_card_deck`, the same op the
                                    Change Deck dialog runs, one undo step
  - drop between rows of the table: an insertion line shows on the top or
                                    bottom edge of the row under the cursor
                                    (whichever half it is in), and on
                                    release `reposition_new_cards` puts
                                    every dragged new card there, in its
                                    queue order, just before or just after
                                    that row's card in the new-card queue
                                    (only new cards have a position, so the
                                    row has to be a new card too); then the
                                    table is re-searched sorted by Due so
                                    the rows visibly move

How it is wired. QAbstractItemView has its own drag machinery, but it is
driven by the model (`flags`, `mimeData`), and the table's model is Anki's
`DataModel`. Rather than reach into that class we watch the table's
viewport with an event filter: a press on a row arms a drag, and once the
cursor travels past Qt's drag distance we start a `QDrag` ourselves
carrying the selected card ids under a private MIME type. On an
already-selected row Qt defers the selection change until release, so the
whole selection travels and a plain click still collapses the selection to
that row; on an unselected row Qt selects it on the press, so that one
card travels when pulled sideways. Pulled along the list instead, the
press is handed back to the view: that gesture is how a run of rows is
selected by hand (20260923-104846), and a drag would swallow it.

The drop side is another pair of filters, on the sidebar tree and on the
table. They only react to our MIME type, so Anki's own sidebar drags (deck
into deck, tag into tag) pass through untouched.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from aqt import mw
from aqt.qt import (
    QApplication,
    QByteArray,
    QDrag,
    QEvent,
    QFrame,
    QMimeData,
    QObject,
    QPoint,
    QRect,
    Qt,
    QTimer,
)

ADDON = __name__.split(".")[0]

MIME = "application/x-anki-design-cards"

# Hover this long over a collapsed deck while dragging and it opens, so a
# card can be dropped into a sub-deck that is not showing. Matches the
# tree's own autoExpandDelay.
_EXPAND_DELAY_MS = 600


def _config() -> Dict[str, Any]:
    try:
        return mw.addonManager.getConfig(ADDON) or {}
    except Exception:
        return {}


def enabled() -> bool:
    return bool(_config().get("browse_card_drag", True))


# --------------------------------------------------------------------------- #
# MIME payload
# --------------------------------------------------------------------------- #
def encode_cards(card_ids: List[int]) -> "QMimeData":
    """The selection as MIME: our private type, ids as a JSON list."""
    mime = QMimeData()
    payload = json.dumps([int(c) for c in card_ids]).encode("utf-8")
    mime.setData(MIME, QByteArray(payload))
    return mime


def decode_cards(mime: Any) -> List[int]:
    """Card ids out of a drop, or [] if this is not one of our drags."""
    try:
        if mime is None or not mime.hasFormat(MIME):
            return []
        raw = mime.data(MIME)
        data = raw.data() if hasattr(raw, "data") else bytes(raw)
        if isinstance(data, memoryview):
            data = data.tobytes()
        ids = json.loads(bytes(data).decode("utf-8"))
        out: List[int] = []
        for c in ids:
            c = int(c)
            if c not in out:
                out.append(c)
        return out
    except Exception:
        return []


# --------------------------------------------------------------------------- #
# Event helpers
# --------------------------------------------------------------------------- #
def _pos(ev: Any) -> QPoint:
    try:
        return ev.position().toPoint()
    except Exception:  # Qt5 shape, kept for safety
        return ev.pos()


def _extending(ev: Any) -> bool:
    try:
        mods = ev.modifiers()
    except Exception:
        return False
    return bool(
        mods
        & (
            Qt.KeyboardModifier.ControlModifier
            | Qt.KeyboardModifier.MetaModifier
            | Qt.KeyboardModifier.ShiftModifier
        )
    )


def _drag_distance() -> int:
    try:
        return int(QApplication.startDragDistance())
    except Exception:
        return 10


def _drag_pixmap(count: int) -> Any:
    """A small badge that travels under the cursor: "3 cards"."""
    try:
        from aqt.qt import QColor, QFont, QPainter, QPixmap

        from . import addcard as _addcard

        palette, _dark = _addcard._resolve_palette()
        text = "1 card" if count == 1 else f"{count} cards"
        font = QFont()
        font.setPointSizeF(10.5)
        font.setWeight(QFont.Weight.Medium)
        pm = QPixmap(1, 1)
        painter = QPainter(pm)
        painter.setFont(font)
        w = painter.fontMetrics().horizontalAdvance(text) + 22
        painter.end()
        h = 26
        pm = QPixmap(w, h)
        pm.fill(QColor(0, 0, 0, 0))
        painter = QPainter(pm)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(palette["ink"]))
        painter.drawRoundedRect(0, 0, w, h, 7, 7)
        painter.setPen(QColor(palette["paper"]))
        painter.setFont(font)
        painter.drawText(0, 0, w, h, int(Qt.AlignmentFlag.AlignCenter), text)
        painter.end()
        return pm
    except Exception:
        return None


# --------------------------------------------------------------------------- #
# Drag source: the card table
# --------------------------------------------------------------------------- #
def selected_card_ids(browser: Any) -> List[int]:
    """Every selected card. In Notes mode: every card of every selected
    note, which is what `Table.get_selected_card_ids` already returns."""
    try:
        table = browser.table
        ids = table.get_selected_card_ids()
        return [int(c) for c in ids]
    except Exception:
        return []


class _TableDrag(QObject):
    """Turns a press-and-pull on a selected row into a QDrag.

    A viewport filter rather than `setDragEnabled` on the view: the view's
    own path asks the model for `mimeData`, and the model is Anki's. This
    way nothing about the model changes and the table keeps rubber-band
    selecting from unselected rows as it always has.
    """

    def __init__(self, browser: Any, view: Any) -> None:
        super().__init__(view)
        self.browser = browser
        self.view = view
        self.press_pos: Optional[QPoint] = None
        self.press_selected = False
        self.armed = False
        self.dragging = False

    def eventFilter(self, obj: Any, ev: Any) -> bool:  # noqa: N802 (Qt)
        try:
            etype = ev.type()
            if etype == QEvent.Type.MouseButtonPress:
                return self._on_press(ev)
            if etype == QEvent.Type.MouseMove:
                return self._on_move(ev)
            if etype == QEvent.Type.MouseButtonRelease:
                return self._on_release(ev)
        except Exception:
            self._disarm()
        return False

    def _disarm(self) -> None:
        self.armed = False
        self.press_pos = None
        self.press_selected = False

    def _on_press(self, ev: Any) -> bool:
        self._disarm()
        if ev.button() != Qt.MouseButton.LeftButton or _extending(ev):
            return False
        pos = _pos(ev)
        index = self.view.indexAt(pos)
        if not index.isValid():
            return False
        sel = self.view.selectionModel()
        if sel is None:
            return False
        # Armed on ANY row. A selected row: with ExtendedSelection Qt
        # defers the selection change to release, so the whole selection
        # travels. An unselected row: Qt selects it on this press, and a
        # sideways pull drags that one card, the way Finder and a
        # drag-enabled Qt view behave. (This used to be left to the view,
        # which read the pull as a rubber-band selection, so "grab a card
        # and drag it" in one motion never moved anything.) Which row it
        # was is remembered: a pull along the list from an unselected row
        # is the table's own gesture for selecting a run of rows, and
        # `_on_move` gives that one back. Shift/Cmd-click still extend.
        self.press_pos = QPoint(pos)
        try:
            self.press_selected = bool(sel.isSelected(index))
        except Exception:
            self.press_selected = False
        self.armed = True
        return False

    def _on_move(self, ev: Any) -> bool:
        if not self.armed or self.press_pos is None:
            return False
        try:
            if not (ev.buttons() & Qt.MouseButton.LeftButton):
                self._disarm()
                return False
        except Exception:
            pass
        pos = _pos(ev)
        dx = abs(pos.x() - self.press_pos.x())
        dy = abs(pos.y() - self.press_pos.y())
        if dx + dy < _drag_distance():
            # Below the threshold. Swallowed: the view would otherwise read
            # the wobble as the start of a rubber-band and collapse the
            # selection to the rows under it.
            return True
        if not self.press_selected and dy >= dx:
            # A sweep up or down the list from a row that was not selected:
            # that is how a group of cards is gathered by hand, and a drag
            # here would take the gesture and select nothing
            # (20260923-104846). Hand it to the view, which extends the
            # selection row by row from the press as the cursor travels,
            # as Anki's table always has. The group can then be dragged by
            # pressing on any of its rows. A sideways pull still drags the
            # one card: the sidebar is beside the table, and no run of rows
            # is selected by moving along one.
            self._disarm()
            return False
        self._disarm()
        self._start_drag()
        return True

    def _on_release(self, ev: Any) -> bool:
        # A click after all: the view collapses the selection to this row,
        # as it always has.
        self._disarm()
        return False

    def _start_drag(self) -> None:
        card_ids = selected_card_ids(self.browser)
        if not card_ids:
            return
        drag = QDrag(self.view)
        drag.setMimeData(encode_cards(card_ids))
        pm = _drag_pixmap(len(card_ids))
        if pm is not None:
            try:
                drag.setPixmap(pm)
                drag.setHotSpot(QPoint(-12, 14))
            except Exception:
                pass
        self.dragging = True
        try:
            drag.exec(Qt.DropAction.MoveAction)
        finally:
            self.dragging = False


# --------------------------------------------------------------------------- #
# Drop targets
# --------------------------------------------------------------------------- #
def _deck_is_filtered(did: int) -> bool:
    try:
        return bool(mw.col.decks.is_filtered(did))
    except Exception:
        return False


def deck_at(sidebar: Any, pos: QPoint) -> Optional[int]:
    """The deck id under `pos` in the sidebar tree, if cards may go there.

    Deck rows and the "Current Deck" row qualify; filtered decks do not
    (cards can't be moved into one by hand, the op refuses). Everything
    else in the tree (tags, flags, headings) is not a place a card lives.
    """
    try:
        from aqt.browser.sidebar.item import SidebarItemType

        index = sidebar.indexAt(pos)
        if not index.isValid():
            return None
        item = sidebar.model().item_for_index(index)
        if item is None:
            return None
        did = 0
        if item.item_type is SidebarItemType.DECK:
            did = int(item.id or 0)
        elif item.item_type is SidebarItemType.DECK_CURRENT:
            did = int(item.id or 0) or int(mw.col.decks.get_current_id())
        if did <= 0 or _deck_is_filtered(did):
            return None
        return did
    except Exception:
        return None


def move_to_deck(browser: Any, card_ids: List[int], did: int) -> bool:
    """One `set_card_deck` for the whole drop: one op, one undo."""
    if not card_ids or not did:
        return False
    try:
        from anki.cards import CardId
        from anki.decks import DeckId
        from aqt.operations.card import set_card_deck

        op = set_card_deck(
            parent=browser,
            card_ids=[CardId(c) for c in card_ids],
            deck_id=DeckId(did),
        )
        n = len(card_ids)
        try:
            name = mw.col.decks.name(did)
        except Exception:
            name = "deck"
        op = op.success(
            lambda _out, n=n, name=name: _on_moved(browser, n, name)
        )
        op.run_in_background()
        return True
    except Exception:
        return False


def _on_moved(browser: Any, n: int, name: str) -> None:
    """After a sidebar drop lands: say so, and re-run the search.

    `Table.op_executed` only repaints cells over the row snapshot of the
    last search, so cards moved out of the deck being browsed stayed on
    screen, looking exactly as before the drop (the embedded Browse is
    never `current_window()`, so it did not even repaint). Re-searching is
    what makes them leave a deck-filtered list."""
    _tooltip(browser, ("1 card" if n == 1 else f"{n} cards") + f" moved to {name}")
    try:
        browser.search()
    except Exception:
        pass


def card_at(browser: Any, pos: QPoint) -> Optional[tuple]:
    """(card_id, due, is_new) for the table row under `pos`, or None."""
    try:
        from anki.consts import CARD_TYPE_NEW

        view = browser.table._view
        index = view.indexAt(pos)
        if not index.isValid():
            return None
        card = browser.table._model.get_card(index)
        if card is None:
            return None
        return (int(card.id), int(card.due), int(card.type) == int(CARD_TYPE_NEW))
    except Exception:
        return None


NOT_NEW_TARGET = (
    "Only new cards have a place in the queue to move to. Review cards are "
    "ordered by due date. To move cards to another deck, drop them on the "
    "deck in the sidebar."
)


def new_card_at(browser: Any, pos: QPoint) -> Optional[tuple]:
    """(card_id, due) for the table row under `pos` if that card is new.

    Only new cards have a queue position, so only a new card's row is a
    place another card can be put before.
    """
    try:
        from anki.consts import CARD_TYPE_NEW

        view = browser.table._view
        index = view.indexAt(pos)
        if not index.isValid():
            return None
        card = browser.table._model.get_card(index)
        if card is None or int(card.type) != int(CARD_TYPE_NEW):
            return None
        return (int(card.id), int(card.due))
    except Exception:
        return None


# The Browse column that shows a new card's position. Same key in Cards
# and Notes mode (rslib's Column::Due).
DUE_COLUMN = "cardDue"


def _tooltip(browser: Any, text: str) -> None:
    try:
        from aqt.utils import tooltip

        tooltip(text, parent=browser)
    except Exception:
        pass


def new_cards_only(card_ids: List[int]) -> List[int]:
    """The dragged cards that have a position: type new, in queue order.

    The op would skip the others anyway, but it would still shift the
    queue to make room for them and report a change, so they are dropped
    here, before it runs. The rest are ordered by their current position:
    the op hands out positions in the order it is given the ids, and the
    selection model lists rows in the order they were selected, so without
    this a group could land in the order the user clicked its rows."""
    try:
        from anki.consts import CARD_TYPE_NEW
    except Exception:
        CARD_TYPE_NEW = 0
    out: List[tuple] = []
    for c in card_ids:
        try:
            card = mw.col.get_card(int(c))
            if int(card.type) == int(CARD_TYPE_NEW):
                out.append((int(card.due), int(c)))
        except Exception:
            continue
    out.sort()
    return [c for _due, c in out]


def sort_state(browser: Any) -> tuple:
    """(sort column key, descending?) of the table, or (None, False)."""
    try:
        state = browser.table._state
        return str(state.sort_column or ""), bool(state.sort_backwards)
    except Exception:
        return None, False


def _sort_by_due(browser: Any) -> None:
    """Order the table by position, ascending. Through the state's setters,
    which persist it in the collection like a click on the header does."""
    state = browser.table._state
    state.sort_column = DUE_COLUMN
    state.sort_backwards = False
    try:
        browser.table._set_sort_indicator()
    except Exception:
        pass


def refresh_order(browser: Any) -> None:
    """Make the table show the queue order the drop just changed.

    `Table.op_executed` only invalidates cached cell text and repaints; the
    rows themselves are a snapshot of the last search, so a reposition
    updates the Due column and moves nothing. Re-running the search is
    what re-sorts. And the new order is only visible when the table is
    sorted by Due, so a table sorted by anything else is switched to Due
    first: the drop was a request to see cards in that order.
    """
    column, _ = sort_state(browser)
    if column != DUE_COLUMN:
        try:
            _sort_by_due(browser)
        except Exception:
            pass
    try:
        browser.search()
    except Exception:
        pass


def _on_repositioned(browser: Any, out: Any, skipped: int) -> None:
    count = getattr(out, "count", None)
    try:
        count = int(count)
    except Exception:
        count = 0
    if count > 0:
        text = ("1 card" if count == 1 else f"{count} cards") + " repositioned"
        if skipped:
            text += (
                ", 1 card that isn't new left in place"
                if skipped == 1
                else f", {skipped} cards that aren't new left in place"
            )
        _tooltip(browser, text)
    refresh_order(browser)


def reposition_at(
    browser: Any, card_ids: List[int], target: tuple, below: bool = False
) -> bool:
    """Put the dragged new cards at the insertion line: just before
    `target` in the new queue, or just after it when `below`.

    `reposition_new_cards` with `shift_existing` bumps every new card at or
    after the start position along by the number moved, so whatever sat
    there slides back and the dragged cards take its place, in their own
    queue order. That holds when the target is itself one of the dragged
    cards: it is shifted with the rest and then placed with its group, so
    the group gathers at the line in order and nothing is left behind.
    (The target used to be dropped from the group, so the others landed
    in front of it and the group came out in a different order.)

    Only new cards have a position, so cards that aren't new are filtered
    out first; a drag of nothing but those is refused with a message
    instead of shifting the queue for no visible change.

    With the table sorted by Due descending, later positions sit higher
    up, so a line above a row means just after it in the queue and a line
    below it means just before: the two cases swap.

    On success the table is re-searched (sorted by Due if it wasn't) so
    the rows actually move; see `refresh_order`.
    """
    if not card_ids or target is None:
        return False
    target_id, due = target
    ids = [int(c) for c in card_ids]
    new_ids = new_cards_only(ids)
    if not new_ids:
        _tooltip(browser, "Only new cards can be repositioned")
        return False
    skipped = len(ids) - len(new_ids)
    column, backwards = sort_state(browser)
    after = bool(below) != bool(column == DUE_COLUMN and backwards)
    start = int(due) + 1 if after else int(due)
    try:
        from anki.cards import CardId
        from aqt.operations.scheduling import reposition_new_cards

        reposition_new_cards(
            parent=browser,
            card_ids=[CardId(c) for c in new_ids],
            starting_from=start,
            step_size=1,
            randomize=False,
            shift_existing=True,
        ).success(
            lambda out, skipped=skipped: _on_repositioned(browser, out, skipped)
        ).run_in_background()
        return True
    except Exception:
        return False


class _DropTarget(QObject):
    """Accepts our card drags on a view, with a marker over the row under
    the cursor. `resolve(pos)` says what is there (or None), `perform`
    does the drop, `marker_rect` says where the marker goes (by default a
    box around the row). Subclasses fill those in."""

    MARKER_NAME = "ba-card-drop-marker"

    def __init__(self, browser: Any, view: Any) -> None:
        super().__init__(view)
        self.browser = browser
        self.view = view
        self.marker: Optional[QFrame] = None
        self.hover: Any = None
        self.drops: List[tuple] = []

    # -- what's under the cursor -------------------------------------------
    def resolve(self, pos: QPoint) -> Any:
        raise NotImplementedError

    def perform(self, card_ids: List[int], target: Any) -> bool:
        raise NotImplementedError

    def on_hover(self, pos: QPoint, target: Any) -> None:
        pass

    # -- highlight ------------------------------------------------------------
    def _viewport(self) -> Any:
        try:
            return self.view.viewport()
        except Exception:
            return self.view

    def _row_rect(self, pos: QPoint) -> Optional[QRect]:
        try:
            index = self.view.indexAt(pos)
            if not index.isValid():
                return None
            vr = self.view.visualRect(index)
            vp = self._viewport()
            return QRect(0, vr.top(), vp.width(), vr.height())
        except Exception:
            return None

    def marker_rect(self, pos: QPoint, target: Any) -> Optional[QRect]:
        """Where the marker goes for a hover at `pos` over `target`."""
        return self._row_rect(pos)

    def marker_style(self, accent: str) -> str:
        """The marker's stylesheet: a box around the row."""
        return (
            "QFrame#" + self.MARKER_NAME + " { border: 2px solid "
            + accent + "; border-radius: 6px; background: transparent; }"
        )

    def _show_marker(self, rect: Optional[QRect]) -> None:
        if rect is None:
            self._hide_marker()
            return
        try:
            if self.marker is None:
                accent = "#6c8cff"
                try:
                    from . import addcard as _addcard

                    accent = _addcard._config().get("accent", accent)
                except Exception:
                    pass
                m = QFrame(self._viewport())
                m.setObjectName(self.MARKER_NAME)
                m.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
                m.setStyleSheet(self.marker_style(accent))
                self.marker = m
            self.marker.setGeometry(rect)
            self.marker.show()
            self.marker.raise_()
        except Exception:
            pass

    def _hide_marker(self) -> None:
        if self.marker is not None:
            try:
                self.marker.hide()
            except Exception:
                pass

    # -- filter -----------------------------------------------------------------
    def _local(self, obj: Any, ev: Any) -> QPoint:
        pos = _pos(ev)
        vp = self._viewport()
        if obj is not vp and obj is self.view:
            # Delivered to the view rather than its viewport: translate.
            try:
                pos = vp.mapFrom(self.view, pos)
            except Exception:
                pass
        return pos

    def eventFilter(self, obj: Any, ev: Any) -> bool:  # noqa: N802 (Qt)
        try:
            etype = ev.type()
            if etype == QEvent.Type.DragEnter:
                if not decode_cards(ev.mimeData()):
                    return False
                ev.acceptProposedAction()
                self._on_move(obj, ev)
                return True
            if etype == QEvent.Type.DragMove:
                if not decode_cards(ev.mimeData()):
                    return False
                self._on_move(obj, ev)
                return True
            if etype == QEvent.Type.DragLeave:
                if self.hover is None and self.marker is None:
                    return False
                self._leave()
                return False
            if etype == QEvent.Type.Drop:
                ids = decode_cards(ev.mimeData())
                if not ids:
                    return False
                return self._on_drop(obj, ev, ids)
        except Exception:
            self._leave()
        return False

    def _on_move(self, obj: Any, ev: Any) -> None:
        pos = self._local(obj, ev)
        target = self.resolve(pos)
        self.hover = target
        if target is None:
            self._hide_marker()
            ev.ignore()
            return
        self._show_marker(self.marker_rect(pos, target))
        self.on_hover(pos, target)
        try:
            ev.setDropAction(Qt.DropAction.MoveAction)
        except Exception:
            pass
        ev.accept()

    def _leave(self) -> None:
        self.hover = None
        self._hide_marker()

    def _on_drop(self, obj: Any, ev: Any, ids: List[int]) -> bool:
        pos = self._local(obj, ev)
        target = self.resolve(pos)
        self._leave()
        if target is None:
            ev.ignore()
            return True
        ok = self.perform(ids, target)
        if ok:
            self.drops.append((tuple(ids), target))
            try:
                ev.setDropAction(Qt.DropAction.MoveAction)
            except Exception:
                pass
            ev.accept()
        else:
            ev.ignore()
        return True


class _SidebarDrop(_DropTarget):
    """Drop on a deck row: move the cards there."""

    def __init__(self, browser: Any, sidebar: Any) -> None:
        super().__init__(browser, sidebar)
        self.expand_index: Any = None
        self.expand_timer: Optional[QTimer] = None
        try:
            t = QTimer(self)
            t.setSingleShot(True)
            t.setInterval(_EXPAND_DELAY_MS)
            t.timeout.connect(self._expand_hovered)
            self.expand_timer = t
        except Exception:
            self.expand_timer = None

    def resolve(self, pos: QPoint) -> Optional[int]:
        return deck_at(self.view, pos)

    def perform(self, card_ids: List[int], target: Any) -> bool:
        return move_to_deck(self.browser, card_ids, int(target))

    def on_hover(self, pos: QPoint, target: Any) -> None:
        # Linger over a closed deck and it opens, like the tree does for
        # its own drags.
        try:
            index = self.view.indexAt(pos)
            if index != self.expand_index:
                self.expand_index = index
                if self.expand_timer is not None:
                    self.expand_timer.start()
        except Exception:
            pass

    def _expand_hovered(self) -> None:
        index = self.expand_index
        if index is None:
            return
        try:
            if index.isValid() and not self.view.isExpanded(index):
                self.view.expand(index)
        except Exception:
            pass

    def _leave(self) -> None:
        super()._leave()
        self.expand_index = None
        if self.expand_timer is not None:
            try:
                self.expand_timer.stop()
            except Exception:
                pass


class _TableDrop(_DropTarget):
    """Drop between rows: an insertion line on the top or bottom edge of
    the row under the cursor, whichever half of it the cursor is in, and
    on release the dragged new cards are repositioned to that line."""

    MARKER_NAME = "ba-card-drop-line"
    LINE_H = 2

    def resolve(self, pos: QPoint) -> Optional[tuple]:
        """(card_id, due, is_new, below, row) for the row under `pos`:
        `below` when the cursor is in the row's lower half."""
        # Every card row is a drop target, so a drop on a review card is
        # answered with why nothing moved instead of a silent "no entry"
        # cursor (the Week 1 decks the user drags in are all review cards).
        found = card_at(self.browser, pos)
        if found is None:
            return None
        below, row = False, -1
        try:
            index = self.view.indexAt(pos)
            row = int(index.row())
            vr = self.view.visualRect(index)
            below = (pos.y() - vr.top()) * 2 >= vr.height()
        except Exception:
            pass
        return found + (below, row)

    def marker_rect(self, pos: QPoint, target: Any) -> Optional[QRect]:
        """A line across the viewport on the row's top edge, or its bottom
        edge when the cursor is in the lower half."""
        row = self._row_rect(pos)
        if row is None:
            return None
        below = bool(target[3]) if target and len(target) > 3 else False
        edge = row.top() + row.height() if below else row.top()
        y = max(0, edge - self.LINE_H // 2)
        return QRect(0, y, row.width(), self.LINE_H)

    def marker_style(self, accent: str) -> str:
        return (
            "QFrame#" + self.MARKER_NAME + " { border: none; background: "
            + accent + "; border-radius: 1px; }"
        )

    def _covered_by(self, row: int, card_ids: List[int]) -> bool:
        """True when the dragged cards are exactly the run of rows around
        `row` that is being dragged: a drop on the group's own rows, where
        the order would come out as it is. Skipped as a no-op rather than
        renumbering the queue and reporting a move that shows nothing.
        Rows are read only along that run, so this is cheap."""
        try:
            table = self.browser.table
            model = table._model
            ids = set(int(c) for c in card_ids)
            n = int(table.len())

            def dragged(r: int) -> bool:
                card = model.get_card(model.index(r, 0))
                return card is not None and int(card.id) in ids

            if row < 0 or row >= n or not dragged(row):
                return False
            lo = row
            while lo > 0 and dragged(lo - 1):
                lo -= 1
            hi = row
            while hi + 1 < n and dragged(hi + 1):
                hi += 1
            items = [model.get_item(model.index(r, 0)) for r in range(lo, hi + 1)]
            covered = set(int(c) for c in model._state.get_card_ids(items))
            return ids <= covered
        except Exception:
            return False

    def perform(self, card_ids: List[int], target: Any) -> bool:
        target_id, due, is_new, below, row = target
        if not is_new:
            if [c for c in card_ids if int(c) != int(target_id)]:
                _tooltip(self.browser, NOT_NEW_TARGET)
            return False
        if self._covered_by(row, card_ids):
            return False
        return reposition_at(self.browser, card_ids, (target_id, due), below)


# --------------------------------------------------------------------------- #
# Install
# --------------------------------------------------------------------------- #
def _accept_drops(view: Any) -> None:
    for w in (view, getattr(view, "viewport", lambda: None)()):
        if w is None:
            continue
        try:
            w.setAcceptDrops(True)
        except Exception:
            pass


def _watch(view: Any, watcher: QObject) -> None:
    """Filter both the view and its viewport. Qt hands drag events to the
    first widget under the cursor that accepts drops, and which of the two
    that is depends on attributes set elsewhere; a filter on each means we
    see the event exactly once either way (the scroll area forwards from
    viewport to view without re-dispatching through filters)."""
    try:
        view.viewport().installEventFilter(watcher)
    except Exception:
        pass
    try:
        view.installEventFilter(watcher)
    except Exception:
        pass


def install(browser: Any) -> Optional[Dict[str, Any]]:
    """Make the Browse table a drag source and the sidebar and table drop
    targets for its cards. Returns the watchers, for tests."""
    if not enabled():
        return None
    if getattr(browser, "_ba_card_drag", None) is not None:
        return browser._ba_card_drag
    table = getattr(browser, "table", None)
    view = getattr(table, "_view", None) if table is not None else None
    if view is None:
        return None

    watchers: Dict[str, Any] = {}
    try:
        source = _TableDrag(browser, view)
        view.viewport().installEventFilter(source)
        watchers["source"] = source
    except Exception:
        return None

    try:
        _accept_drops(view)
        table_drop = _TableDrop(browser, view)
        _watch(view, table_drop)
        watchers["table"] = table_drop
    except Exception:
        pass

    sidebar = getattr(browser, "sidebar", None)
    if sidebar is not None:
        try:
            _accept_drops(sidebar)
            side_drop = _SidebarDrop(browser, sidebar)
            _watch(sidebar, side_drop)
            watchers["sidebar"] = side_drop
        except Exception:
            pass

    # Park the watchers on the Browser: an event filter Qt doesn't own is
    # only alive as long as something Python-side holds it.
    browser._ba_card_drag = watchers
    return watchers
