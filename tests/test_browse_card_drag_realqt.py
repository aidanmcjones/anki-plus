"""Browse card drag through REAL aqt + PyQt6: a real Table, a real
SidebarTreeView over a real collection, and a real QDrag.

test_browse_card_drag.py fakes Qt, so it could not tell whether a press,
a pull and a release in the real widgets ever get from the table to the
sidebar. Here the `minimal` Qt platform is used on purpose: `offscreen`
stubs QDrag.exec() to return at once, while `minimal` runs Qt's own
QSimpleDrag, so synthetic window-level mouse events drive a genuine drag
(DragEnter/DragMove/Drop delivered by Qt to whatever widget is under the
cursor, through the add-on's filters and Anki's own tree).

Covers ticket 20260923-101011 ("I still cannot move cards around at all"):
  1. a selected group pulled onto a sidebar deck moves every card there,
     and the Week 1 list re-searches so they actually leave it
  2. grab-and-pull on an UNSELECTED row drags that card in one motion
  3. dropping on a review card's row says why nothing moved
  4. dropping new cards on a new card's row repositions them before it
"""

import os
import sys
import tempfile
import types

os.environ["QT_QPA_PLATFORM"] = "minimal"
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
from _realqt import ensure_real_aqt  # noqa: E402

ensure_real_aqt(__file__)

from aqt.qt import (  # noqa: E402
    QApplication, QDockWidget, QItemSelectionModel, QMainWindow, QModelIndex,
    QPoint, QTimer, Qt,
)
from PyQt6.QtTest import QTest  # noqa: E402

app = QApplication.instance() or QApplication(sys.argv)

import anki.lang  # noqa: E402
import aqt  # noqa: E402
import aqt.operations.card as ops_card  # noqa: E402
import aqt.operations.scheduling as ops_sched  # noqa: E402
import aqt.utils  # noqa: E402
from anki.collection import Collection  # noqa: E402

anki.lang.set_lang("en_US")

tmp = tempfile.mkdtemp(prefix="ba-carddrag-")
col = Collection(os.path.join(tmp, "collection.anki2"))
WEEK1 = int(col.decks.id("Microbiology::Week 1"))
WEEK2 = int(col.decks.id("Microbiology::Week 2"))
basic = col.models.by_name("Basic")
for i in range(6):
    n = col.new_note(basic)
    n["Front"] = f"q{i}"
    n["Back"] = "a"
    col.add_note(n, WEEK1)


class _PM:
    profile: dict = {}

    def __getattr__(self, name):
        return lambda *a, **k: None

    def show_browser_table_tooltips(self):
        return False


mw = types.SimpleNamespace(col=col, pm=_PM(), app=app)
from aqt.flags import FlagManager  # noqa: E402

mw.flags = FlagManager(mw)
aqt.mw = mw

TOOLTIPS = []
aqt.utils.tooltip = lambda msg, *a, **k: TOOLTIPS.append(str(msg))


class _SyncOp:
    """A CollectionOp stand-in that runs the real collection call at once
    (there is no task manager here) and then the success callbacks."""

    def __init__(self, fn):
        self.fn, self.cbs = fn, []

    def success(self, cb):
        self.cbs.append(cb)
        return self

    def run_in_background(self, **_k):
        out = self.fn()
        for cb in self.cbs:
            cb(out)


ops_card.set_card_deck = lambda *, parent, card_ids, deck_id: _SyncOp(
    lambda: col.set_deck(card_ids, deck_id))
ops_sched.reposition_new_cards = lambda *, parent, card_ids, starting_from, step_size, randomize, shift_existing: _SyncOp(  # noqa: E501
    lambda: col.sched.reposition_new_cards(
        card_ids=card_ids, starting_from=starting_from, step_size=step_size,
        randomize=randomize, shift_existing=shift_existing))

from aqt.browser.sidebar.model import SidebarModel  # noqa: E402
from aqt.browser.sidebar.toolbar import SidebarTool  # noqa: E402
from aqt.browser.sidebar.tree import SidebarTreeView  # noqa: E402
from aqt.browser.table import Table  # noqa: E402

SEARCH = "deck:Microbiology::Week*1"


class Browser(QMainWindow):
    """Just enough Browser for the real Table and SidebarTreeView."""

    def __getattr__(self, name):
        if name.startswith("__") or name.startswith("_ba"):
            raise AttributeError(name)
        return lambda *a, **k: None

    def search(self):
        self.table.search(SEARCH)


br = Browser()
br.mw, br.col = mw, col
# The real Browse window layout, as Browser.__init__ builds it.
import aqt.forms  # noqa: E402

br.form = aqt.forms.browser.Ui_Dialog()
br.form.setupUi(br)
br.table = Table(br)
view = br.form.tableView
br.table.set_view(view)
sb = SidebarTreeView(br)
sb.setModel(SidebarModel(sb, sb._root_tree()))
sb._selection_model().selectionChanged.connect(sb._on_selection_changed)
sb.tool = SidebarTool.SELECT
br.sidebar = sb
dock = QDockWidget("Sidebar", br)
dock.setWidget(sb)
br.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, dock)
br.resize(1100, 650)
br.show()
QTest.qWaitForWindowExposed(br)
sb.expandAll()
br.search()

import importlib.util  # noqa: E402

spec = importlib.util.spec_from_file_location(
    "anki_design_card_drag", os.path.join(ROOT, "browse_card_drag.py"))
bcd = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bcd)
W = bcd.install(br)
assert W and {"source", "table", "sidebar"} <= set(W), W
win = br.windowHandle()


def row_ids():
    m = br.table._model
    return [int(m.get_card(m.index(r, 0)).id) for r in range(br.table.len())]


def select_rows(rows):
    sm = view.selectionModel()
    sm.clearSelection()
    for r in rows:
        sm.select(br.table._model.index(r, 0),
                  QItemSelectionModel.SelectionFlag.Select
                  | QItemSelectionModel.SelectionFlag.Rows)


def at_row(r):
    return view.viewport().mapTo(br, view.visualRect(br.table._model.index(r, 0)).center())


def sidebar_deck(name):
    m = sb.model()

    def walk(parent):
        for r in range(m.rowCount(parent)):
            c = m.index(r, 0, parent)
            if m.item_for_index(c).name == name:
                return c
            hit = walk(c)
            if hit is not None:
                return hit
        return None

    idx = walk(QModelIndex())
    assert idx is not None, name
    return sb.viewport().mapTo(br, sb.visualRect(idx).center())


def real_drag(src, dst):
    """Press at src, pull past the drag distance, move to dst, release:
    all as window-level mouse events, so Qt's QSimpleDrag does the rest."""
    seen = {}

    def during():
        QTest.mouseMove(win, dst + QPoint(0, 1))
        QTest.qWait(40)
        QTest.mouseMove(win, dst)
        QTest.qWait(40)
        seen["selected_during_drag"] = len(br.table.get_selected_card_ids())
        QTest.mouseRelease(win, Qt.MouseButton.LeftButton,
                           Qt.KeyboardModifier.NoModifier, dst)

    QTest.mousePress(win, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, src)
    QTimer.singleShot(80, during)
    step = QApplication.startDragDistance() + 8
    QTest.mouseMove(win, src + QPoint(2, 1))
    QTest.qWait(10)
    QTest.mouseMove(win, src + QPoint(step, 4))   # drag starts, nested loop
    QTest.qWait(150)
    return seen


def main():
    ids = row_ids()
    assert len(ids) == 6, ids

    # 1. A selected group onto a sidebar deck.
    TOOLTIPS.clear()
    select_rows([0, 1, 2])
    seen = real_drag(at_row(1), sidebar_deck("Week 2"))
    assert seen.get("selected_during_drag") == 3, seen
    moved = ids[:3]
    assert [int(col.get_card(c).did) for c in moved] == [WEEK2] * 3, "cards not moved"
    assert row_ids() == ids[3:], "moved cards must leave the Week 1 list: %r" % row_ids()
    assert any("3 cards moved to" in t for t in TOOLTIPS), TOOLTIPS
    print("ok  selected group dragged onto 'Week 2' moved all 3 and left the list")

    # 2. Grab an unselected row and pull, in one motion.
    ids = row_ids()
    select_rows([0])
    seen = real_drag(at_row(2), sidebar_deck("Week 2"))
    assert seen.get("selected_during_drag") == 1, seen
    assert int(col.get_card(ids[2]).did) == WEEK2, "unselected-row drag did not move it"
    assert int(col.get_card(ids[0]).did) == WEEK1, "the old selection must stay put"
    assert row_ids() == [ids[0], ids[1]], row_ids()
    print("ok  press-and-pull on an unselected row drags that one card")

    # 3. Review cards: the drop explains itself, nothing changes.
    col.sched.set_due_date([ids[1]], "3")
    br.search()
    ids = row_ids()
    review_row = ids.index(int(col.find_cards("is:review")[0]))
    new_row = 1 - review_row
    TOOLTIPS.clear()
    before = [(int(c.id), int(c.due), int(c.type)) for c in map(col.get_card, ids)]
    select_rows([new_row])
    real_drag(at_row(new_row), at_row(review_row))
    after = [(int(c.id), int(c.due), int(c.type)) for c in map(col.get_card, ids)]
    assert before == after, "a drop on a review card must change nothing"
    assert any("review cards" in t.lower() for t in TOOLTIPS), TOOLTIPS
    print("ok  drop on a review card's row says why, changes nothing")

    # 4. New onto new: reposition before the target.
    for c in col.find_cards("deck:Microbiology::Week*2"):
        col.set_deck([c], WEEK1)
    col.sched.schedule_cards_as_new(col.find_cards("is:review"))
    br.search()
    ids = row_ids()
    dues = {c: int(col.get_card(c).due) for c in ids}
    order = sorted(ids, key=lambda c: dues[c])
    last, first = order[-1], order[0]
    select_rows([ids.index(last)])
    real_drag(at_row(ids.index(last)), at_row(ids.index(first)))
    assert int(col.get_card(last).due) < int(col.get_card(first).due), (
        "the dragged new card should now come before the target")
    print("ok  new card dropped on a new card's row is repositioned before it")
    print("PASS test_browse_card_drag_realqt")


if __name__ == "__main__":
    try:
        main()
    finally:
        col.close()
