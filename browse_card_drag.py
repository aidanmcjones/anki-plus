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
  - drop on a deck in the sidebar : `set_card_deck`, the same op the
                                    Change Deck dialog runs, one undo step
  - drop on a row of the table    : `reposition_new_cards`, the dragged new
                                    cards land just before that row's card
                                    in the new-card queue (only new cards
                                    have a position, so the row has to be a
                                    new card too)

How it is wired. QAbstractItemView has its own drag machinery, but it is
driven by the model (`flags`, `mimeData`), and the table's model is Anki's
`DataModel`. Rather than reach into that class we watch the table's
viewport with an event filter: a press on an already-selected row arms a
drag, and once the cursor travels past Qt's drag distance we start a
`QDrag` ourselves carrying the selected card ids under a private MIME
type. Qt defers the selection change on such a press until release, so
the selection is still intact when the drag starts and a plain click on a
selected row still collapses the selection to that row, exactly as
before.

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

    def _on_press(self, ev: Any) -> bool:
        self._disarm()
        if ev.button() != Qt.MouseButton.LeftButton or _extending(ev):
            return False
        pos = _pos(ev)
        index = self.view.indexAt(pos)
        if not index.isValid():
            return False
        sel = self.view.selectionModel()
        if sel is None or not sel.isSelected(index):
            # An unselected row: Qt selects it on press, and pulling from
            # it rubber-bands. Select first, then drag, as everywhere else.
            return False
        self.press_pos = QPoint(pos)
        self.armed = True
        # Let the view see the press: with ExtendedSelection it defers the
        # selection change to release, which is what keeps the selection
        # whole for the drag.
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
        dist = abs(pos.x() - self.press_pos.x()) + abs(pos.y() - self.press_pos.y())
        if dist < _drag_distance():
            # Below the threshold. Swallowed: the view would otherwise read
            # the wobble as the start of a rubber-band and collapse the
            # selection to the rows under it.
            return True
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
        try:
            from aqt.utils import tooltip

            n = len(card_ids)
            name = mw.col.decks.name(did)
            op = op.success(
                lambda _out, n=n, name=name: tooltip(
                    ("1 card" if n == 1 else f"{n} cards") + f" moved to {name}",
                    parent=browser,
                )
            )
        except Exception:
            pass
        op.run_in_background()
        return True
    except Exception:
        return False


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


def reposition_before(browser: Any, card_ids: List[int], target: tuple) -> bool:
    """Put the dragged new cards just before `target` in the new queue.

    `reposition_new_cards` with `shift_existing` bumps every new card at or
    after the start position along by the number moved, so the target and
    what follows it slide back and the dragged cards take their place.
    Cards that aren't new are left where they are by the op itself.
    """
    if not card_ids or target is None:
        return False
    target_id, due = target
    ids = [int(c) for c in card_ids if int(c) != int(target_id)]
    if not ids:
        return False
    try:
        from anki.cards import CardId
        from aqt.operations.scheduling import reposition_new_cards

        reposition_new_cards(
            parent=browser,
            card_ids=[CardId(c) for c in ids],
            starting_from=int(due),
            step_size=1,
            randomize=False,
            shift_existing=True,
        ).run_in_background()
        return True
    except Exception:
        return False


class _DropTarget(QObject):
    """Accepts our card drags on a view, with a highlight on the row under
    the cursor. `resolve(pos)` says what is there (or None), `perform`
    does the drop. Subclasses fill those in."""

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
                m.setObjectName("ba-card-drop-marker")
                m.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
                m.setStyleSheet(
                    "QFrame#ba-card-drop-marker { border: 2px solid "
                    + accent + "; border-radius: 6px; background: transparent; }"
                )
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
        self._show_marker(self._row_rect(pos))
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
    """Drop on a new card's row: reposition the dragged new cards before
    it."""

    def resolve(self, pos: QPoint) -> Optional[tuple]:
        return new_card_at(self.browser, pos)

    def perform(self, card_ids: List[int], target: Any) -> bool:
        return reposition_before(self.browser, card_ids, target)


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
