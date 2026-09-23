"""Live: drop cards between two rows that share a new-card position.

Ticket 20260923-121900: "I just tried to move 20a and 20b for after 19 and
before 21 and they didn't actually move." In the user's deck nearly every
card sits at New #25 (E-Q19 and E-Q21 both), and a reposition to the
target's position, or the one after it, cannot land between two cards
that share one: the dragged cards came out in front of the whole tied run
(the rows they already held) or behind all of it.

The real Browse table is sorted by position, cards are given tied
positions through the collection, and the drag is the real mouse gesture
with the OS drag session stood in for by delivering DragEnter / DragMove /
Drop to the widget under the pointer (as test_browse_card_reposition.py
does). After the drop the re-searched rows and the cards' positions are
what the user would see.

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
                        QDropEvent, QItemSelectionModel, QPoint, QPointF, Qt)
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

    def set_dues(mapping):
        """Give cards positions directly, ties included, the way a deck
        import or a bulk reposition to one number leaves them."""
        for cid, due in mapping.items():
            card = col.get_card(cid)
            card.due = int(due)
            col.update_card(card)

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
            t.pump(50)
            if not move.isAccepted():
                return Qt.DropAction.IgnoreAction
            drop = QDropEvent(QPointF(local), acts, md, btn, mod)
            QApplication.sendEvent(w, drop)
            session["dropped"] = drop.isAccepted()
            t.pump(50)
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
        return t.wait_until(lambda: row_ids() == expect_ids, timeout=10)

    def strictly_increasing(ids):
        d = dues(ids)
        vals = [d[c] for c in ids]
        return all(a < b for a, b in zip(vals, vals[1:])), vals

    try:
        br.table._state.sort_column = "cardDue"
        br.table._state.sort_backwards = False
        ok = search("deck:Parent", 6)
        t.check("Browse lists the 6 new cards under Parent", ok, br.table.len())
        base = row_ids()
        # Named after the user's rows: 16 and 19 ahead, 20a and 20b next,
        # then E-Q19 and E-Q21 both at #25.
        a0, a1, b0, b1, c0, c1 = base
        solo = [int(c) for c in col.find_cards("deck:Solo")]
        solo_before = dues(solo)

        # 1. The ticket: 20a and 20b dropped on the line between two cards
        #    that share position 25 (lower half of the first of them).
        set_dues({a0: 16, a1: 19, b0: 23, b1: 24, c0: 25, c1: 25})
        search("deck:Parent", 6)
        t.pump(100)
        ids = row_ids()
        want_start = [a0, a1, b0, b1, c0, c1]
        t.check("the table shows the tied cards after the pair, in id order",
                ids == want_start, (ids, want_start, dues(ids)))
        if ids != want_start:
            return
        tips.clear()
        select([2, 3])
        s = mouse_drag(at_row(3), at_row(4, 0.8))
        t.note(f"between-tied session: {s}")
        t.check("pulling the pair starts a drag carrying both cards",
                s.get("started") and sorted(s.get("carried") or []) == sorted([b0, b1]), s)
        t.check("the drop is accepted", s.get("dropped"), s)
        want = [a0, a1, c0, b0, b1, c1]
        t.check("the pair now sits between the two tied cards",
                settled(want), (row_ids(), want, dues(row_ids())))
        inc, vals = strictly_increasing(row_ids())
        t.check("every card in the list has a position of its own", inc, vals)
        t.check("the drop says the two cards moved",
                any("2 cards repositioned" in x for x in tips), tips)

        # 2. The other edge: a single card dropped on the upper half of the
        #    second tied card. Before the fix the whole tied run slid back
        #    and the card took the front, leaving the list exactly as it
        #    was: "they didn't actually move".
        set_dues({a0: 16, a1: 19, b0: 23, b1: 24, c0: 25, c1: 25})
        search("deck:Parent", 6)
        t.pump(100)
        t.check("tied rows again", row_ids() == want_start, row_ids())
        tips.clear()
        select([3])
        s = mouse_drag(at_row(3), at_row(5, 0.25))
        t.note(f"above-second-tied session: {s}")
        want = [a0, a1, b0, c0, b1, c1]
        t.check("the card now sits just above the second tied card",
                settled(want), (row_ids(), want, dues(row_ids())))
        inc, vals = strictly_increasing(row_ids())
        t.check("positions are distinct after that drop too", inc, vals)
        t.check("the drop says one card moved",
                any("1 card repositioned" in x for x in tips), tips)

        # 3. The user's deck: everything at one position. A card from the
        #    top dropped below the fifth row lands there, the others keep
        #    their order, and the message counts the dragged card only.
        set_dues({a0: 25, a1: 25, b0: 25, b1: 25, c0: 25, c1: 25})
        search("deck:Parent", 6)
        t.pump(100)
        ids = row_ids()
        t.check("six cards tied at one position list in id order",
                ids == want_start, (ids, dues(ids)))
        tips.clear()
        select([0])
        s = mouse_drag(at_row(0), at_row(4, 0.8))
        t.note(f"all-tied session: {s}")
        want = [a1, b0, b1, c0, a0, c1]
        t.check("the card lands below the fifth row of the tied run",
                settled(want), (row_ids(), want, dues(row_ids())))
        inc, vals = strictly_increasing(row_ids())
        t.check("the run is numbered in the order shown", inc, vals)
        t.check("the message counts the dragged card, not the whole run",
                any("1 card repositioned" in x for x in tips)
                and not any("6 cards" in x for x in tips), tips)
        t.check("cards outside the table ahead of the run keep their positions",
                dues(solo) == solo_before, (dues(solo), solo_before))
    finally:
        bcd.QDrag = real_qdrag
        aqt.utils.tooltip = real_tooltip
