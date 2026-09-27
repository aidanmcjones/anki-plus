"""Live: select, drag and sort decks in the real Browse sidebar, sharing the
home deck list's custom order.

Request (2026-09-24), after tags got the card rules: "now apply these same
changes to the decks section above it".

Adds top-level decks Alpha, Mid, Zulu and Other::{X, Y} to the fixture,
opens the real inline Browse and drives the real sidebar with the real
mouse (QTest's QWindow overloads, the entry OS events take):

  1. press on an unselected deck and sweep down: the deck rows crossed
     are selected, no drag starts (the add-on's or the tree's own), no
     deck moves
  2. press on that selected group and drag it to the top edge of Zulu: an
     accent insertion line is painted on Zulu's top edge (the marker
     widget and the viewport's pixels); after the drop the sidebar lists
     the group just above Zulu in shown order, `deck_order["0"]` holds
     the ids, and the home list's payload follows
  3. one deck, pulled sideways, onto Mid's bottom edge: lands just below
  4. a Cmd-click pair onto the line above Parent::B: reparented under
     Parent (ids kept, names change), in order just before B; Parent's
     `deck_order` entry holds them and the top level's entry drops them
  5. a parent dragged with its own child onto the line above Solo: the
     child stays inside the parent, which moves as one
  6. refused: Parent onto the line above its own child Parent::A, and into
     the middle of Parent::A: no marker, nothing changes
  7. the middle of a row is boxed, not lined, and nests
  8. the Decks header moves a subdeck to the top level
  9. after one more, order-only, line drop, the real home deck list shown
     when Browse closes (no refresh by hand) has the order the sidebar
     shows

Offscreen, Qt ends every QDrag at once, so the drag is run by a stand-in
that does what the OS drag session does (as test_sidebar_tag_drag.py):
from the moment a drag starts it owns the pointer, sends the rest of the
gesture to the widget under it as DragEnter / DragMove / Drop, and eats
the release. The tree's own startDrag is replaced with a tripwire that
records it was called and drops nothing.

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
    embed = _module(".browse_embed")
    st = _module(".sidebar_tags")
    sd = _module(".sidebar_decks")
    pkg = sys.modules.get("anki-design")
    t.check("sidebar_decks is loaded", sd is not None and st is not None,
            [k for k in sys.modules if k.startswith("anki-design")][:40])

    for name in ("Alpha", "Mid", "Zulu", "Other::X", "Other::Y"):
        col.decks.id(name)
    did = {n: int(col.decks.id_for_name(n)) for n in
           ("Alpha", "Mid", "Zulu", "Other", "Other::X", "Other::Y", "Parent",
            "Parent::A", "Parent::B", "Parent::C", "Solo")}

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
    def find_did(d):
        m = sb.model()
        if m is None:
            return None

        def walk(parent):
            for r in range(m.rowCount(parent)):
                c = m.index(r, 0, parent)
                it = m.item_for_index(c)
                if it is not None and it.item_type.name == "DECK" and int(it.id) == int(d):
                    return c
                hit = walk(c)
                if hit is not None:
                    return hit
            return None

        return walk(QModelIndex())

    def find(name):
        return find_did(did[name])

    def section():
        m = sb.model()
        for r in range(m.rowCount(QModelIndex())):
            c = m.index(r, 0, QModelIndex())
            if m.item_for_index(c).item_type.name == "DECK_ROOT":
                return c
        return None

    def name_of(d):
        return col.decks.name(d)

    def shown(parent_name):
        """Deck rows under a parent as the user sees them (leaf names):
        model order, and the on-screen tops must agree."""
        m = sb.model()
        pidx = section() if parent_name == "" else find(parent_name)
        if pidx is None:
            return None
        sb.expand(pidx)
        rows = []
        for r in range(m.rowCount(pidx)):
            c = m.index(r, 0, pidx)
            it = m.item_for_index(c)
            if it.item_type.name == "DECK":
                rows.append((sb.visualRect(c).top(), it.name))
        rows = [r for r in rows if r[1] != "Default"]
        if [n for _, n in sorted(rows)] != [n for _, n in rows]:
            return ["<screen order differs>"] + [n for _, n in sorted(rows)]
        return [n for _, n in rows]

    def row_rect(name):
        idx = find(name)
        if idx is None:
            return None
        if idx.parent().isValid() and not sb.isExpanded(idx.parent()):
            sb.expand(idx.parent())
            idle()
        sb.scrollTo(idx)
        t.pump(20)
        return sb.visualRect(idx)

    def header_point():
        idx = section()
        sb.scrollTo(idx)
        t.pump(20)
        r = sb.visualRect(idx)
        return sb.viewport().mapTo(top, QPoint(r.left() + 30, r.center().y()))

    def at(name, where="mid"):
        r = row_rect(name)
        if r is None:
            return None
        x = r.left() + min(30, max(4, r.width() // 3))
        y = {"mid": r.center().y(), "top": r.top() + 2, "bottom": r.bottom() - 1}[where]
        return sb.viewport().mapTo(top, QPoint(x, y))

    def selected():
        return sorted(name_of(d) for d in sb._selected_decks())

    def click(name, mods=NM):
        idle()
        Q.mouseClick(win, L, mods, at(name))
        t.pump(60)

    def deck_order():
        cfg = mw.addonManager.getConfig("anki-design") or {}
        return cfg.get("deck_order") or {}

    def ids(*names):
        return [did[n] for n in names]

    def home_top_level():
        """Top-level rows of the payload the home deck list renders."""
        rows = pkg._full_deck_tree_payload()
        return [r["name"] for r in rows if r["depth"] == 0 and r["name"] != "Default"]

    def home_children(parent_path):
        rows = pkg._full_deck_tree_payload()
        return [r["name"] for r in rows
                if r["path"].rsplit("::", 1)[0] == parent_path and "::" in r["path"]]

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
                if sd is not None:
                    session["carried"] = [name_of(d) for d in sd.decode_decks(self.mimeData())]
                return run_session(self.mimeData())

        st.QDrag = LiveDrag

    def tree_start_drag(actions):
        # the tree's own drag: must never start on a deck row now
        session["tree_drag"] = True
        session["path"] = []

    sb.startDrag = tree_start_drag

    def idle():
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

    def refresh_sidebar():
        sb.refresh()
        t.pump(150)

    try:
        ok = settle(lambda: find("Other::Y") is not None)
        t.check("the sidebar lists the new decks", ok, "")
        if not ok:
            return
        start = shown("")
        t.check("top-level decks start in Anki's name order",
                start == ["Alpha", "Mid", "Other", "Parent", "Solo", "Zulu"], start)

        # 1. sweep from an unselected deck ----------------------------------------
        names_before = sorted(n.name for n in col.decks.all_names_and_ids())
        sb.clearSelection()
        t.pump(30)
        s = gesture(at("Alpha"), [at("Mid"), at("Other")])
        t.note(f"sweep session: { {k: v for k, v in s.items() if k not in ('markers', 'press')} }")
        t.check("sweeping down from an unselected deck selects the decks crossed",
                selected() == ["Alpha", "Mid", "Other"], selected())
        t.check("the sweep starts no drag (the add-on's or the tree's own)",
                not s.get("started") and not s.get("tree_drag"),
                (s.get("started"), s.get("tree_drag")))
        t.check("the sweep moves no deck",
                sorted(n.name for n in col.decks.all_names_and_ids()) == names_before, "")

        # 2. drag the selected group to the ABOVE line of Zulu --------------------
        r_zulu = row_rect("Zulu")
        src = at("Mid")
        s = gesture(src, [src + QPoint(0, step), at("Zulu", "top"), at("Zulu", "top")])
        t.note(f"above session: carried={s.get('carried')} zone={s.get('zone')} "
               f"markers={s.get('markers')} after={s.get('markers_after')}")
        t.check("pressing on the selected group drags all of it, in shown order",
                s.get("carried") == ["Alpha", "Mid", "Other"] and not s.get("tree_drag"),
                (s.get("carried"), s.get("tree_drag")))
        lines, boxes = line_of(s)
        t.check("hovering Zulu's top edge shows one insertion line, no box",
                len(lines) == 1 and not boxes and s.get("zone") == "above", s.get("markers"))
        t.check("the line sits on Zulu's top edge",
                bool(lines) and r_zulu is not None and abs(lines[0]["top"] - (r_zulu.top() - 1)) <= 1,
                (lines, r_zulu))
        t.check("the line is painted in the accent colour", bool(lines) and lines[0]["painted"], lines)
        t.check("the drop is accepted and the marker goes away",
                s.get("dropped") and not s.get("markers_after"),
                (s.get("dropped"), s.get("markers_after")))
        want = ["Parent", "Solo", "Alpha", "Mid", "Other", "Zulu"]
        ok = settle(lambda: shown("") == want)
        t.check("after the refresh the sidebar shows the group just above Zulu", ok, shown(""))
        order0 = [int(x) for x in deck_order().get("0", []) if int(x) in did.values()]
        t.check("deck_order['0'] (the home list's own order) holds it",
                order0 == ids(*want), (deck_order(), ids(*want)))
        t.check("the home deck list's payload follows", home_top_level() == want, home_top_level())
        refresh_sidebar()
        t.check("a fresh sidebar build keeps the order", shown("") == want, shown(""))

        # 3. BELOW line: Alpha just under Mid --------------------------------------
        click("Alpha")
        t.check("a click selects Alpha alone", selected() == ["Alpha"], selected())
        r_mid = row_rect("Mid")
        src = at("Alpha")
        s = gesture(src, [src + QPoint(step, 0), at("Mid", "bottom"), at("Mid", "bottom")])
        t.note(f"below session: carried={s.get('carried')} zone={s.get('zone')} markers={s.get('markers')}")
        lines, boxes = line_of(s)
        t.check("a sideways pull drags just Alpha", s.get("carried") == ["Alpha"], s.get("carried"))
        t.check("hovering Mid's bottom edge shows the line on that edge",
                len(lines) == 1 and not boxes and s.get("zone") == "below"
                and abs(lines[0]["top"] - (r_mid.top() + r_mid.height() - 1)) <= 1,
                (s.get("markers"), r_mid))
        want = ["Parent", "Solo", "Mid", "Alpha", "Other", "Zulu"]
        ok = settle(lambda: shown("") == want)
        t.check("Alpha dropped on Mid's bottom line lands just below Mid", ok, shown(""))
        t.check("deck_order['0'] follows",
                [int(x) for x in deck_order().get("0", []) if int(x) in did.values()] == ids(*want),
                deck_order())
        t.check("and the home payload", home_top_level() == want, home_top_level())

        # 4. across parents: Zulu + Cmd-click Mid, above Parent::B -----------------
        click("Zulu")
        click("Mid", CMD)
        t.check("Cmd-click extends the selection", selected() == ["Mid", "Zulu"], selected())
        src = at("Zulu")
        s = gesture(src, [src + QPoint(0, -step), at("Parent::B", "top"), at("Parent::B", "top")])
        t.note(f"cross session: carried={s.get('carried')} zone={s.get('zone')}")
        t.check("both travel, in shown order", s.get("carried") == ["Mid", "Zulu"], s.get("carried"))
        ok = settle(lambda: name_of(did["Mid"]) == "Parent::Mid" and name_of(did["Zulu"]) == "Parent::Zulu")
        t.check("they are reparented under Parent (same ids, new names)", ok,
                (name_of(did["Mid"]), name_of(did["Zulu"])))
        want_p = ["A", "Mid", "Zulu", "B", "C"]
        ok = settle(lambda: shown("Parent") == want_p)
        t.check("the sidebar shows them in order just above B", ok, shown("Parent"))
        want = ["Parent", "Solo", "Alpha", "Other"]
        t.check("the top level loses them", shown("") == want, shown(""))
        t.check("deck_order holds Parent's new order",
                [int(x) for x in deck_order().get(str(did["Parent"]), [])]
                == ids("Parent::A", "Mid", "Zulu", "Parent::B", "Parent::C"), deck_order())
        t.check("and the top level's entry no longer lists them",
                not ({did["Mid"], did["Zulu"]} & {int(x) for x in deck_order().get("0", [])}),
                deck_order().get("0"))
        t.check("the home payload shows Parent's children in the same order",
                home_children("Parent") == want_p, home_children("Parent"))

        # 5. a parent dragged with its own child moves as one ----------------------
        click("Other")
        click("Other::X", CMD)
        t.check("Other and Other::X selected", selected() == ["Other", "Other::X"], selected())
        src = at("Other")
        s = gesture(src, [src + QPoint(0, -step), at("Solo", "top"), at("Solo", "top")])
        t.note(f"parent+child session: carried={s.get('carried')} zone={s.get('zone')}")
        want = ["Parent", "Other", "Solo", "Alpha"]
        ok = settle(lambda: shown("") == want)
        t.check("the parent lands just above Solo", ok, shown(""))
        t.check("its child stays inside it, not beside it",
                name_of(did["Other::X"]) == "Other::X" and shown("Other") == ["X", "Y"],
                (name_of(did["Other::X"]), shown("Other")))

        # 6. refusals: a deck into its own subtree -----------------------------------
        click("Parent")
        before = sorted(n.name for n in col.decks.all_names_and_ids())
        src = at("Parent")
        s = gesture(src, [src + QPoint(step, 0), at("Parent::A", "top"), at("Parent::A", "top")])
        t.check("Parent over the line above its own child: no marker, no drop",
                not s.get("markers") and not s.get("dropped") and s.get("zone") is None,
                (s.get("markers"), s.get("zone"), s.get("dropped")))
        s = gesture(src, [src + QPoint(step, 0), at("Parent::A"), at("Parent::A")])
        t.check("Parent into the middle of its own child: refused too",
                not s.get("markers") and not s.get("dropped"), (s.get("markers"), s.get("dropped")))
        t.pump(300)
        t.check("nothing moved", sorted(n.name for n in col.decks.all_names_and_ids()) == before, "")

        # 7. the middle of a row nests ------------------------------------------------
        click("Alpha")
        src = at("Alpha")
        s = gesture(src, [src + QPoint(step, 0), at("Solo"), at("Solo")])
        lines, boxes = line_of(s)
        t.note(f"nest session: zone={s.get('zone')} markers={s.get('markers')}")
        t.check("the middle of Solo is boxed, not lined",
                len(boxes) == 1 and not lines and s.get("zone") == "into" and boxes[0]["painted"],
                s.get("markers"))
        ok = settle(lambda: name_of(did["Alpha"]) == "Solo::Alpha")
        t.check("a middle drop nests Alpha inside Solo", ok, name_of(did["Alpha"]))
        ok = settle(lambda: shown("Solo") == ["Alpha"])
        t.check("the sidebar shows it under Solo", ok, shown("Solo"))

        # 8. the Decks header: to the top level ----------------------------------------
        click("Zulu")
        src = at("Zulu")
        s = gesture(src, [src + QPoint(step, 0), header_point(), header_point()])
        t.note(f"header session: zone={s.get('zone')} dropped={s.get('dropped')}")
        ok = settle(lambda: name_of(did["Zulu"]) == "Zulu")
        t.check("dropped on the Decks header, Parent::Zulu goes to the top level", ok,
                name_of(did["Zulu"]))
        t.check("and Parent's entry forgets it",
                did["Zulu"] not in [int(x) for x in deck_order().get(str(did["Parent"]), [])],
                deck_order())
        want = ["Parent", "Other", "Solo", "Zulu"]
        ok = settle(lambda: shown("") == want)
        t.check("the sidebar top level", ok, shown(""))

        # one more line drop that is only a reorder (no Anki op runs), so
        # only the add-on can bring the home list up to date
        click("Zulu")
        src = at("Zulu")
        s = gesture(src, [src + QPoint(step, 0), at("Parent", "top"), at("Parent", "top")])
        want = ["Zulu", "Parent", "Other", "Solo"]
        ok = settle(lambda: shown("") == want)
        t.check("an order-only drop: Zulu just above Parent", ok, shown(""))

        # 9. the real home deck list -----------------------------------------------------
        want_home = ["Zulu", "Parent", "A", "Mid", "B", "C", "Other", "X", "Y", "Solo", "Alpha"]
        t.note(f"state under the inline Browse: {mw.state}")
        # no refresh by hand: closing Browse must bring back a list that
        # already shows the new order
        embed.close_inline()
        t.pump(300)
        t.wait_js("document.querySelectorAll('.ad-list-row').length > 0", timeout=15)
        ok = t.wait_until(lambda: [n for n in t.deck_list_names() if n != "Default"] == want_home,
                          timeout=15)
        t.check("the home deck list shows the order the sidebar made", ok,
                (t.deck_list_names(), want_home))
    finally:
        if st is not None:
            st.QDrag = real_qdrag
        try:
            del sb.startDrag
        except Exception:
            pass
        QApplication.instance().removeEventFilter(loop_filter)
