"""Live: reposition cards by dragging them between rows of the real Browse
table, with an insertion line as the drop target.

Ticket 20260923-120519: "the drop target should be an insertion line above
or below the row being hovered, not a box highlight on the card. On
release, every selected card must actually be moved to that position."

The table is sorted by position (Due) so the rows show the new-card queue.
The drag is real (QTest's QWindow mouse overloads reach the table's
viewport and the add-on starts a QDrag); the OS drag session, which
offscreen cannot run, is stood in for by delivering DragEnter / DragMove /
Drop to the widget under the pointer. Between the move and the drop the
test looks at what the user sees over the table: the marker widget the
add-on shows, where it sits, and how tall it is. After the drop it reads
the rows of the re-searched table and the cards' positions.

Fixture: Parent::{A,B,C} with two new Basic cards each, six under Parent.
"""

import sys


def _module(suffix):
    for name, mod in list(sys.modules.items()):
        if mod is not None and name.startswith("anki-design") and name.endswith(suffix):
            return mod
    return None


def run(t):
    from aqt.qt import (QApplication, QDragEnterEvent, QDragMoveEvent,
                        QDropEvent, QFrame, QItemSelectionModel, QPoint,
                        QPointF, Qt)
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
    t.wait_until(lambda: embed._state.get("curtain") is None, timeout=10)
    t.pump(200)
    watchers = getattr(br, "_ba_card_drag", None)
    t.check("card drag is installed on the real Browser",
            isinstance(watchers, dict) and {"source", "table"} <= set(watchers),
            watchers)
    if not watchers:
        return

    view = br.table._view
    vp = view.viewport()
    top = mw
    win = top.windowHandle()

    def search(q, n):
        br.search_for(q)
        return t.wait_until(lambda: br.table.len() == n, timeout=10)

    def row_ids():
        m = br.table._model
        return [int(m.get_card(m.index(r, 0)).id) for r in range(br.table.len())]

    def dues(ids):
        return {c: int(col.get_card(c).due) for c in ids}

    def select(rows):
        smod = view.selectionModel()
        smod.clearSelection()
        for r in rows:
            smod.select(br.table._model.index(r, 0),
                        QItemSelectionModel.SelectionFlag.Select
                        | QItemSelectionModel.SelectionFlag.Rows)
        t.pump(50)

    def row_rect(r):
        idx = br.table._model.index(r, 0)
        view.scrollTo(idx)
        return view.visualRect(idx)

    def at_row(r, frac=0.5):
        rc = row_rect(r)
        p = QPoint(rc.center().x(), rc.top() + int(rc.height() * frac))
        return vp.mapTo(top, p)

    def markers():
        """Every visible marker the add-on has put over the table, as
        (objectName, top, height, viewport row-top the marker aligns with)."""
        out = []
        for f in vp.findChildren(QFrame):
            name = f.objectName()
            if not name.startswith("ba-card-drop") or not f.isVisible():
                continue
            g = f.geometry()
            out.append((name, g.top(), g.height(), g.bottom()))
        return out

    # The OS drag loop, offscreen: deliver the drag where the pointer is,
    # and look at the table between the move and the drop.
    session = {}

    class LiveDrag(bcd.QDrag):
        def exec(self, *a, **k):
            session["started"] = True
            session["carried"] = bcd.decode_cards(self.mimeData())
            pos = session["target"]
            w = top.childAt(pos)
            while w is not None and not w.acceptDrops():
                w = w.parentWidget()
            if w is None:
                return Qt.DropAction.IgnoreAction
            local = w.mapFrom(top, pos)
            md = self.mimeData()
            acts = Qt.DropAction.MoveAction
            btn, mod = Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier
            enter = QDragEnterEvent(local, acts, md, btn, mod)
            QApplication.sendEvent(w, enter)
            if not enter.isAccepted():
                return Qt.DropAction.IgnoreAction
            move = QDragMoveEvent(local, acts, md, btn, mod)
            QApplication.sendEvent(w, move)
            session["move_accepted"] = move.isAccepted()
            t.pump(50)
            session["markers"] = markers()
            if not move.isAccepted():
                return Qt.DropAction.IgnoreAction
            drop = QDropEvent(QPointF(local), acts, md, btn, mod)
            QApplication.sendEvent(w, drop)
            session["dropped"] = drop.isAccepted()
            t.pump(50)
            session["markers_after"] = markers()
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

    def settled(expect_ids):
        """The table has re-searched and shows `expect_ids` top to bottom."""
        return t.wait_until(lambda: row_ids() == expect_ids, timeout=10)

    def line_marker(s):
        ms = s.get("markers") or []
        lines = [m for m in ms if m[2] <= 4]
        boxes = [m for m in ms if m[2] > 4]
        return lines, boxes

    try:
        # Show the queue order: sorted by position, ascending.
        br.table._state.sort_column = "cardDue"
        br.table._state.sort_backwards = False
        ok = search("deck:Parent", 6)
        t.check("Browse lists the 6 new cards under Parent", ok, br.table.len())
        ids = row_ids()
        d = dues(ids)
        t.check("the table shows them in queue order",
                ids == sorted(ids, key=d.get), (ids, d))
        if ids != sorted(ids, key=d.get):
            return
        rh = row_rect(0).height()

        # 1. Hover the upper half of row 3 with rows 4 and 5 in hand: a thin
        #    line along row 3's top edge, no box around the row.
        select([4, 5])
        s = mouse_drag(at_row(5), at_row(3, 0.25))
        t.note(f"drop-above session: {s}")
        t.check("pulling the selection starts a drag carrying both cards",
                s.get("started") and sorted(s.get("carried") or []) == sorted(ids[4:]), s)
        lines, boxes = line_marker(s)
        top3 = row_rect(3).top()
        t.check("hovering the upper half of a row shows a thin insertion line",
                len(lines) == 1, s.get("markers"))
        t.check("and no box highlight on the card", not boxes, s.get("markers"))
        t.check("the line sits on the hovered row's top edge",
                lines and abs(lines[0][1] + lines[0][2] / 2 - top3) <= 2,
                (lines, top3, rh))
        t.check("the marker is gone once the drop lands",
                not s.get("markers_after"), s.get("markers_after"))
        want = ids[:3] + ids[4:] + [ids[3]]
        t.check("both cards now sit just above that row, in order",
                settled(want), (row_ids(), want))
        t.check("the drop says how many cards moved",
                any("2 cards repositioned" in x for x in tips), tips)

        # 2. Hover the lower half of a row: the line is on its bottom edge and
        #    the cards land just below it.
        ids = row_ids()
        tips.clear()
        select([0])
        s = mouse_drag(at_row(0), at_row(2, 0.8))
        t.note(f"drop-below session: {s}")
        lines, boxes = line_marker(s)
        bottom2 = row_rect(2).bottom()
        t.check("hovering the lower half of a row shows a thin insertion line",
                len(lines) == 1 and not boxes, s.get("markers"))
        t.check("the line sits on the hovered row's bottom edge",
                lines and abs(lines[0][1] + lines[0][2] / 2 - bottom2) <= 2,
                (lines, bottom2, rh))
        want = ids[1:3] + [ids[0]] + ids[3:]
        t.check("the card now sits just below that row",
                settled(want), (row_ids(), want))
        t.check("the drop says the card moved",
                any("1 card repositioned" in x for x in tips), tips)

        # 3. Every selected card moves, including one that is the hovered row
        #    itself: rows 0, 2 and 5 dropped above row 2 gather there, in
        #    their order, and the rest close up around them.
        ids = row_ids()
        tips.clear()
        select([0, 2, 5])
        s = mouse_drag(at_row(5), at_row(2, 0.25))
        t.note(f"gather session: {s}")
        t.check("the drag carries all three selected cards",
                sorted(s.get("carried") or []) == sorted([ids[0], ids[2], ids[5]]), s)
        want = [ids[1], ids[0], ids[2], ids[5], ids[3], ids[4]]
        t.check("all three selected cards land at the line, in order",
                settled(want), (row_ids(), want))
        d = dues(ids)
        t.check("their queue positions are consecutive",
                d[ids[2]] == d[ids[0]] + 1 and d[ids[5]] == d[ids[2]] + 1, d)
        t.check("the drop says three cards moved",
                any("3 cards repositioned" in x for x in tips), tips)

        # 4. Dropping a card onto its own row changes nothing and says nothing.
        ids = row_ids()
        tips.clear()
        before = dues(ids)
        select([1])
        s = mouse_drag(at_row(1), at_row(1, 0.7))
        t.pump(300)
        t.check("a drop on the card's own row moves nothing",
                row_ids() == ids and dues(ids) == before, (row_ids(), ids))
        t.check("and reports nothing moved",
                not any("repositioned" in x for x in tips), tips)
    finally:
        bcd.QDrag = real_qdrag
        aqt.utils.tooltip = real_tooltip
