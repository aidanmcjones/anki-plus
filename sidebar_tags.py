"""Anki Design: tags in the Browse sidebar select, drag and sort like cards.

Reported as: "I should also be able to mass select tags and move them
around with dragging and selecting and right clicking same as card, with
hovering above or below a tag a line appears where you can drop it."

The gestures on a tag row follow the Browse table's card rules
(browse_card_drag.py):

  - plain click                    : search the tag (sidebar_select.py)
  - Cmd/Shift-click                : add to / extend the selection
  - press on an UNSELECTED tag and
    sweep up or down               : the tag rows the cursor crosses become
                                     the selection; nothing is dragged
  - press on an unselected tag and
    pull sideways                  : that one tag is dragged
  - press on a SELECTED tag and
    pull                           : the whole selection is dragged
  - right-click                    : the menu acts on the whole selection
                                     ("Restudy N tags" is in restudy.py)

Where a drag of tags lands, by where in a tag row the cursor is:

  - top quarter    : an insertion line above the row; the tags move to sit
                     just before it, under the same parent, in the order
                     they are shown in
  - bottom quarter : the same, below the row, just after it
  - the middle     : the row is boxed; the tags nest inside it (Anki's own
                     reparent, as a stock drag does)
  - the Tags header: nest at the top level

Anki sorts tags by name and stores no order, so the order a line drop
makes lives in the add-on config: `tag_order` maps a parent tag's full
name ("" for the top level) to its children's full names in order. Ranked
children come first; the rest follow in Anki's name order; names that no
longer sit under that parent are ignored. A drop that moves tags to a new
parent renames them (`beta` under `alpha` becomes `alpha::beta`), so the
keys and entries of `tag_order` are renamed with them. The order is put on
the tree through `browser_will_build_tree` at the TAGS stage: Anki's own
`_tag_tree` builds the section (collapse state and all), then each
sibling group is sorted.

Deck rows get the same gestures and drop zones (sidebar_decks.py holds
what differs: deck ids, Anki's `reparent_decks`, and the order living in
the add-on's shared `deck_order`, the one the home deck list uses). The
mouse filter and the drop target below serve both kinds of row; a sweep
only selects rows of the kind it started on. Anything else is left to the
tree and to sidebar_select.py.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from aqt import mw
from aqt.qt import (
    QByteArray,
    QDrag,
    QEvent,
    QFrame,
    QItemSelection,
    QItemSelectionModel,
    QMimeData,
    QModelIndex,
    QObject,
    QPersistentModelIndex,
    QPoint,
    QRect,
    Qt,
    QTimer,
)

from . import browse_card_drag as _bcd

ADDON = __name__.split(".")[0]

MIME = "application/x-anki-design-tags"
TAG_ORDER_KEY = "tag_order"

# Share of a row's height, from its top and from its bottom, that reads as
# "on the line" rather than "into this tag".
EDGE = 0.25


def _log(msg: str) -> None:
    print(f"[anki-design] sidebar_tags: {msg}", flush=True)


def _config() -> Dict[str, Any]:
    try:
        return mw.addonManager.getConfig(ADDON) or {}
    except Exception:
        return {}


def enabled() -> bool:
    # Part of the modeless sidebar multi-select; one switch for both.
    return bool(_config().get("browse_sidebar_multiselect", True))


# --------------------------------------------------------------------------- #
# Tag names
# --------------------------------------------------------------------------- #
def _key(name: str) -> str:
    # Anki's tags are case-insensitive
    return (name or "").lower()


def parent_of(full: str) -> str:
    return full.rsplit("::", 1)[0] if "::" in full else ""


def leaf_of(full: str) -> str:
    return full.rsplit("::", 1)[-1]


def join(parent: str, leaf: str) -> str:
    return f"{parent}::{leaf}" if parent else leaf


def is_within(name: str, ancestor: str) -> bool:
    """`name` is `ancestor` or one of its descendants."""
    n, a = _key(name), _key(ancestor)
    return n == a or n.startswith(a + "::")


def prune(tags: List[str]) -> List[str]:
    """Drop tags whose ancestor is also in the list (it carries them), and
    duplicates. Order kept."""
    out: List[str] = []
    for t in tags:
        if any(_key(t) == _key(o) for o in out):
            continue
        if any(o != t and is_within(t, o) and _key(t) != _key(o) for o in tags):
            continue
        out.append(t)
    return out


# --------------------------------------------------------------------------- #
# Custom order (config)
# --------------------------------------------------------------------------- #
def order_state(cfg: Optional[Dict[str, Any]] = None) -> Dict[str, list]:
    cfg = cfg if cfg is not None else _config()
    raw = cfg.get(TAG_ORDER_KEY)
    return dict(raw) if isinstance(raw, dict) else {}


def apply_order(names: List[str], ranked: Any) -> List[int]:
    """Indices of `names` in display order: `ranked` first, in its order,
    then the rest in their incoming (Anki's name) order."""
    rank: Dict[str, int] = {}
    for i, r in enumerate(ranked or []):
        if isinstance(r, str):
            rank.setdefault(_key(r), i)
    tail = len(rank)
    idx = list(range(len(names)))
    if rank:
        idx.sort(key=lambda i: (rank.get(_key(names[i]), tail), i))
    return idx


def _ranked_for(order: Dict[str, list], parent: str) -> Any:
    if parent in order:
        return order[parent]
    for k, v in order.items():
        if _key(k) == _key(parent):
            return v
    return None


def sort_tag_items(section: Any, order: Dict[str, list]) -> None:
    """Sort every tag sibling group under the Tags section root in place.
    Non-tag rows (Untagged) keep their places in front."""
    from aqt.browser.sidebar.item import SidebarItemType

    def visit(node: Any, parent: str) -> None:
        kids = list(node.children)
        tags = [k for k in kids if k.item_type is SidebarItemType.TAG]
        if len(tags) > 1:
            ranked = _ranked_for(order, parent)
            if ranked:
                idx = apply_order([k.full_name for k in tags], ranked)
                tags = [tags[i] for i in idx]
                it = iter(tags)
                node.children = [
                    next(it) if k.item_type is SidebarItemType.TAG else k
                    for k in kids
                ]
        for k in tags:
            visit(k, k.full_name)

    visit(section, "")


def on_will_build_tree(handled: bool, root: Any, stage: Any, browser: Any) -> bool:
    """`browser_will_build_tree`: build the Tags section with Anki's own
    `_tag_tree`, then put the saved order on it."""
    try:
        from aqt.browser.sidebar.item import SidebarItemType
        from aqt.browser.sidebar.tree import SidebarStage

        if handled or stage is not SidebarStage.TAGS:
            return handled
        order = order_state()
        if not order:
            return handled
        sidebar = getattr(browser, "sidebar", None)
        if sidebar is None:
            return handled
    except Exception:
        return handled
    before = len(root.children)
    try:
        sidebar._tag_tree(root)
    except Exception as exc:
        _log(f"tag tree failed: {exc!r}")
        # let Anki try on its own, unless the section got half-built
        return len(root.children) != before
    try:
        for section in root.children[before:]:
            if section.item_type is SidebarItemType.TAG_ROOT:
                sort_tag_items(section, order)
    except Exception as exc:
        _log(f"sort failed: {exc!r}")
    return True


def renamed_state(
    order: Dict[str, list], renames: Dict[str, str]
) -> Dict[str, list]:
    """`order` with tag renames (old full name -> new) applied to its keys
    and entries (descendants too), and every list trimmed to names that
    still sit under its key."""

    def rn(name: str) -> str:
        for old, new in renames.items():
            if is_within(name, old):
                return new + name[len(old):]
        return name

    out: Dict[str, list] = {}
    for k, v in order.items():
        if not isinstance(v, list):
            continue
        nk = rn(k)
        seen = set()
        lst: List[str] = []
        for e in list(out.get(nk, [])) + [rn(str(x)) for x in v if isinstance(x, str)]:
            if _key(parent_of(e)) != _key(nk) or _key(e) in seen:
                continue
            seen.add(_key(e))
            lst.append(e)
        out[nk] = lst
    return out


def placed_order(
    siblings: List[str], moving: List[str], target: str, after: bool
) -> List[str]:
    """The sibling list after `moving` (new full names, in order) is put
    just before/after `target`. `target` may itself be moving: the group
    then gathers where it was."""
    mv = {_key(m) for m in moving}
    rest = [s for s in siblings if _key(s) not in mv]
    if _key(target) in mv:
        at = 0
        for s in siblings:
            if _key(s) == _key(target):
                break
            if _key(s) not in mv:
                at += 1
    else:
        keys = [_key(s) for s in rest]
        if _key(target) not in keys:
            return rest + list(moving)
        at = keys.index(_key(target)) + (1 if after else 0)
    return rest[:at] + list(moving) + rest[at:]


def _write_state(state: Dict[str, list]) -> None:
    cfg = _config()
    cfg[TAG_ORDER_KEY] = {k: v for k, v in state.items() if v}
    mw.addonManager.writeConfig(ADDON, cfg)


# --------------------------------------------------------------------------- #
# The sidebar's tag rows
# --------------------------------------------------------------------------- #
def tag_section(sidebar: Any) -> Any:
    from aqt.browser.sidebar.item import SidebarItemType

    try:
        for item in sidebar.model().root.children:
            if item.item_type is SidebarItemType.TAG_ROOT:
                return item
    except Exception:
        pass
    return None


def find_tag(sidebar: Any, full: str) -> Any:
    """The tag's SidebarItem ("" is the Tags section root), or None."""
    section = tag_section(sidebar)
    if section is None or not full:
        return section

    def walk(node: Any) -> Any:
        for k in node.children:
            if _key(getattr(k, "full_name", "")) == _key(full):
                return k
            if is_within(full, getattr(k, "full_name", "") or "\0"):
                hit = walk(k)
                if hit is not None:
                    return hit
        return None

    return walk(section)


def children_of(sidebar: Any, parent: str) -> List[str]:
    """Full names of the tag rows under `parent`, as shown."""
    from aqt.browser.sidebar.item import SidebarItemType

    node = find_tag(sidebar, parent)
    if node is None:
        return []
    return [k.full_name for k in node.children if k.item_type is SidebarItemType.TAG]


def tags_in_display_order(sidebar: Any, names: List[str]) -> List[str]:
    """`names` sorted into the order the sidebar shows them."""
    from aqt.browser.sidebar.item import SidebarItemType

    want = {_key(n) for n in names}
    out: List[str] = []
    section = tag_section(sidebar)

    def walk(node: Any) -> None:
        for k in node.children:
            if k.item_type is not SidebarItemType.TAG:
                continue
            if _key(k.full_name) in want:
                out.append(k.full_name)
            walk(k)

    if section is not None:
        walk(section)
    for n in names:
        if _key(n) not in {_key(o) for o in out}:
            out.append(n)
    return out


def selected_tags(sidebar: Any) -> List[str]:
    from aqt.browser.sidebar.item import SidebarItemType

    try:
        names = [
            it.full_name
            for it in sidebar._selected_items()
            if it is not None and it.item_type is SidebarItemType.TAG
        ]
    except Exception:
        return []
    return tags_in_display_order(sidebar, names)


def _item_at(sidebar: Any, pos: QPoint) -> tuple:
    try:
        index = sidebar.indexAt(pos)
        if not index.isValid():
            return QModelIndex(), None
        return index, sidebar.model().item_for_index(index)
    except Exception:
        return QModelIndex(), None


# --------------------------------------------------------------------------- #
# Moves
# --------------------------------------------------------------------------- #
def _tooltip(sidebar: Any, text: str) -> None:
    try:
        from aqt.utils import tooltip

        tooltip(text, parent=getattr(sidebar, "browser", None) or mw)
    except Exception:
        pass


def _refresh(sidebar: Any) -> None:
    try:
        sidebar.refresh()
    except Exception:
        pass


def _count(n: int) -> str:
    return "1 tag" if n == 1 else f"{n} tags"


def _reparent(sidebar: Any, tags: List[str], new_parent: str, done: str) -> None:
    """One `col.tags.reparent` for the lot: one op, one undo. The sidebar is
    refreshed afterwards itself: the inline Browse is never the focused
    window an op's own refresh waits for."""
    from aqt.operations import CollectionOp

    browser = getattr(sidebar, "browser", None) or mw

    def ok(_out: Any) -> None:
        _tooltip(sidebar, done)
        _refresh(sidebar)

    CollectionOp(
        browser, lambda col: col.tags.reparent(tags=tags, new_parent=new_parent)
    ).success(ok).run_in_background()


def nest(sidebar: Any, tags: List[str], new_parent: str) -> bool:
    """Middle-of-row drop: the tags go inside `new_parent` ("" = top level)."""
    moving = [
        t
        for t in prune(tags)
        if not is_within(new_parent, t) and _key(parent_of(t)) != _key(new_parent)
    ]
    if not moving:
        return False
    try:
        renames = {t: join(new_parent, leaf_of(t)) for t in moving}
        _write_state(renamed_state(order_state(), renames))
    except Exception as exc:
        _log(f"order update failed: {exc!r}")
    where = new_parent or "the top level"
    _reparent(sidebar, moving, new_parent, f"{_count(len(moving))} moved into {where}")
    return True


def place(sidebar: Any, tags: List[str], target: str, after: bool) -> bool:
    """Line drop: the tags sit just before (or after) `target`, under its
    parent, in the order they are shown in."""
    parent = parent_of(target)
    moving = [t for t in prune(tags) if not is_within(parent, t)]
    if not moving:
        return False
    moving = tags_in_display_order(sidebar, moving)
    new_names: List[str] = []
    for t in moving:
        n = join(parent, leaf_of(t))
        if _key(n) not in {_key(x) for x in new_names}:
            new_names.append(n)
    siblings = children_of(sidebar, parent)
    order = placed_order(siblings, new_names, target, after)
    to_move = [t for t in moving if _key(parent_of(t)) != _key(parent)]
    try:
        state = order_state()
        if to_move:
            state = renamed_state(state, {t: join(parent, leaf_of(t)) for t in to_move})
        state = {k: v for k, v in state.items() if _key(k) != _key(parent)}
        state[parent] = order
        _write_state(state)
    except Exception as exc:
        _log(f"order write failed: {exc!r}")
        return False
    done = f"{_count(len(moving))} moved"
    if to_move:
        _reparent(sidebar, to_move, parent, done)
    else:
        _tooltip(sidebar, done)
        _refresh(sidebar)
    return True


# --------------------------------------------------------------------------- #
# Drag source + sweep: tag and deck rows
# --------------------------------------------------------------------------- #
def encode_payload(mime_type: str, items: List[Any]) -> QMimeData:
    mime = QMimeData()
    mime.setData(mime_type, QByteArray(json.dumps(list(items)).encode("utf-8")))
    return mime


def decode_payload(mime: Any, mime_type: str) -> List[Any]:
    try:
        if mime is None or not mime.hasFormat(mime_type):
            return []
        raw = mime.data(mime_type)
        data = raw.data() if hasattr(raw, "data") else bytes(raw)
        if isinstance(data, memoryview):
            data = data.tobytes()
        return [t for t in json.loads(bytes(data).decode("utf-8")) if t]
    except Exception:
        return []


def encode_tags(tags: List[str]) -> QMimeData:
    return encode_payload(MIME, tags)


def decode_tags(mime: Any) -> List[str]:
    return [str(t) for t in decode_payload(mime, MIME)]


class _TagKind:
    """What the shared row gestures need to know about tag rows. The deck
    rows' counterpart is sidebar_decks.DeckKind; both answer the same
    questions, so one mouse filter and one drop target serve both."""

    ITEM = "TAG"
    ROOT = "TAG_ROOT"
    MIME = MIME

    def decode(self, mime: Any) -> List[Any]:
        return decode_tags(mime)

    def encode(self, items: List[Any]) -> QMimeData:
        return encode_tags(items)

    def selected(self, sidebar: Any) -> List[Any]:
        return selected_tags(sidebar)

    def count(self, n: int) -> str:
        return _count(n)

    def root_target(self, dragged: List[Any]) -> Optional[tuple]:
        if all(_key(parent_of(t)) == "" for t in prune(dragged)):
            return None
        return ("into", "")

    def edge_target(self, dragged: List[Any], item: Any, zone: str) -> Optional[tuple]:
        name = item.full_name
        parent = parent_of(name)
        if not [t for t in prune(dragged) if not is_within(parent, t)]:
            return None
        return (zone, name)

    def middle_target(self, dragged: List[Any], item: Any) -> Optional[tuple]:
        name = item.full_name
        # not into itself or its own subtree
        if any(is_within(name, t) for t in dragged):
            return None
        return ("into", name)

    def perform(self, sidebar: Any, items: List[Any], target: Any) -> bool:
        zone, name = target
        if zone == "into":
            return nest(sidebar, items, name)
        return place(sidebar, items, name, zone == "below")


TAGS = _TagKind()


def _kinds() -> List[Any]:
    kinds: List[Any] = [TAGS]
    try:
        from . import sidebar_decks as _decks

        kinds.append(_decks.DECKS)
    except Exception as exc:
        _log(f"deck rows unavailable: {exc!r}")
    return kinds


def _kind_of(item: Any) -> Any:
    if item is None:
        return None
    name = getattr(getattr(item, "item_type", None), "name", "")
    for k in _kinds():
        if k.ITEM == name:
            return k
    return None


class _RowMouse(QObject):
    """The card table's gesture rules, on tag and deck rows (see module
    doc). A sweep only ever selects rows of the kind it started on."""

    def __init__(self, sidebar: Any) -> None:
        super().__init__(sidebar)
        self.sidebar = sidebar
        self.kind: Any = None
        self.press_pos: Optional[QPoint] = None
        self.press_index: Optional[QPersistentModelIndex] = None
        self.press_selected = False
        self.armed = False
        self.sweeping = False
        self.dragging = False
        self.sweeps = 0

    def _disarm(self) -> None:
        self.armed = False
        self.sweeping = False
        self.press_pos = None
        self.press_index = None
        self.press_selected = False

    def eventFilter(self, obj: Any, ev: Any) -> bool:  # noqa: N802 (Qt)
        try:
            etype = ev.type()
            if etype == QEvent.Type.MouseButtonPress:
                return self._on_press(ev)
            if etype == QEvent.Type.MouseMove:
                return self._on_move(ev)
            if etype == QEvent.Type.MouseButtonRelease:
                self._disarm()
        except Exception:
            self._disarm()
        return False

    def _on_press(self, ev: Any) -> bool:
        self._disarm()
        if ev.button() != Qt.MouseButton.LeftButton or _bcd._extending(ev):
            return False
        pos = _bcd._pos(ev)
        index, item = _item_at(self.sidebar, pos)
        kind = _kind_of(item)
        if kind is None:
            return False
        sel = self.sidebar.selectionModel()
        if sel is None:
            return False
        # Qt still handles the press: an unselected row is selected now,
        # a selected one keeps the selection until release (so the whole
        # selection can be dragged, and a click still collapses it).
        self.kind = kind
        self.press_selected = bool(sel.isSelected(index))
        self.press_pos = QPoint(pos)
        self.press_index = QPersistentModelIndex(index)
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
        pos = _bcd._pos(ev)
        if self.sweeping:
            self._sweep_to(pos)
            return True
        dx = abs(pos.x() - self.press_pos.x())
        dy = abs(pos.y() - self.press_pos.y())
        if dx + dy < _bcd._drag_distance():
            # Below the threshold: the tree would take the wobble as the
            # start of its own drag.
            return True
        if not self.press_selected and dy >= dx:
            # A sweep along the list from an unselected row: select the run.
            self.sweeping = True
            self.sweeps += 1
            self._sweep_to(pos)
            return True
        self.armed = False
        self._start_drag()
        self._disarm()
        return True

    def _sweep_to(self, pos: QPoint) -> None:
        view = self.sidebar
        if self.press_index is None or not self.press_index.isValid():
            return
        start = QModelIndex(self.press_index)
        end = view.indexAt(QPoint(max(2, pos.x()), pos.y()))
        if not end.isValid():
            return
        rows: List[QModelIndex] = []
        # walk the visible rows from the press to the cursor
        down = view.visualRect(end).top() >= view.visualRect(start).top()
        idx, guard = start, 0
        while idx.isValid() and guard < 5000:
            guard += 1
            rows.append(idx)
            if idx == end:
                break
            idx = view.indexBelow(idx) if down else view.indexAbove(idx)
        model = view.model()
        sel = QItemSelection()
        for i in rows:
            if _kind_of(model.item_for_index(i)) is self.kind:
                sel.select(i, i)
        view.selectionModel().select(
            sel,
            QItemSelectionModel.SelectionFlag.ClearAndSelect
            | QItemSelectionModel.SelectionFlag.Rows,
        )

    def _start_drag(self) -> None:
        kind = self.kind
        if kind is None:
            return
        items = kind.selected(self.sidebar)
        if not items:
            return
        drag = QDrag(self.sidebar)
        drag.setMimeData(kind.encode(items))
        pm = _bcd._drag_pixmap(len(items), kind.count(len(items)))
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


_TagMouse = _RowMouse


# --------------------------------------------------------------------------- #
# Drop target: insertion line / nest box
# --------------------------------------------------------------------------- #
class _RowDrop(_bcd._DropTarget):
    """Drops of our tag or deck drags on the sidebar tree. `resolve` gives
    (zone, target) with zone "above", "below" or "into" (target: a tag's
    full name, or a deck id; "" / 0 for the section header); the marker is
    a 2px accent line on the row's top or bottom edge for the first two
    (the Browse table's card insertion line) and a box around the row for
    "into"."""

    MARKER_NAME = "ba-tag-drop-marker"
    LINE_H = 2

    def __init__(self, browser: Any, sidebar: Any) -> None:
        super().__init__(browser, sidebar)
        self.dragged: List[Any] = []
        self.kind: Any = None
        self.zone: Optional[str] = None
        self._style_zone: Optional[str] = None
        self.expand_index: Any = None
        self.expand_timer: Optional[QTimer] = None
        try:
            t = QTimer(self)
            t.setSingleShot(True)
            t.setInterval(_bcd._EXPAND_DELAY_MS)
            t.timeout.connect(self._expand_hovered)
            self.expand_timer = t
        except Exception:
            self.expand_timer = None

    def decode(self, mime: Any) -> List[Any]:
        for kind in _kinds():
            items = kind.decode(mime)
            if items:
                self.kind = kind
                self.dragged = items
                return items
        return []

    def resolve(self, pos: QPoint) -> Optional[tuple]:
        index, item = _item_at(self.view, pos)
        kind = self.kind
        if item is None or kind is None:
            return None
        tname = getattr(item.item_type, "name", "")
        if tname == kind.ROOT:
            return kind.root_target(self.dragged)
        if tname != kind.ITEM:
            return None
        vr = self.view.visualRect(index)
        frac = (pos.y() - vr.top()) / max(1, vr.height())
        if frac < EDGE or frac >= 1 - EDGE:
            return kind.edge_target(self.dragged, item, "above" if frac < EDGE else "below")
        return kind.middle_target(self.dragged, item)

    def marker_rect(self, pos: QPoint, target: Any) -> Optional[QRect]:
        row = self._row_rect(pos)
        if row is None:
            return None
        zone = target[0]
        if zone == "into":
            return row
        try:
            left = max(0, self.view.visualRect(self.view.indexAt(pos)).left())
        except Exception:
            left = 0
        edge = row.top() + row.height() if zone == "below" else row.top()
        y = max(0, edge - self.LINE_H // 2)
        return QRect(left, y, row.width() - left, self.LINE_H)

    def marker_style(self, accent: str) -> str:
        if self.zone == "into":
            return (
                "QFrame#" + self.MARKER_NAME + " { border: 2px solid "
                + accent + "; border-radius: 6px; background: transparent; }"
            )
        return (
            "QFrame#" + self.MARKER_NAME + " { border: none; background: "
            + accent + "; border-radius: 1px; }"
        )

    def _accent(self) -> str:
        try:
            from . import addcard as _addcard

            return _addcard._config().get("accent", "#6c8cff") or "#6c8cff"
        except Exception:
            return "#6c8cff"

    def _show_marker(self, rect: Optional[QRect]) -> None:
        if rect is None:
            self._hide_marker()
            return
        try:
            if self.marker is None:
                m = QFrame(self._viewport())
                m.setObjectName(self.MARKER_NAME)
                m.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
                self.marker = m
                self._style_zone = None
            style_zone = "into" if self.zone == "into" else "line"
            if style_zone != self._style_zone:
                self.marker.setStyleSheet(self.marker_style(self._accent()))
                self._style_zone = style_zone
            self.marker.setGeometry(rect)
            self.marker.show()
            self.marker.raise_()
        except Exception:
            pass

    def _on_move(self, obj: Any, ev: Any) -> None:
        pos = self._local(obj, ev)
        self._autoscroll(pos)
        target = self.resolve(pos)
        self.zone = target[0] if target else None
        super()._on_move(obj, ev)

    def _autoscroll(self, pos: QPoint) -> None:
        """Near the top or bottom of the tree, scroll a step, so a tag can
        be carried to rows that are off screen (Qt's own drag does this;
        ours replaces it)."""
        try:
            bar = self.view.verticalScrollBar()
            h = self._viewport().height()
            step = max(1, bar.singleStep())
            if pos.y() < 16:
                bar.setValue(bar.value() - step)
            elif pos.y() > h - 16:
                bar.setValue(bar.value() + step)
        except Exception:
            pass

    def on_hover(self, pos: QPoint, target: Any) -> None:
        # Linger in the middle of a closed tag and it opens.
        try:
            index = self.view.indexAt(pos) if target[0] == "into" else None
            if index != self.expand_index:
                self.expand_index = index
                if self.expand_timer is not None:
                    if index is None:
                        self.expand_timer.stop()
                    else:
                        self.expand_timer.start()
        except Exception:
            pass

    def _expand_hovered(self) -> None:
        index = self.expand_index
        try:
            if index is not None and index.isValid() and not self.view.isExpanded(index):
                self.view.expand(index)
        except Exception:
            pass

    def _leave(self) -> None:
        super()._leave()
        self.zone = None
        self.expand_index = None
        if self.expand_timer is not None:
            try:
                self.expand_timer.stop()
            except Exception:
                pass

    def perform(self, items: List[Any], target: Any) -> bool:
        if self.kind is None:
            return False
        return self.kind.perform(self.view, items, target)


_TagDrop = _RowDrop


# --------------------------------------------------------------------------- #
# Install
# --------------------------------------------------------------------------- #
def install(browser: Any) -> Optional[Dict[str, Any]]:
    """Tag and deck gestures on this Browser's sidebar. Returns the watchers."""
    if not enabled():
        return None
    sidebar = getattr(browser, "sidebar", None)
    if sidebar is None:
        return None
    if getattr(sidebar, "_ba_tag_drag", None) is not None:
        return sidebar._ba_tag_drag
    watchers: Dict[str, Any] = {}
    try:
        mouse = _RowMouse(sidebar)
        sidebar.viewport().installEventFilter(mouse)
        watchers["mouse"] = mouse
    except Exception as exc:
        _log(f"mouse install failed: {exc!r}")
        return None
    try:
        _bcd._accept_drops(sidebar)
        drop = _RowDrop(browser, sidebar)
        _bcd._watch(sidebar, drop)
        watchers["drop"] = drop
    except Exception as exc:
        _log(f"drop install failed: {exc!r}")
    sidebar._ba_tag_drag = watchers
    return watchers


def register() -> None:
    from aqt import gui_hooks

    gui_hooks.browser_will_show.append(install)
    gui_hooks.browser_will_build_tree.append(on_will_build_tree)
