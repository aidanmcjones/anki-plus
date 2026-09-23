"""Live: drag cards in the real Browse (the add-on's inline embed inside the
main window) and see them move.

Fixture: Parent::{A,B,C} with two new Basic cards each.

The mouse is real: presses and moves enter Qt through QTest's QWindow
overloads on the main window, reach the real Browse table's viewport, and
the add-on's filter starts a real QDrag. What offscreen cannot do is run
the OS drag session (QOffscreenDrag cancels every QDrag at once), so the
add-on's QDrag.exec is swapped for a stand-in that does what the platform
drag loop does with the pointer: find the widget under it the way
QWidgetWindow does (childAt, then up to the first widget that accepts
drops) and deliver DragEnter / DragMove / Drop there with the drag's own
mime data, honouring Qt's rule that a drag whose enter was not accepted
gets no move or drop. Everything after that (the add-on's drop filters,
Anki's SidebarTreeView, the collection op, the table refresh) is real,
and the checks read what the user sees: the rows in the table and where
the cards live in the collection.
"""

import sys


def _module(suffix):
    for name, mod in list(sys.modules.items()):
        if mod is not None and name.startswith("anki-design") and name.endswith(suffix):
            return mod
    return None


def run(t):
    from aqt.qt import (QApplication, QDragEnterEvent, QDragMoveEvent,
                        QDropEvent, QItemSelectionModel, QModelIndex, QPoint,
                        QPointF, Qt, QTimer)
    import aqt.utils

    mw, Q = t.mw, t.QTest
    col = mw.col
    bcd = _module(".browse_card_drag")
    embed = _module(".browse_embed")
    t.check("browse_card_drag and browse_embed are loaded", bcd and embed,
            [k for k in sys.modules if k.startswith("anki-design")][:20])
    if not (bcd and embed):
        return

    tips = []
    real_tooltip = aqt.utils.tooltip
    aqt.utils.tooltip = lambda msg, *a, **k: (tips.append(str(msg)), real_tooltip(msg, *a, **k))

    mw.onBrowse()
    br = t.wait_until(lambda: embed._state.get("browser"), timeout=20)
    t.check("Browse opens inline", br is not None, embed._state)
    if br is None:
        return
    t.pump(500)
    watchers = getattr(br, "_ba_card_drag", None)
    t.check("card drag is installed on the real Browser",
            isinstance(watchers, dict) and {"source", "table", "sidebar"} <= set(watchers),
            watchers)
    if not watchers:
        return

    view, sb = br.table._view, br.sidebar
    top = mw
    win = top.windowHandle()
    did = {n: int(col.decks.id_for_name("Parent::" + n)) for n in "ABC"}

    def search(q, n):
        br.search_for(q)
        return t.wait_until(lambda: br.table.len() == n, timeout=10)

    def row_ids():
        m = br.table._model
        return [int(m.get_card(m.index(r, 0)).id) for r in range(br.table.len())]

    def select(rows):
        smod = view.selectionModel()
        smod.clearSelection()
        for r in rows:
            smod.select(br.table._model.index(r, 0),
                        QItemSelectionModel.SelectionFlag.Select
                        | QItemSelectionModel.SelectionFlag.Rows)
        t.pump(50)

    def at_row(r):
        idx = br.table._model.index(r, 0)
        view.scrollTo(idx)
        return view.viewport().mapTo(top, view.visualRect(idx).center())

    def at_deck(name):
        m = sb.model()

        def walk(parent):
            for r in range(m.rowCount(parent)):
                c = m.index(r, 0, parent)
                it = m.item_for_index(c)
                if it is not None and it.name == name and it.full_name.startswith("Parent"):
                    return c
                hit = walk(c)
                if hit is not None:
                    return hit
            return None

        idx = walk(QModelIndex())
        if idx is None:
            return None
        sb.scrollTo(idx)
        return sb.viewport().mapTo(top, sb.visualRect(idx).center())

    # The OS drag loop, offscreen: deliver the drag where the pointer is.
    session = {}

    class LiveDrag(bcd.QDrag):
        def exec(self, *a, **k):
            session["started"] = True
            session["mime"] = list(self.mimeData().formats())
            session["selected_at_start"] = len(br.table.get_selected_card_ids())
            pos = session["target"]
            w = top.childAt(pos)
            while w is not None and not w.acceptDrops():
                w = w.parentWidget()
            session["target_widget"] = type(w).__name__ if w else None
            if w is None:
                return Qt.DropAction.IgnoreAction
            local = w.mapFrom(top, pos)
            md = self.mimeData()
            acts = Qt.DropAction.MoveAction
            btn, mod = Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier
            enter = QDragEnterEvent(local, acts, md, btn, mod)
            QApplication.sendEvent(w, enter)
            session["enter_accepted"] = enter.isAccepted()
            if not enter.isAccepted():
                return Qt.DropAction.IgnoreAction
            move = QDragMoveEvent(local, acts, md, btn, mod)
            QApplication.sendEvent(w, move)
            session["move_accepted"] = move.isAccepted()
            if not move.isAccepted():
                return Qt.DropAction.IgnoreAction
            drop = QDropEvent(QPointF(local), acts, md, btn, mod)
            QApplication.sendEvent(w, drop)
            session["drop_accepted"] = drop.isAccepted()
            return drop.dropAction() if drop.isAccepted() else Qt.DropAction.IgnoreAction

    real_qdrag = bcd.QDrag
    bcd.QDrag = LiveDrag

    def mouse_drag(src, dst):
        session.clear()
        session["target"] = dst
        L, NM = Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier
        Q.mousePress(win, L, NM, src)
        Q.mouseMove(win, src + QPoint(2, 1))
        Q.mouseMove(win, src + QPoint(QApplication.startDragDistance() + 8, 4))
        Q.mouseRelease(win, L, NM, src + QPoint(QApplication.startDragDistance() + 8, 4))
        t.pump(100)
        return dict(session)

    try:
        # 1. A selected group onto a sidebar deck.
        ok = search("deck:Parent::A", 2)
        t.check("Browse lists Parent::A's 2 cards", ok, br.table.len())
        ids = row_ids()
        select([0, 1])
        dst = at_deck("B")
        t.check("sidebar shows deck B", dst is not None, "")
        if dst is None:
            return
        s = mouse_drag(at_row(1), dst)
        t.note(f"group drag session: {s}")
        t.check("real mouse pull on the selection starts the add-on's drag",
                s.get("started") and s.get("mime") == [bcd.MIME]
                and s.get("selected_at_start") == 2, s)
        moved = t.wait_until(
            lambda: all(int(col.get_card(c).did) == did["B"] for c in ids), timeout=10)
        t.check("both cards now live in Parent::B", moved,
                [int(col.get_card(c).did) for c in ids])
        gone = t.wait_until(lambda: br.table.len() == 0, timeout=10)
        t.check("the Parent::A list no longer shows them", gone, row_ids())
        t.check("the drop says where they went",
                any("2 cards moved to" in x for x in tips), tips)

        # 2. Press-and-pull an unselected row: just that card moves.
        search("deck:Parent::B", 4)
        ids = row_ids()
        select([0])
        s = mouse_drag(at_row(2), at_deck("C"))
        t.note(f"unselected drag session: {s}")
        moved = t.wait_until(lambda: int(col.get_card(ids[2]).did) == did["C"], timeout=10)
        t.check("pulling an unselected row moves that card", moved,
                int(col.get_card(ids[2]).did))
        t.check("the previously selected card stays",
                int(col.get_card(ids[0]).did) == did["B"], int(col.get_card(ids[0]).did))

        # 3. New card onto a review card's row: explained, nothing changes.
        search("deck:Parent::B", 3)
        ids = row_ids()
        col.sched.set_due_date([ids[0]], "5")
        search("deck:Parent::B", 3)
        ids = row_ids()
        rev = [c for c in ids if int(col.get_card(c).type) != 0]
        new = [c for c in ids if int(col.get_card(c).type) == 0]
        before = [(int(col.get_card(c).due), int(col.get_card(c).did)) for c in ids]
        tips.clear()
        select([ids.index(new[0])])
        s = mouse_drag(at_row(ids.index(new[0])), at_row(ids.index(rev[0])))
        t.note(f"review-row drag session: {s}")
        t.pump(300)
        after = [(int(col.get_card(c).due), int(col.get_card(c).did)) for c in ids]
        t.check("a drop on a review card changes nothing", before == after, (before, after))
        t.check("and says why", any("review cards" in x.lower() for x in tips), tips)

        # 4. New card onto a new card's row: repositioned before it.
        search("deck:Parent::C", 3)
        ids = row_ids()
        due = {c: int(col.get_card(c).due) for c in ids}
        first, last = min(ids, key=due.get), max(ids, key=due.get)
        select([ids.index(last)])
        s = mouse_drag(at_row(ids.index(last)), at_row(ids.index(first)))
        t.note(f"reposition drag session: {s}")
        ok = t.wait_until(
            lambda: int(col.get_card(last).due) < int(col.get_card(first).due), timeout=10)
        t.check("a new card dropped on a new card's row now comes before it", ok,
                {c: int(col.get_card(c).due) for c in ids})
    finally:
        bcd.QDrag = real_qdrag
        aqt.utils.tooltip = real_tooltip
