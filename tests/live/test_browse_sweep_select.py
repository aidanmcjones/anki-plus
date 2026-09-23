"""Live: sweep-select a group of cards in the real Browse table, then drag
the group.

Ticket 20260923-104846: "I cannot group-select cards by dragging and
selecting at the same time to group a bunch of cards and move them all."

Anki's table selects a run of rows when you press on one and sweep down
(or up) the list with the button held. The add-on's card-drag filter took
that gesture for itself: it armed on every row and started a QDrag the
moment the cursor moved past the drag threshold, so the sweep selected
nothing. This test drives the gesture with the real mouse (QTest's QWindow
overloads, the same entry OS events take) against the real table inside
the inline Browse and reads back what the user sees: the selection, then
where the cards live after the group is dragged to a sidebar deck.

Offscreen, Qt ends every QDrag at once, so the add-on's QDrag.exec is
swapped for a stand-in that does what the OS drag session does: from the
moment the drag starts it owns the pointer, feeding the rest of the
gesture to the widget under it as DragEnter / DragMove / Drop and eating
the release (as Qt's own QBasicDrag does, with a filter on the
application), so no move of that gesture reaches the table as a mouse
move. That is exactly why a sweep that starts a drag cannot select
anything.

Fixture: Parent::{A,B,C} with two new Basic cards each, and Solo.
"""

import sys


def _module(suffix):
    for name, mod in list(sys.modules.items()):
        if mod is not None and name.startswith("anki-design") and name.endswith(suffix):
            return mod
    return None


def run(t):
    from aqt.qt import (QApplication, QDragEnterEvent, QDragLeaveEvent,
                        QDragMoveEvent, QDropEvent, QEvent, QModelIndex,
                        QObject, QPoint, QPointF, Qt)

    mw, Q = t.mw, t.QTest
    col = mw.col
    bcd = _module(".browse_card_drag")
    embed = _module(".browse_embed")
    t.check("browse_card_drag and browse_embed are loaded", bcd and embed,
            [k for k in sys.modules if k.startswith("anki-design")][:20])
    if not (bcd and embed):
        return

    mw.onBrowse()
    br = t.wait_until(lambda: embed._state.get("browser"), timeout=20)
    t.check("Browse opens inline", br is not None, embed._state)
    if br is None:
        return
    # The embed keeps an anti-flash curtain over itself for ~0.9 s after
    # opening; the mouse has to wait for it, as the user does.
    gone = t.wait_until(lambda: embed._state.get("curtain") is None, timeout=10)
    t.check("the opening curtain is down", gone, embed._state.get("curtain"))
    t.pump(200)
    watchers = getattr(br, "_ba_card_drag", None)
    t.check("card drag is installed on the real Browser",
            isinstance(watchers, dict) and "source" in watchers, watchers)
    if not watchers:
        return

    view, sb = br.table._view, br.sidebar
    top = mw
    win = top.windowHandle()
    solo = int(col.decks.id_for_name("Solo"))
    L, NM = Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier
    step = QApplication.startDragDistance() + 8

    def search(q, n):
        br.search_for(q)
        return t.wait_until(lambda: br.table.len() == n, timeout=10)

    def row_ids():
        m = br.table._model
        return [int(m.get_card(m.index(r, 0)).id) for r in range(br.table.len())]

    def at_row(r):
        idx = br.table._model.index(r, 0)
        view.scrollTo(idx)
        return view.viewport().mapTo(top, view.visualRect(idx).center())

    def at_deck(full_name):
        m = sb.model()

        def walk(parent):
            for r in range(m.rowCount(parent)):
                c = m.index(r, 0, parent)
                it = m.item_for_index(c)
                if it is not None and it.full_name == full_name:
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

    def drop_widget_at(p):
        w = top.childAt(p)
        while w is not None and not w.acceptDrops():
            w = w.parentWidget()
        return w

    # The OS drag session, offscreen. `session["path"]` holds the points
    # the gesture has not reached yet; a drag that starts consumes them,
    # and while it is live no mouse event gets past the application.
    session = {}
    mouse_types = (QEvent.Type.MouseMove, QEvent.Type.MouseButtonPress,
                   QEvent.Type.MouseButtonRelease)

    class DragLoop(QObject):
        def eventFilter(self, obj, ev):
            return bool(session.get("live")) and ev.type() in mouse_types

    loop_filter = DragLoop()
    QApplication.instance().installEventFilter(loop_filter)

    class LiveDrag(bcd.QDrag):
        def exec(self, *a, **k):
            session["started"] = True
            session["carried"] = bcd.decode_cards(self.mimeData())
            session["live"] = True
            try:
                return self._session()
            finally:
                session["live"] = False

        def _session(self):
            md = self.mimeData()
            acts = Qt.DropAction.MoveAction
            entered, last, move_ok = None, session["press"], False
            while session["path"]:
                p = session["path"].pop(0)
                last = p
                w = drop_widget_at(p)
                if w is not entered:
                    if entered is not None:
                        QApplication.sendEvent(entered, QDragLeaveEvent())
                    entered, move_ok = None, False
                    if w is None:
                        continue
                    enter = QDragEnterEvent(w.mapFrom(top, p), acts, md, L, NM)
                    QApplication.sendEvent(w, enter)
                    if not enter.isAccepted():
                        continue
                    entered = w
                move = QDragMoveEvent(entered.mapFrom(top, p), acts, md, L, NM)
                QApplication.sendEvent(entered, move)
                move_ok = move.isAccepted()
            # The button comes up inside the drag session: the drag takes
            # the release, the table never sees it.
            Q.mouseRelease(win, L, NM, last)
            if entered is None or not move_ok:
                return Qt.DropAction.IgnoreAction
            drop = QDropEvent(QPointF(entered.mapFrom(top, last)), acts, md, L, NM)
            QApplication.sendEvent(entered, drop)
            session["dropped"] = drop.isAccepted()
            return drop.dropAction() if drop.isAccepted() else Qt.DropAction.IgnoreAction

    real_qdrag = bcd.QDrag
    bcd.QDrag = LiveDrag

    def gesture(src, path):
        """Press at src, move through path, release at its end. Every move
        is a real mouse move until a drag starts; from then on the drag
        session has the rest of the gesture, release included."""
        session.clear()
        session["press"] = src
        session["path"] = [src + QPoint(1, 2)] + list(path)
        last = path[-1]
        Q.mousePress(win, L, NM, src)
        while session["path"]:
            Q.mouseMove(win, session["path"].pop(0))
        if not session.get("started"):
            Q.mouseRelease(win, L, NM, last)
        t.pump(100)
        return dict(session)

    try:
        ok = search("deck:Parent", 6)
        t.check("Browse lists the 6 cards under Parent", ok, br.table.len())
        ids = row_ids()
        view.selectionModel().clearSelection()
        t.pump(50)

        # 1. Press on an unselected row and sweep down the list: the rows
        #    the cursor crosses become the selection, and nothing is dragged.
        s = gesture(at_row(0), [at_row(1), at_row(2), at_row(3)])
        t.note(f"sweep session: {s}")
        picked = [int(c) for c in br.table.get_selected_card_ids()]
        t.check("sweeping down from an unselected row selects the rows crossed",
                sorted(picked) == sorted(ids[:4]), (picked, ids))
        t.check("the sweep starts no card drag", not s.get("started"), s)
        t.check("the sweep moves no card",
                row_ids() == ids
                and all(int(col.get_card(c).did) != solo for c in ids), (row_ids(), ids))
        if sorted(picked) != sorted(ids[:4]):
            return

        # 2. Press on the selected group and pull it onto a sidebar deck:
        #    every swept card moves there.
        dst = at_deck("Solo")
        t.check("sidebar shows deck Solo", dst is not None, "")
        if dst is None:
            return
        src = at_row(2)
        s = gesture(src, [src + QPoint(-step, 3), dst])
        t.note(f"group drag session: {s}")
        t.check("pulling the swept group starts a drag carrying all of it",
                s.get("started") and sorted(s.get("carried") or []) == sorted(ids[:4]), s)
        moved = t.wait_until(
            lambda: all(int(col.get_card(c).did) == solo for c in ids[:4]), timeout=10)
        t.check("all four swept cards now live in Solo", moved,
                [int(col.get_card(c).did) for c in ids[:4]])
        left = t.wait_until(lambda: br.table.len() == 2, timeout=10)
        t.check("the Parent list keeps only the two cards not swept",
                left and sorted(row_ids()) == sorted(ids[4:]), row_ids())

        # 3. A sideways pull on an unselected row still drags that one card
        #    in one motion (20260923-101011).
        ids = row_ids()
        view.selectionModel().clearSelection()
        t.pump(50)
        src = at_row(1)
        s = gesture(src, [src + QPoint(step, 4), dst])
        t.note(f"sideways session: {s}")
        t.check("a sideways pull on an unselected row drags just that card",
                s.get("started") and s.get("carried") == [ids[1]], s)
        moved = t.wait_until(lambda: int(col.get_card(ids[1]).did) == solo, timeout=10)
        t.check("and it moves to Solo while its neighbour stays",
                moved and int(col.get_card(ids[0]).did) != solo,
                [int(col.get_card(c).did) for c in ids])
    finally:
        bcd.QDrag = real_qdrag
        QApplication.instance().removeEventFilter(loop_filter)
