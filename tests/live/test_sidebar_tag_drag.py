"""Live: select, drag and sort tags in the real Browse sidebar.

Request (2026-09-24): "I should also be able to mass select tags and move
them around with dragging and selecting and right clicking same as card,
with hovering above or below a tag a line appears where you can drop it".

Tags the fixture notes (alpha, beta, beta::ant, beta::kid, delta, epsilon,
gamma), opens the real inline Browse and drives the real sidebar with the
real mouse (QTest's QWindow overloads, the entry OS events take):

  1. press on an unselected tag and sweep down: the tag rows crossed are
     selected and no drag starts
  2. press on that selected group and drag it to the top edge of another
     tag: while hovering, an accent insertion line is painted on that
     row's top edge (the marker widget and the viewport's pixels); after
     the drop the sidebar the user sees lists the group just above it, in
     order, and `tag_order` holds the order
  3. the bottom edge: the line is on the bottom edge, the tag lands just
     below the row
  4. a Cmd-click selection dropped on the line above a child tag of
     another parent: the tags are reparented (full names change on the
     notes) and land in order before it
  5. the middle of a row: the row is boxed, not lined, and the drop nests
  6. deck rows: a sweep from a deck is not taken over, and dragging a
     deck onto another still reparents it through Anki's own drop

Offscreen, Qt ends every QDrag at once, so the drag is run by a stand-in
that does what the OS drag session does (as test_browse_sweep_select.py):
from the moment a drag starts it owns the pointer, sends the rest of the
gesture to the widget under it as DragEnter / DragMove / Drop, and eats
the release. The add-on's tag drag uses `sidebar_tags.QDrag`; the deck
drag is Qt's own `startDrag`, which is replaced on the instance with one
that carries the model's own MIME data through the same stand-in.

Fixture: Parent::{A,B,C} with two new Basic cards each, and Solo.
"""

import sys


def _module(suffix):
    for name, mod in list(sys.modules.items()):
        if mod is not None and name.startswith("anki-design") and name.endswith(suffix):
            return mod
    return None


def run(t):
    from aqt.qt import (QApplication, QColor, QDragEnterEvent, QDragLeaveEvent,
                        QDragMoveEvent, QDropEvent, QEvent, QFrame, QModelIndex,
                        QObject, QPoint, QPointF, Qt)

    mw, Q = t.mw, t.QTest
    col = mw.col
    # these flat fixture tags must stay flat: keep the tag organizer off
    _cfg = mw.addonManager.getConfig("anki-design") or {}
    _cfg["auto_organize_tags"] = False
    mw.addonManager.writeConfig("anki-design", _cfg)
    embed = _module(".browse_embed")
    st = _module(".sidebar_tags")
    t.check("sidebar_tags is loaded", st is not None,
            [k for k in sys.modules if k.startswith("anki-design")][:30])

    # --- tags on the fixture notes ------------------------------------------
    nids = sorted({col.get_card(c).nid for c in col.find_cards("")})
    names = ["alpha", "beta", "gamma", "delta", "beta::kid", "beta::ant", "epsilon"]
    for nid, tag in zip(nids, names):
        col.tags.bulk_add([nid], tag)
    note_of = dict(zip(names, nids))

    mw.onBrowse()
    br = t.wait_until(lambda: embed._state.get("browser"), timeout=20)
    t.check("Browse opens inline", br is not None, embed and embed._state)
    if br is None:
        return
    t.wait_until(lambda: embed._state.get("curtain") is None, timeout=10)
    t.pump(200)
    sb = br.sidebar
    top, win = mw, mw.windowHandle()
    L, NM = Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier
    CMD = Qt.KeyboardModifier.ControlModifier
    step = QApplication.startDragDistance() + 8
    watchers = getattr(sb, "_ba_tag_drag", None) or {}
    drop_w = watchers.get("drop")

    # --- tree helpers ---------------------------------------------------------
    def find(full, kind="TAG"):
        m = sb.model()
        if m is None:
            return None

        def walk(parent):
            for r in range(m.rowCount(parent)):
                c = m.index(r, 0, parent)
                it = m.item_for_index(c)
                if it is not None and it.item_type.name == kind and it.full_name == full:
                    return c
                hit = walk(c)
                if hit is not None:
                    return hit
            return None

        return walk(QModelIndex())

    def section():
        m = sb.model()
        for r in range(m.rowCount(QModelIndex())):
            c = m.index(r, 0, QModelIndex())
            if m.item_for_index(c).item_type.name == "TAG_ROOT":
                return c
        return None

    def shown(parent_full):
        """Tag rows under a parent as the user sees them: model order, and
        the on-screen tops must agree."""
        m = sb.model()
        pidx = section() if parent_full == "" else find(parent_full)
        if pidx is None:
            return None
        sb.expand(pidx)
        rows = []
        for r in range(m.rowCount(pidx)):
            c = m.index(r, 0, pidx)
            it = m.item_for_index(c)
            if it.item_type.name == "TAG":
                rows.append((sb.visualRect(c).top(), it.full_name))
        if [n for _, n in sorted(rows)] != [n for _, n in rows]:
            return ["<screen order differs>"] + [n for _, n in sorted(rows)]
        return [n for _, n in rows]

    def row_rect(full, kind="TAG"):
        idx = find(full, kind)
        if idx is None:
            return None
        sb.scrollTo(idx)
        t.pump(20)
        return sb.visualRect(idx)

    def at(full, where="mid", kind="TAG"):
        """A point on a row (mw coordinates): its middle, 2px under its top
        edge, or 2px over its bottom edge."""
        r = row_rect(full, kind)
        if r is None:
            return None
        x = r.left() + min(30, max(4, r.width() // 3))
        y = {"mid": r.center().y(), "top": r.top() + 2, "bottom": r.bottom() - 1}[where]
        return sb.viewport().mapTo(top, QPoint(x, y))

    def selected():
        return sorted(sb._selected_tags())

    def click(full, mods=NM, kind="TAG"):
        idle()
        Q.mouseClick(win, L, mods, at(full, kind=kind))
        t.pump(60)

    def tag_order():
        cfg = mw.addonManager.getConfig("anki-design") or {}
        return cfg.get("tag_order")

    def accent():
        cfg = mw.addonManager.getConfig("anki-design") or {}
        return QColor(cfg.get("accent") or "#6c8cff")

    # --- the OS drag session, offscreen -----------------------------------------
    session = {}
    mouse_types = (QEvent.Type.MouseMove, QEvent.Type.MouseButtonPress,
                   QEvent.Type.MouseButtonRelease)

    class DragLoop(QObject):
        def eventFilter(self, obj, ev):
            return bool(session.get("live")) and ev.type() in mouse_types

    loop_filter = DragLoop()
    QApplication.instance().installEventFilter(loop_filter)

    def drop_widget_at(p):
        w = top.childAt(p)
        while w is not None and not w.acceptDrops():
            w = w.parentWidget()
        return w

    def markers():
        """Visible add-on markers over the sidebar: (name, rect, is line,
        the accent's pixel under it in a grab of the viewport)."""
        out = []
        vp = sb.viewport()
        img = vp.grab().toImage()
        want = accent()
        for f in vp.findChildren(QFrame):
            if not f.isVisible() or not f.objectName().startswith("ba-"):
                continue
            g = f.geometry()
            line = g.height() <= 3
            px = img.pixelColor(g.left() + g.width() // 2, g.top() + (g.height() // 2 if line else 0))
            painted = (abs(px.red() - want.red()) < 40 and abs(px.green() - want.green()) < 40
                       and abs(px.blue() - want.blue()) < 40)
            out.append({"name": f.objectName(), "top": g.top(), "left": g.left(),
                        "w": g.width(), "h": g.height(), "line": line,
                        "painted": painted, "px": px.name()})
        return out

    def run_session(md):
        session["started"] = True
        session["live"] = True
        try:
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
                t.pump(10)
                session["markers"] = markers()
                session["zone"] = getattr(drop_w, "zone", None)
            Q.mouseRelease(win, L, NM, last)
            if entered is None or not move_ok:
                session["dropped"] = False
                return Qt.DropAction.IgnoreAction
            drop = QDropEvent(QPointF(entered.mapFrom(top, last)), acts, md, L, NM)
            QApplication.sendEvent(entered, drop)
            session["dropped"] = drop.isAccepted()
            session["markers_after"] = markers()
            return drop.dropAction() if drop.isAccepted() else Qt.DropAction.IgnoreAction
        finally:
            session["live"] = False

    real_qdrag = st.QDrag if st else None
    if st is not None:
        class LiveDrag(real_qdrag):
            def exec(self, *a, **k):
                session["carried"] = st.decode_tags(self.mimeData())
                return run_session(self.mimeData())

        st.QDrag = LiveDrag

    session_opts = {}

    def deck_start_drag(actions):
        # Qt's own QAbstractItemView::startDrag, with the drag loop stood
        # in for. A drag event made here has no QDrag source, and an
        # InternalMove view refuses enter/move/drop from any other source
        # (Qt checks QDragManager's live drag), so the drop is delivered
        # the way QWidgetWindow would once the view had accepted it: first
        # through every add-on filter on the sidebar (none may claim a
        # deck drag), then to the tree's own dropEvent, which is Anki's
        # SidebarTreeView.dropEvent (reparent_decks).
        session["deck_drag"] = True
        path, session["path"] = session["path"], []
        if session_opts.get("deck_no_drop") or not path:
            return
        md = sb.model().mimeData(sb.selectedIndexes())
        last = path[-1]
        Q.mouseRelease(win, L, NM, last)
        vp = sb.viewport()
        pos = QPointF(vp.mapFrom(top, last))
        acts = Qt.DropAction.MoveAction
        claimed = []
        for f in [drop_w, (getattr(br, "_ba_card_drag", None) or {}).get("sidebar")]:
            if f is None:
                continue
            for etype in ("move", "drop"):
                ev = (QDragMoveEvent(vp.mapFrom(top, last), acts, md, L, NM) if etype == "move"
                      else QDropEvent(pos, acts, md, L, NM))
                if f.eventFilter(vp, ev):
                    claimed.append((type(f).__name__, etype))
        session["claimed"] = claimed
        drop = QDropEvent(pos, acts, md, L, NM)
        sb.dropEvent(drop)
        session["dropped"] = drop.isAccepted()

    sb.startDrag = deck_start_drag

    def idle():
        # an expand animation (sb.expand in `shown`, or a refresh) makes
        # the tree drop presses; the user's hand is slower than that
        t.wait_until(lambda: sb.state() != sb.State.AnimatingState, timeout=5)
        t.pump(30)

    def gesture(src, path, mods=NM):
        idle()
        session.clear()
        session["press"] = src
        session["path"] = [src + QPoint(1, 2)] + list(path)
        last = path[-1]
        Q.mousePress(win, L, mods, src)
        while session["path"]:
            Q.mouseMove(win, session["path"].pop(0))
        if not session.get("started"):
            Q.mouseRelease(win, L, mods, last)
        t.pump(100)
        return dict(session)

    def settle(pred, timeout=10):
        return t.wait_until(pred, timeout=timeout)

    def line_of(s):
        ms = s.get("markers") or []
        return [m for m in ms if m["line"]], [m for m in ms if not m["line"]]

    try:
        ok = settle(lambda: find("beta::kid") is not None)
        t.check("the sidebar lists the fixture tags", ok, "")
        if not ok:
            return
        start = shown("")
        t.check("top-level tags start in Anki's name order",
                start == ["alpha", "beta", "delta", "epsilon", "gamma"], start)
        t.check("beta's children start as ant, kid", shown("beta") == ["beta::ant", "beta::kid"],
                shown("beta"))

        # 1. sweep from an unselected tag ----------------------------------------
        sb.clearSelection()
        t.pump(30)
        s = gesture(at("delta"), [at("epsilon"), at("gamma")])
        t.note(f"sweep session: { {k: v for k, v in s.items() if k != 'markers'} }")
        t.check("sweeping down from an unselected tag selects the tags crossed",
                selected() == ["delta", "epsilon", "gamma"], selected())
        t.check("the sweep starts no drag (the add-on's or the tree's own)",
                not s.get("started") and not s.get("deck_drag"),
                (s.get("started"), s.get("deck_drag")))
        t.check("the sweep renames nothing", "delta" in col.tags.all(), col.tags.all())

        # 2. drag the selected group to the ABOVE line of alpha ---------------
        r_alpha = row_rect("alpha")
        src = at("epsilon")
        s = gesture(src, [src + QPoint(0, step), at("alpha", "top"), at("alpha", "top")])
        t.note(f"above session: carried={s.get('carried')} zone={s.get('zone')} "
               f"markers={s.get('markers')} after={s.get('markers_after')}")
        t.check("pressing on the selected group drags all of it, in shown order",
                s.get("carried") == ["delta", "epsilon", "gamma"], s.get("carried"))
        lines, boxes = line_of(s)
        t.check("hovering alpha's top edge shows one insertion line, no box",
                len(lines) == 1 and not boxes and s.get("zone") == "above", s.get("markers"))
        t.check("the line sits on alpha's top edge",
                bool(lines) and abs(lines[0]["top"] - (r_alpha.top() - 1)) <= 1, (lines, r_alpha))
        t.check("the line is painted in the accent colour",
                bool(lines) and lines[0]["painted"], lines)
        t.check("the drop is accepted and the marker goes away",
                s.get("dropped") and not s.get("markers_after"), (s.get("dropped"), s.get("markers_after")))
        want = ["delta", "epsilon", "gamma", "alpha", "beta"]
        ok = settle(lambda: shown("") == want)
        t.check("after the refresh the sidebar shows the group just above alpha", ok, shown(""))
        t.check("tag_order persists the top-level order",
                (tag_order() or {}).get("") == want, tag_order())

        # 3. BELOW line: alpha just under delta ---------------------------------
        click("alpha")
        t.check("a click selects alpha alone", selected() == ["alpha"], selected())
        r_delta = row_rect("delta")
        src = at("alpha")
        s = gesture(src, [src + QPoint(step, 0), at("delta", "bottom"), at("delta", "bottom")])
        t.note(f"below session: zone={s.get('zone')} markers={s.get('markers')}")
        lines, boxes = line_of(s)
        t.check("hovering delta's bottom edge shows the line on that edge",
                len(lines) == 1 and not boxes and s.get("zone") == "below"
                and abs(lines[0]["top"] - (r_delta.top() + r_delta.height() - 1)) <= 1,
                (s.get("markers"), r_delta))
        want = ["delta", "alpha", "epsilon", "gamma", "beta"]
        ok = settle(lambda: shown("") == want)
        t.check("alpha lands just below delta", ok, shown(""))
        t.check("tag_order follows", (tag_order() or {}).get("") == want, tag_order())

        # 4. across parents: gamma + Cmd-click delta, above beta::ant ------------
        click("gamma")
        click("delta", CMD)
        t.check("Cmd-click extends the selection", selected() == ["delta", "gamma"], selected())
        src = at("gamma")
        s = gesture(src, [src + QPoint(0, -step), at("beta::ant", "top"), at("beta::ant", "top")])
        t.note(f"cross session: carried={s.get('carried')} zone={s.get('zone')}")
        t.check("both travel, in shown order", s.get("carried") == ["delta", "gamma"], s.get("carried"))
        ok = settle(lambda: {"beta::delta", "beta::gamma"} <= set(col.tags.all()))
        t.check("they are reparented under beta (full names change)",
                ok and "delta" not in col.tags.all() and "gamma" not in col.tags.all(), col.tags.all())
        t.check("the notes carry the new names",
                "beta::delta" in col.get_note(note_of["delta"]).tags
                and "beta::gamma" in col.get_note(note_of["gamma"]).tags,
                (col.get_note(note_of["delta"]).tags, col.get_note(note_of["gamma"]).tags))
        want_beta = ["beta::delta", "beta::gamma", "beta::ant", "beta::kid"]
        ok = settle(lambda: shown("beta") == want_beta)
        t.check("the sidebar shows them in order just above beta::ant", ok, shown("beta"))
        t.check("the top level loses them", shown("") == ["alpha", "epsilon", "beta"], shown(""))
        t.check("tag_order holds beta's order", (tag_order() or {}).get("beta") == want_beta, tag_order())

        # 5. the middle of a row nests -------------------------------------------
        click("epsilon")
        src = at("epsilon")
        s = gesture(src, [src + QPoint(step, 0), at("alpha"), at("alpha")])
        lines, boxes = line_of(s)
        t.note(f"nest session: zone={s.get('zone')} markers={s.get('markers')}")
        t.check("the middle of alpha is boxed, not lined",
                len(boxes) == 1 and not lines and s.get("zone") == "into", s.get("markers"))
        ok = settle(lambda: "alpha::epsilon" in col.tags.all() and "epsilon" not in col.tags.all())
        t.check("a middle drop nests epsilon inside alpha", ok, col.tags.all())
        ok = settle(lambda: shown("alpha") == ["alpha::epsilon"])
        t.check("the sidebar shows it under alpha", ok, shown("alpha"))
        t.check("the top level is alpha, beta", settle(lambda: shown("") == ["alpha", "beta"]), shown(""))

        # 6. deck rows keep working ------------------------------------------------
        sweeps = getattr(watchers.get("mouse"), "sweeps", None)
        solo = int(col.decks.id_for_name("Solo"))
        sb.clearSelection()
        t.pump(30)
        session_opts["deck_no_drop"] = True
        s = gesture(at("Parent", kind="DECK"), [at("Solo", kind="DECK")])
        session_opts["deck_no_drop"] = False
        picked = [int(i) for i in sb._selected_decks()]
        t.note(f"deck sweep: deck_drag={s.get('deck_drag')} picked={picked}")
        t.check("a pull down from an unselected deck row is left to the tree: its own drag starts",
                s.get("deck_drag") and getattr(watchers.get("mouse"), "sweeps", None) == sweeps,
                (s.get("deck_drag"), sweeps))
        t.check("and the tree selected just the pressed deck, as before",
                picked == [int(col.decks.id_for_name("Parent"))], picked)
        click("Solo", kind="DECK")
        t.check("Solo deck selected", [int(i) for i in sb._selected_decks()] == [solo],
                sb._selected_decks())
        src = at("Solo", kind="DECK")
        s = gesture(src, [src + QPoint(step, 0), at("Parent", kind="DECK"), at("Parent", kind="DECK")])
        t.note(f"deck drag: {s.get('deck_drag')} claimed={s.get('claimed')} dropped={s.get('dropped')}")
        t.check("the deck drag carries Qt's own item MIME, and no add-on drop filter claims it",
                s.get("deck_drag") and s.get("claimed") == [], s.get("claimed"))
        ok = settle(lambda: col.decks.name(solo) == "Parent::Solo")
        t.check("dragging a deck onto another still reparents it", ok, col.decks.name(solo))
    finally:
        if st is not None:
            st.QDrag = real_qdrag
        try:
            del sb.startDrag
        except Exception:
            pass
        QApplication.instance().removeEventFilter(loop_filter)
