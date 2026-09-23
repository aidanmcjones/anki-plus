"""Regression tests for browse_card_drag.py: drag selected cards out of the
Browse table and drop them on a deck in the sidebar (move) or on a new
card's row in the table (reposition).

Reported as: "When selecting one card or multiple, I should be able to
select one or a group and drag and drop it to another deck or a different
place in the deck." Stock Anki's card table is not a drag source at all,
so on the unfixed code there is nothing to drag and nowhere to drop.

Runs standalone (no Anki or PyQt install needed):
`python3 tests/test_browse_card_drag.py`. `aqt` is stubbed with a small
fake Qt: views with rows, mouse and drag events, a QDrag that records
what it carried, and the two collection ops as recorders.
"""

import os
import sys
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


# --------------------------------------------------------------------------- #
# Fake Qt
# --------------------------------------------------------------------------- #

class QPoint:
    def __init__(self, x=0, y=0):
        if isinstance(x, QPoint):
            x, y = x.x(), x.y()
        self._x, self._y = x, y

    def x(self):
        return self._x

    def y(self):
        return self._y

    def toPoint(self):
        return self


class QRect:
    def __init__(self, x=0, y=0, w=0, h=0):
        self._x, self._y, self._w, self._h = x, y, w, h

    def top(self):
        return self._y

    def left(self):
        return self._x

    def width(self):
        return self._w

    def height(self):
        return self._h

    def tuple(self):
        return (self._x, self._y, self._w, self._h)


class _EventType:
    MouseButtonPress = 2
    MouseButtonRelease = 3
    MouseMove = 5
    DragEnter = 60
    DragMove = 61
    DragLeave = 62
    Drop = 63


class QEvent:
    Type = _EventType

    def __init__(self, t):
        self._t = t
        self.accepted = True

    def type(self):
        return self._t

    def accept(self):
        self.accepted = True

    def ignore(self):
        self.accepted = False


class _MouseButton:
    NoButton = 0
    LeftButton = 1
    RightButton = 2


class _Modifier:
    ShiftModifier = 0x02000000
    ControlModifier = 0x04000000
    MetaModifier = 0x10000000


class _DropAction:
    MoveAction = 2


class _WidgetAttribute:
    WA_TransparentForMouseEvents = 51


class Qt:
    MouseButton = _MouseButton
    KeyboardModifier = _Modifier
    DropAction = _DropAction
    WidgetAttribute = _WidgetAttribute


class QObject:
    def __init__(self, parent=None):
        self._parent = parent


class QByteArray:
    def __init__(self, b=b""):
        self._b = bytes(b)

    def data(self):
        return self._b


class QMimeData:
    def __init__(self):
        self._data = {}

    def setData(self, fmt, ba):
        self._data[fmt] = ba

    def hasFormat(self, fmt):
        return fmt in self._data

    def data(self, fmt):
        return self._data.get(fmt, QByteArray())


class QDrag:
    instances = []

    def __init__(self, source):
        self.source = source
        self.mime = None
        self.pixmap = None
        self.executed = None
        QDrag.instances.append(self)

    def setMimeData(self, m):
        self.mime = m

    def setPixmap(self, p):
        self.pixmap = p

    def setHotSpot(self, p):
        pass

    def exec(self, action):
        self.executed = action
        return action


class QApplication:
    @staticmethod
    def startDragDistance():
        return 10


class QTimer:
    def __init__(self, parent=None):
        self.timeout = self
        self._cb = None
        self.active = False

    def setSingleShot(self, v):
        pass

    def setInterval(self, ms):
        pass

    def connect(self, fn):
        self._cb = fn

    def start(self):
        self.active = True

    def stop(self):
        self.active = False

    def fire(self):
        if self.active:
            self.active = False
            self._cb()


class QFrame:
    def __init__(self, parent=None):
        self.parent = parent
        self.geometry = None
        self.visible = False

    def setObjectName(self, n):
        pass

    def setAttribute(self, a, v):
        pass

    def setStyleSheet(self, s):
        pass

    def setGeometry(self, r):
        self.geometry = r

    def show(self):
        self.visible = True

    def hide(self):
        self.visible = False

    def raise_(self):
        pass


class _Widget:
    def __init__(self, w=300):
        self._filters = []
        self._w = w
        self.accepts_drops = False

    def installEventFilter(self, f):
        self._filters.append(f)

    def setAcceptDrops(self, v):
        self.accepts_drops = bool(v)

    def width(self):
        return self._w

    def mapFrom(self, other, pos):
        return pos

    def event(self, ev):
        """Qt's dispatch: filters run in install order and a True stops
        the event before the widget sees it."""
        for f in list(self._filters):
            if f.eventFilter(self, ev):
                return True
        return False


class MouseEvent(QEvent):
    def __init__(self, t, x, y, button=Qt.MouseButton.LeftButton,
                 buttons=None, modifiers=0):
        super().__init__(t)
        self._pos = QPoint(x, y)
        self._button = button
        self._buttons = button if buttons is None else buttons
        self._mods = modifiers

    def position(self):
        return self._pos

    def button(self):
        return self._button

    def buttons(self):
        return self._buttons

    def modifiers(self):
        return self._mods


class DragEvent(QEvent):
    def __init__(self, t, x, y, mime):
        super().__init__(t)
        self.accepted = False
        self._pos = QPoint(x, y)
        self._mime = mime
        self.drop_action = None

    def position(self):
        return self._pos

    def mimeData(self):
        return self._mime

    def acceptProposedAction(self):
        self.accepted = True

    def setDropAction(self, a):
        self.drop_action = a


# --------------------------------------------------------------------------- #
# Fake views: 20px rows, index row = y // 20
# --------------------------------------------------------------------------- #

ROW_H = 20


class Index:
    def __init__(self, row):
        self._row = row

    def row(self):
        return self._row

    def isValid(self):
        return self._row >= 0

    def __eq__(self, other):
        return isinstance(other, Index) and other._row == self._row

    def __ne__(self, other):
        return not self.__eq__(other)

    def __hash__(self):
        return hash(self._row)


class _Selection:
    def __init__(self, view):
        self.view = view

    def isSelected(self, index):
        return index.row() in self.view.selected


class FakeView(_Widget):
    def __init__(self, rows):
        super().__init__()
        self.rows = rows
        self.selected = set()
        self._vp = _Widget()
        self.expanded = set()

    def viewport(self):
        return self._vp

    def indexAt(self, pos):
        r = pos.y() // ROW_H
        return Index(r if 0 <= r < len(self.rows) else -1)

    def visualRect(self, index):
        return QRect(30, index.row() * ROW_H, 120, ROW_H)

    def selectionModel(self):
        return _Selection(self)

    def isExpanded(self, index):
        return index.row() in self.expanded

    def expand(self, index):
        self.expanded.add(index.row())


class _SidebarItemType:
    DECK = "deck"
    DECK_CURRENT = "deck_current"
    DECK_ROOT = "deck_root"
    TAG = "tag"


class FakeSidebar(FakeView):
    """rows: (item_type, id)"""

    def model(self):
        view = self

        class _M:
            def item_for_index(self_inner, index):
                t, i = view.rows[index.row()]
                return types.SimpleNamespace(item_type=t, id=i, name=str(i))
        return _M()


class Card:
    def __init__(self, cid, ctype, due):
        self.id, self.type, self.due = cid, ctype, due


class _SortState:
    """Anki's ItemState: the sort column and direction the last search used,
    and (Cards mode) the card ids behind a list of row items."""

    def __init__(self, column="noteFld", backwards=False):
        self.sort_column = column
        self.sort_backwards = backwards

    def get_card_ids(self, items):
        return list(items)


class FakeTable:
    def __init__(self, cards):
        self._view = FakeView(cards)
        self._state = _SortState()
        self.indicator_sets = 0
        table = self

        class _Model:
            _state = self._state

            def index(self_inner, row, col=0):
                return Index(row)

            def get_item(self_inner, index):
                return table._view.rows[index.row()].id

            def get_card(self_inner, index):
                return table._view.rows[index.row()]
        self._model = _Model()

    def len(self):
        return len(self._view.rows)

    def get_selected_card_ids(self):
        # Qt's selectedRows() lists rows in the order they were selected,
        # not top to bottom; the drag must not depend on it.
        return [self._view.rows[r].id for r in reversed(sorted(self._view.selected))]

    def _set_sort_indicator(self):
        self.indicator_sets += 1


class FakeBrowser:
    def __init__(self, cards, sidebar_rows):
        self.table = FakeTable(cards)
        self.sidebar = FakeSidebar(sidebar_rows)
        self.searches = 0

    def search(self):
        """Browser.search(): re-runs the last search, which is the only
        thing that re-sorts the rows (op_executed only redraws cells)."""
        self.searches += 1


# --------------------------------------------------------------------------- #
# Stubs: aqt, anki, and the two ops as recorders
# --------------------------------------------------------------------------- #

def _stub(name, **attrs):
    mod = sys.modules.get(name) or types.ModuleType(name)
    for k, v in attrs.items():
        setattr(mod, k, v)
    sys.modules[name] = mod
    return mod


class _AddonManager:
    cfg = {}

    def getConfig(self, *a, **k):
        return dict(self.cfg)


FILTERED = {7}
CURRENT_DECK = 3


class _Decks:
    def is_filtered(self, did):
        return did in FILTERED

    def get_current_id(self):
        return CURRENT_DECK

    def name(self, did):
        return f"Deck {did}"


CARDS = {}


class _Col:
    def __init__(self):
        self.decks = _Decks()

    def get_card(self, cid):
        return CARDS[int(cid)]


_mw = types.SimpleNamespace(
    addonManager=_AddonManager(),
    col=_Col(),
)

OPS = []
TOOLTIPS = []


class _Op:
    def __init__(self, kind, kwargs):
        self.kind, self.kwargs = kind, kwargs
        self.ran = False
        self._success = None

    def success(self, fn):
        # CollectionOp.success replaces the callback, it does not chain.
        self._success = fn
        return self

    def run_in_background(self):
        self.ran = True
        OPS.append(self)

    def finish(self, count):
        """The collection op completed: run the success callback the way
        CollectionOp does, with an OpChangesWithCount."""
        assert self._success is not None, "no success callback attached"
        self._success(types.SimpleNamespace(count=count))


def set_card_deck(**kw):
    return _Op("set_card_deck", kw)


def reposition_new_cards(**kw):
    return _Op("reposition_new_cards", kw)


_stub("aqt", mw=_mw)
_stub(
    "aqt.qt",
    QApplication=QApplication, QByteArray=QByteArray, QDrag=QDrag,
    QEvent=QEvent, QFrame=QFrame, QMimeData=QMimeData, QObject=QObject,
    QPoint=QPoint, QRect=QRect, Qt=Qt, QTimer=QTimer,
)
_stub("aqt.browser")
_stub("aqt.browser.sidebar")
_stub("aqt.browser.sidebar.item", SidebarItemType=_SidebarItemType)
_stub("aqt.operations")
_stub("aqt.operations.card", set_card_deck=set_card_deck)
_stub("aqt.operations.scheduling", reposition_new_cards=reposition_new_cards)
_stub("aqt.utils", tooltip=lambda msg, *a, **k: TOOLTIPS.append(str(msg)))
_stub("anki")
_stub("anki.cards", CardId=int)
_stub("anki.decks", DeckId=int)
_stub("anki.consts", CARD_TYPE_NEW=0)

import browse_card_drag  # noqa: E402


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

NEW, REVIEW = 0, 2


def _boot():
    QDrag.instances.clear()
    OPS.clear()
    TOOLTIPS.clear()
    cards = [
        Card(10, NEW, 5),        # row 0
        Card(11, NEW, 6),        # row 1
        Card(12, NEW, 7),        # row 2
        Card(13, NEW, 8),        # row 3
        Card(14, REVIEW, 900),   # row 4
        Card(15, NEW, 20),       # row 5
    ]
    CARDS.clear()
    CARDS.update({c.id: c for c in cards})
    side = [
        (_SidebarItemType.DECK_ROOT, 0),        # row 0
        (_SidebarItemType.DECK_CURRENT, 0),     # row 1
        (_SidebarItemType.DECK, 42),            # row 2
        (_SidebarItemType.DECK, 7),             # row 3 (filtered)
        (_SidebarItemType.TAG, 99),             # row 4
    ]
    br = FakeBrowser(cards, side)
    watchers = browse_card_drag.install(br)
    assert watchers, "install() returned nothing"
    return br, watchers


def _press(view, x, y, **kw):
    return view.viewport().event(MouseEvent(QEvent.Type.MouseButtonPress, x, y, **kw))


def _move(view, x, y, **kw):
    return view.viewport().event(MouseEvent(QEvent.Type.MouseMove, x, y, **kw))


def _release(view, x, y):
    return view.viewport().event(MouseEvent(
        QEvent.Type.MouseButtonRelease, x, y, buttons=Qt.MouseButton.NoButton))


def _drag_to(view, t, x, y, mime):
    ev = DragEvent(t, x, y, mime)
    consumed = view.viewport().event(ev)
    return ev, consumed


def _pull(br, rows, from_row):
    """Select `rows`, press on `from_row`, pull past the drag distance.
    Returns the QDrag that started."""
    view = br.table._view
    view.selected = set(rows)
    y = from_row * ROW_H + 5
    assert _press(view, 10, y) is False, "press must reach the view"
    assert _move(view, 12, y + 2) is True, "sub-threshold wobble is swallowed"
    assert not QDrag.instances, "no drag before the threshold"
    assert _move(view, 10, y + 40) is True, "the drag consumes the move"
    assert len(QDrag.instances) == 1, "one drag should have started"
    return QDrag.instances[-1]


# --------------------------------------------------------------------------- #
# Wiring: the add-on installs the module on every Browser
# --------------------------------------------------------------------------- #

def test_addon_wires_install_on_browser_will_show():
    with open(os.path.join(ROOT, "__init__.py"), encoding="utf-8") as f:
        src = f.read()
    assert "browse_card_drag" in src, "__init__.py never imports browse_card_drag"
    assert "browser_will_show.append(_card_drag.install)" in src, (
        "browse_card_drag.install is not registered on browser_will_show"
    )
    import json
    with open(os.path.join(ROOT, "config.json"), encoding="utf-8") as f:
        cfg = json.load(f)
    assert cfg.get("browse_card_drag") is True, "config.json lacks browse_card_drag"


# --------------------------------------------------------------------------- #
# The table is a drag source for the selection
# --------------------------------------------------------------------------- #

def test_pulling_a_selected_row_starts_a_drag_with_every_selected_card():
    br, _ = _boot()
    drag = _pull(br, {1, 2, 3}, from_row=2)
    assert sorted(browse_card_drag.decode_cards(drag.mime)) == [11, 12, 13]
    assert drag.executed == Qt.DropAction.MoveAction
    assert drag.source is br.table._view


def test_single_card_drag():
    br, _ = _boot()
    drag = _pull(br, {5}, from_row=5)
    assert browse_card_drag.decode_cards(drag.mime) == [15]


def test_click_on_selected_row_is_still_a_click():
    br, _ = _boot()
    view = br.table._view
    view.selected = {1, 2, 3}
    assert _press(view, 10, 45) is False
    assert _move(view, 11, 46) is True
    assert _release(view, 11, 46) is False, "the release must reach the view"
    assert not QDrag.instances, "a click must not start a drag"
    # And the watcher is disarmed: a later move with no press does nothing.
    assert _move(view, 10, 200, buttons=Qt.MouseButton.NoButton) is False


def test_pulling_an_unselected_row_sideways_drags_it():
    # 20260923-101011: grab-and-pull on a row that was not selected used to
    # be left to the view (a rubber-band selection), so a one-motion drag
    # never moved anything. The press still reaches the view (which, in
    # real Qt, selects that row; see test_browse_card_drag_realqt.py) and
    # a sideways pull starts a drag.
    br, _ = _boot()
    view = br.table._view
    view.selected = {1}
    assert _press(view, 10, 85) is False, "the press must reach the view"
    assert _move(view, 12, 87) is True, "sub-threshold wobble is swallowed"
    assert _move(view, 60, 90) is True
    assert len(QDrag.instances) == 1


def test_sweeping_down_from_an_unselected_row_is_left_to_the_view():
    # 20260923-104846: "I cannot group-select cards by dragging". Press on
    # a row that is not selected and pull down the list: that is the
    # table's own gesture for selecting a run of rows, and the add-on
    # used to turn it into a drag of the one pressed card. Once past the
    # threshold along the list the moves must reach the view unconsumed,
    # and no drag may start.
    br, _ = _boot()
    view = br.table._view
    view.selected = {1}
    assert _press(view, 10, 85) is False, "the press must reach the view"
    assert _move(view, 12, 87) is True, "sub-threshold wobble is swallowed"
    assert _move(view, 11, 130) is False, "the sweep must reach the view"
    assert _move(view, 10, 175) is False
    assert _release(view, 10, 175) is False
    assert not QDrag.instances, "a sweep must not start a drag"
    # An upward sweep, and an exactly diagonal one, are sweeps too.
    assert _press(view, 10, 85) is False
    assert _move(view, 10, 40) is False
    assert not QDrag.instances
    assert _press(view, 10, 85) is False
    assert _move(view, 20, 95) is False
    assert not QDrag.instances


def test_sweeping_from_a_selected_row_still_drags_the_selection():
    # The group gathered by a sweep is moved by pressing on any of its rows
    # and pulling, in any direction: up or down the table to reposition,
    # sideways to the sidebar.
    br, _ = _boot()
    drag = _pull(br, {1, 2, 3}, from_row=1)   # _pull moves straight down
    assert sorted(browse_card_drag.decode_cards(drag.mime)) == [11, 12, 13]


def test_modifier_press_never_arms():
    br, _ = _boot()
    view = br.table._view
    view.selected = {1, 2}
    assert _press(view, 10, 25, modifiers=Qt.KeyboardModifier.ControlModifier) is False
    assert _move(view, 10, 80, modifiers=Qt.KeyboardModifier.ControlModifier) is False
    assert not QDrag.instances
    assert _press(view, 10, 25, button=Qt.MouseButton.RightButton) is False
    assert _move(view, 10, 80, buttons=Qt.MouseButton.RightButton) is False
    assert not QDrag.instances


# --------------------------------------------------------------------------- #
# Drop on a deck in the sidebar: move the cards there
# --------------------------------------------------------------------------- #

def test_drop_on_sidebar_deck_moves_the_cards():
    br, w = _boot()
    drag = _pull(br, {1, 2, 3}, from_row=2)
    side = br.sidebar
    # Enter over the tree.
    ev, consumed = _drag_to(side, QEvent.Type.DragEnter, 10, 45, drag.mime)
    assert consumed and ev.accepted
    # Over deck 42: accepted, row highlighted.
    ev, consumed = _drag_to(side, QEvent.Type.DragMove, 10, 45, drag.mime)
    assert consumed and ev.accepted
    marker = w["sidebar"].marker
    assert marker is not None and marker.visible
    assert marker.geometry.tuple() == (0, 40, 300, 20), marker.geometry.tuple()
    # Drop.
    ev, consumed = _drag_to(side, QEvent.Type.Drop, 10, 45, drag.mime)
    assert consumed and ev.accepted
    assert len(OPS) == 1 and OPS[0].kind == "set_card_deck", OPS
    op = OPS[0]
    assert op.ran
    assert op.kwargs["parent"] is br
    assert sorted(op.kwargs["card_ids"]) == [11, 12, 13]
    assert op.kwargs["deck_id"] == 42
    assert not marker.visible, "highlight must go once the drop is done"


def test_drop_on_current_deck_row_uses_the_current_deck():
    br, _ = _boot()
    drag = _pull(br, {0}, from_row=0)
    ev, _ = _drag_to(br.sidebar, QEvent.Type.Drop, 10, 25, drag.mime)
    assert ev.accepted
    assert OPS and OPS[0].kwargs["deck_id"] == CURRENT_DECK


def test_drop_on_filtered_deck_tag_or_heading_is_refused():
    br, w = _boot()
    drag = _pull(br, {1}, from_row=1)
    for y in (5, 65, 85, 500):  # root heading, filtered deck, tag, empty
        ev, consumed = _drag_to(br.sidebar, QEvent.Type.DragMove, 10, y, drag.mime)
        assert consumed and not ev.accepted, y
        ev, consumed = _drag_to(br.sidebar, QEvent.Type.Drop, 10, y, drag.mime)
        assert consumed and not ev.accepted, y
    assert not OPS


def test_foreign_drags_pass_through_to_the_tree():
    br, _ = _boot()
    other = QMimeData()
    other.setData("application/x-qabstractitemmodeldatalist", QByteArray(b"x"))
    for t in (QEvent.Type.DragEnter, QEvent.Type.DragMove, QEvent.Type.Drop):
        ev, consumed = _drag_to(br.sidebar, t, 10, 45, other)
        assert consumed is False, "the tree's own deck drags are not ours"
    assert not OPS


def test_hovering_a_collapsed_deck_expands_it():
    br, w = _boot()
    drag = _pull(br, {1}, from_row=1)
    _drag_to(br.sidebar, QEvent.Type.DragEnter, 10, 45, drag.mime)
    timer = w["sidebar"].expand_timer
    assert timer is not None and timer.active
    timer.fire()
    assert 2 in br.sidebar.expanded


# --------------------------------------------------------------------------- #
# Drop between rows of the table: an insertion line, and the new cards are
# repositioned to it
# --------------------------------------------------------------------------- #

def test_drop_on_a_new_card_row_repositions_before_it():
    br, w = _boot()
    drag = _pull(br, {2, 3}, from_row=3)
    view = br.table._view
    ev, consumed = _drag_to(view, QEvent.Type.DragMove, 10, 5, drag.mime)  # row 0, upper half
    assert consumed and ev.accepted
    assert w["table"].marker.visible
    ev, consumed = _drag_to(view, QEvent.Type.Drop, 10, 5, drag.mime)
    assert consumed and ev.accepted
    assert len(OPS) == 1 and OPS[0].kind == "reposition_new_cards", OPS
    kw = OPS[0].kwargs
    assert kw["parent"] is br
    assert list(kw["card_ids"]) == [12, 13]
    assert kw["starting_from"] == 5, "lands at the target card's position"
    assert kw["step_size"] == 1 and kw["randomize"] is False
    assert kw["shift_existing"] is True, "the target and what follows slide back"


def test_hover_shows_an_insertion_line_on_the_nearer_edge_not_a_box():
    # 20260923-120519: the drop target used to be a box drawn around the
    # hovered row, which reads as "onto this card". It is a line between
    # rows: on the row's top edge while the cursor is in its upper half,
    # on its bottom edge in the lower half, the viewport's full width.
    br, w = _boot()
    drag = _pull(br, {3}, from_row=3)
    view = br.table._view
    _drag_to(view, QEvent.Type.DragMove, 10, 25, drag.mime)   # row 1, upper half
    marker = w["table"].marker
    assert marker is not None and marker.visible
    assert marker.geometry.tuple() == (0, 19, 300, 2), marker.geometry.tuple()
    _drag_to(view, QEvent.Type.DragMove, 10, 35, drag.mime)   # row 1, lower half
    assert marker.geometry.tuple() == (0, 39, 300, 2), marker.geometry.tuple()
    _drag_to(view, QEvent.Type.DragMove, 10, 3, drag.mime)    # row 0, top edge
    assert marker.geometry.tuple() == (0, 0, 300, 2), "clamped inside the viewport"
    # The sidebar keeps its box: a card is dropped INTO a deck.
    _drag_to(br.sidebar, QEvent.Type.DragMove, 10, 45, drag.mime)
    assert w["sidebar"].marker.geometry.tuple() == (0, 40, 300, 20)


def test_drop_in_the_lower_half_puts_the_cards_after_the_row():
    br, _ = _boot()
    drag = _pull(br, {3}, from_row=3)
    ev, _ = _drag_to(br.table._view, QEvent.Type.Drop, 10, 15, drag.mime)   # row 0, lower half
    assert ev.accepted and len(OPS) == 1
    assert OPS[0].kwargs["starting_from"] == 6, "just after the target card"


def test_every_selected_card_moves_to_the_line_including_the_hovered_one():
    # 20260923-120519: "every selected card must actually be moved". The
    # hovered row's card used to be left out of the group when it was part
    # of the selection, so the others landed in front of it and the group
    # came out in a different order. Rows 0, 2 and 5 dropped above row 2:
    # all three go, in queue order, from row 2's position.
    br, _ = _boot()
    drag = _pull(br, {0, 2, 5}, from_row=5)
    ev, _ = _drag_to(br.table._view, QEvent.Type.Drop, 10, 42, drag.mime)   # row 2, upper half
    assert ev.accepted and len(OPS) == 1
    kw = OPS[0].kwargs
    assert list(kw["card_ids"]) == [10, 12, 15], "all three, in queue order"
    assert kw["starting_from"] == 7
    OPS[0].finish(count=3)
    assert any("3 cards repositioned" in t for t in TOOLTIPS), TOOLTIPS


def test_the_group_lands_in_queue_order_not_selection_order():
    # The selection model lists rows in the order they were clicked; the
    # op hands out positions in the order it is given. The group must keep
    # its queue order whatever order it was gathered in.
    br, _ = _boot()
    drag = _pull(br, {1, 3, 5}, from_row=1)
    assert browse_card_drag.decode_cards(drag.mime) == [15, 13, 11], "selection order"
    ev, _ = _drag_to(br.table._view, QEvent.Type.Drop, 10, 2, drag.mime)
    assert ev.accepted
    assert list(OPS[0].kwargs["card_ids"]) == [11, 13, 15]


def test_drop_on_a_review_card_row_is_refused_with_a_reason():
    # 20260923-101011: the user's decks are review cards; a silent "no
    # entry" cursor over them read as "dragging does nothing". The row now
    # highlights and the drop explains itself, but still runs no op.
    br, w = _boot()
    drag = _pull(br, {1}, from_row=1)
    view = br.table._view
    ev, consumed = _drag_to(view, QEvent.Type.DragMove, 10, 85, drag.mime)  # row 4
    assert consumed and ev.accepted
    assert w["table"].marker.visible
    ev, consumed = _drag_to(view, QEvent.Type.Drop, 10, 85, drag.mime)
    assert consumed and not ev.accepted
    assert not OPS
    assert any("review cards" in t.lower() for t in TOOLTIPS), TOOLTIPS


def test_sidebar_move_re_runs_the_search_so_the_cards_leave_the_list():
    # 20260923-101011: op_executed only repaints the old row snapshot, so
    # cards moved out of the deck being browsed stayed listed there.
    br, _ = _boot()
    drag = _pull(br, {1, 2}, from_row=2)
    ev, _ = _drag_to(br.sidebar, QEvent.Type.Drop, 10, 45, drag.mime)
    assert ev.accepted and len(OPS) == 1
    assert br.searches == 0
    OPS[0].finish(count=2)
    assert br.searches == 1
    assert any("2 cards moved to" in t for t in TOOLTIPS), TOOLTIPS


def test_drop_onto_itself_is_a_no_op():
    br, _ = _boot()
    drag = _pull(br, {1}, from_row=1)
    ev, _ = _drag_to(br.table._view, QEvent.Type.Drop, 10, 25, drag.mime)
    assert not ev.accepted
    assert not OPS
    ev, _ = _drag_to(br.table._view, QEvent.Type.Drop, 10, 35, drag.mime)   # lower half
    assert not ev.accepted
    assert not OPS


def test_drop_of_a_contiguous_group_onto_its_own_rows_is_a_no_op():
    # Rows 1..3 dropped between rows 2 and 3: the order would come out as
    # it is, so the queue is not renumbered and no move is reported.
    br, _ = _boot()
    drag = _pull(br, {1, 2, 3}, from_row=2)
    ev, _ = _drag_to(br.table._view, QEvent.Type.Drop, 10, 55, drag.mime)   # row 2, lower half
    assert not ev.accepted
    assert not OPS
    # But a group with a gap in it does gather at the line.
    QDrag.instances.clear()
    drag = _pull(br, {1, 3}, from_row=3)
    ev, _ = _drag_to(br.table._view, QEvent.Type.Drop, 10, 62, drag.mime)   # row 3, upper half
    assert ev.accepted and len(OPS) == 1
    assert list(OPS[0].kwargs["card_ids"]) == [11, 13]
    assert OPS[0].kwargs["starting_from"] == 8


# --------------------------------------------------------------------------- #
# Regression: "It says it moved but it actually stays in the same place."
#
# Table.op_executed only marks the cell cache stale and redraws: the row
# order is a snapshot of the last search. So after the reposition op the
# Due column changes but the rows do not move, and a table sorted by
# anything other than Due could not show the new order anyway.
# --------------------------------------------------------------------------- #

def test_table_re_sorts_by_due_once_the_reposition_lands():
    br, _ = _boot()
    br.table._state = _SortState("noteFld", False)   # the stock default
    drag = _pull(br, {3}, from_row=3)
    ev, _ = _drag_to(br.table._view, QEvent.Type.Drop, 10, 5, drag.mime)  # row 0
    assert ev.accepted and len(OPS) == 1
    assert br.searches == 0, "nothing to re-sort until the op has run"
    OPS[0].finish(count=1)
    assert br.searches == 1, "the search must re-run so the rows re-sort"
    assert br.table._state.sort_column == "cardDue", (
        "the order only shows with the table sorted by position"
    )
    assert br.table._state.sort_backwards is False
    assert br.table.indicator_sets == 1, "the header must show the new sort"
    assert any("1 card" in t and "reposition" in t for t in TOOLTIPS), TOOLTIPS


def test_a_due_sorted_table_keeps_its_sort_and_still_refreshes():
    br, _ = _boot()
    br.table._state = _SortState("cardDue", False)
    drag = _pull(br, {2, 3}, from_row=3)
    _drag_to(br.table._view, QEvent.Type.Drop, 10, 5, drag.mime)
    assert OPS[0].kwargs["starting_from"] == 5
    OPS[0].finish(count=2)
    assert br.searches == 1
    assert br.table._state.sort_column == "cardDue"
    assert br.table.indicator_sets == 0, "already sorted by Due: leave the header be"
    assert any("2 cards" in t and "reposition" in t for t in TOOLTIPS), TOOLTIPS


def test_due_descending_puts_the_cards_after_the_target():
    # Sorted by Due, newest position at the top: a line above a row on
    # screen means later in the queue, so the cards land just after it,
    # and a line below it means just before.
    br, _ = _boot()
    br.table._state = _SortState("cardDue", True)
    drag = _pull(br, {0}, from_row=0)
    ev, _ = _drag_to(br.table._view, QEvent.Type.Drop, 10, 62, drag.mime)  # row 3 (due 8), upper half
    assert ev.accepted and len(OPS) == 1
    assert OPS[0].kwargs["starting_from"] == 9
    OPS[0].finish(count=1)
    assert br.table._state.sort_backwards is True, "the user's direction stays"
    assert br.searches == 1
    QDrag.instances.clear()
    drag = _pull(br, {0}, from_row=0)
    ev, _ = _drag_to(br.table._view, QEvent.Type.Drop, 10, 75, drag.mime)  # row 3, lower half
    assert ev.accepted and len(OPS) == 2
    assert OPS[1].kwargs["starting_from"] == 8


def test_no_success_message_when_nothing_changed():
    br, _ = _boot()
    drag = _pull(br, {3}, from_row=3)
    _drag_to(br.table._view, QEvent.Type.Drop, 10, 5, drag.mime)
    OPS[0].finish(count=0)
    assert not any("reposition" in t for t in TOOLTIPS), TOOLTIPS
    assert br.searches == 1, "the table is still refreshed"


def test_dragging_only_review_cards_onto_a_new_row_says_so_and_runs_no_op():
    br, _ = _boot()
    drag = _pull(br, {4}, from_row=4)   # card 14, a review card
    view = br.table._view
    ev, consumed = _drag_to(view, QEvent.Type.Drop, 10, 5, drag.mime)  # row 0, new
    assert consumed and not ev.accepted
    assert not OPS, "a review card has no position: no op, no shifted queue"
    assert any("new" in t.lower() for t in TOOLTIPS), TOOLTIPS
    assert br.searches == 0


def test_a_mixed_drag_repositions_only_its_new_cards():
    br, _ = _boot()
    drag = _pull(br, {3, 4, 5}, from_row=5)   # 13 new, 14 review, 15 new
    ev, _ = _drag_to(br.table._view, QEvent.Type.Drop, 10, 5, drag.mime)
    assert ev.accepted and len(OPS) == 1
    assert list(OPS[0].kwargs["card_ids"]) == [13, 15]
    OPS[0].finish(count=2)
    assert any("2 cards" in t and "1 " in t for t in TOOLTIPS), TOOLTIPS


# --------------------------------------------------------------------------- #
# Guards
# --------------------------------------------------------------------------- #

def test_install_is_idempotent_and_config_gated():
    br, w = _boot()
    assert browse_card_drag.install(br) is w
    _AddonManager.cfg = {"browse_card_drag": False}
    try:
        assert browse_card_drag.install(FakeBrowser([], [])) is None
    finally:
        _AddonManager.cfg = {}


def test_drop_targets_accept_drops():
    br, _ = _boot()
    assert br.table._view.accepts_drops and br.table._view.viewport().accepts_drops
    assert br.sidebar.accepts_drops and br.sidebar.viewport().accepts_drops


if __name__ == "__main__":
    names = [n for n in list(globals()) if n.startswith("test_")]
    failed = 0
    for name in names:
        try:
            globals()[name]()
            print(f"ok   {name}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            import traceback
            print(f"FAIL {name}: {e!r}")
            traceback.print_exc()
    if failed:
        print(f"{failed} of {len(names)} failed")
        sys.exit(1)
    print(f"PASS test_browse_card_drag ({len(names)} tests)")
