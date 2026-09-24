"""Live: the Restudy filtered deck is titled after what it holds.

Ticket 20260924-131713: "the restudy deck should have a title denoting
what the restudy deck contains, for example: restudy: funbiochem
hi-yield". Every restudy used to land in one deck named just "Restudy",
so the deck list never said what was in it.

Drives the real inline Browse the way the user does (real sidebar clicks,
`SidebarTreeView.onContextMenu` / `Table._on_context_menu` for the
right-click, QMenu.exec swapped for a recorder so it doesn't block
offscreen) and checks the deck the user then sees in the deck list:

  1. sidebar tag funbiochem::hi-yield -> "Restudy: funbiochem / hi-yield"
     (no "::", which would have made it a subdeck of "Restudy")
  2. sidebar decks Parent::A + Solo -> "Restudy: A, Solo", the same deck
     reused and renamed, still the only Restudy deck
  3. card table, two of Parent::B's cards -> "Restudy: B"
  4. the deck browser's rendered list shows the new title

Fixture: Parent::{A,B,C} with two new Basic cards each, and Solo.
"""

import sys


def _module(suffix):
    for name, mod in list(sys.modules.items()):
        if mod is not None and name.startswith("anki-design") and name.endswith(suffix):
            return mod
    return None


def run(t):
    import aqt.utils
    from aqt.qt import QMenu, QModelIndex, QPoint, Qt

    mw, Q = t.mw, t.QTest
    col = mw.col
    # keep the fixture tag where it is
    _cfg = mw.addonManager.getConfig("anki-design") or {}
    _cfg["auto_organize_tags"] = False
    mw.addonManager.writeConfig("anki-design", _cfg)
    embed = _module(".browse_embed")

    c_nids = sorted({col.get_card(c).nid for c in col.find_cards('deck:"Parent::C"')})
    col.tags.bulk_add(c_nids, "funbiochem::hi-yield")
    c_cards = sorted(col.find_cards('deck:"Parent::C"'))
    b_cards = sorted(col.find_cards('deck:"Parent::B"'))

    captured = []
    orig_exec = QMenu.exec

    def fake_exec(self, *a, **k):
        captured.clear()
        captured.append([act.text() for act in self.actions()])
        captured.append(self)
        return None

    QMenu.exec = fake_exec

    def open_browse():
        if mw.state == "review":
            mw.moveToState("deckBrowser")
            t.pump(200)
        mw.onBrowse()
        br = t.wait_until(lambda: embed._state.get("browser"), timeout=20)
        if br is None:
            return None
        t.wait_until(lambda: embed._state.get("curtain") is None, timeout=10)
        t.pump(200)
        return br

    def find(sb, full, kind):
        m = sb.model()
        if m is None:
            return None

        def walk(parent):
            for r in range(m.rowCount(parent)):
                c = m.index(r, 0, parent)
                it = m.item_for_index(c)
                if it is not None and it.full_name == full and it.item_type.name == kind:
                    return c
                hit = walk(c)
                if hit is not None:
                    return hit
            return None

        return walk(QModelIndex())

    def point(sb, full, kind):
        idx = find(sb, full, kind)
        if idx is None:
            return None
        sb.scrollTo(idx)
        t.pump(30)
        return sb.visualRect(idx).center()

    def click(sb, full, kind, mods=Qt.KeyboardModifier.NoModifier):
        p = point(sb, full, kind)
        if p is None:
            return False
        t.wait_until(lambda: sb.state() != sb.State.AnimatingState, timeout=5)
        Q.mouseClick(mw.windowHandle(), Qt.MouseButton.LeftButton, mods,
                     sb.viewport().mapTo(mw, p))
        t.pump(80)
        return True

    def trigger_restudy(menu):
        act = next((a for a in menu.actions() if a.text().startswith("Restudy")), None) if menu else None
        if act is None:
            return False
        act.trigger()
        t.pump(300)
        return True

    def restudy_decks():
        out = []
        for d in col.decks.all_names_and_ids():
            deck = col.decks.get(d.id)
            if deck and deck.get("dyn") and d.name.startswith("Restudy"):
                out.append((int(d.id), d.name))
        return out

    try:
        # 1. a nested tag ------------------------------------------------------
        br = open_browse()
        t.check("Browse opens inline", br is not None, embed and embed._state)
        if br is None:
            return
        sb = br.sidebar
        ok = t.wait_until(lambda: find(sb, "funbiochem::hi-yield", "TAG") is not None, timeout=10)
        t.check("the sidebar lists the tag", ok, "")
        if not ok:
            return
        click(sb, "funbiochem::hi-yield", "TAG")
        captured.clear()
        sb.onContextMenu(point(sb, "funbiochem::hi-yield", "TAG"))
        t.check("the tag's Restudy item runs", trigger_restudy(captured[1] if len(captured) > 1 else None),
                captured[:1])
        decks = restudy_decks()
        t.check("one Restudy filtered deck exists", len(decks) == 1, decks)
        if not decks:
            return
        did, name = decks[0]
        t.check("it is titled after the tag: Restudy: funbiochem / hi-yield",
                name == "Restudy: funbiochem / hi-yield", name)
        t.check("it is a top-level deck, not a subdeck of Restudy", "::" not in name, name)
        t.check("it holds the tagged cards", sorted(col.find_cards(f"did:{did}")) == c_cards,
                (sorted(col.find_cards(f"did:{did}")), c_cards))
        t.check("the reviewer opens", t.wait_until(lambda: mw.state == "review", timeout=10), mw.state)

        # 2. two decks from the sidebar -------------------------------------------
        br = open_browse()
        if br is None:
            t.check("Browse reopens", False, "")
            return
        sb = br.sidebar
        t.wait_until(lambda: find(sb, "Solo", "DECK") is not None, timeout=10)
        click(sb, "Parent::A", "DECK")
        click(sb, "Solo", "DECK", Qt.KeyboardModifier.ControlModifier)
        captured.clear()
        sb.onContextMenu(point(sb, "Solo", "DECK"))
        t.check("the decks' Restudy item runs", trigger_restudy(captured[1] if len(captured) > 1 else None),
                captured[:1])
        decks = restudy_decks()
        t.check("still one Restudy deck, the same one", [d for d, _ in decks] == [did], decks)
        t.check("it is renamed after the decks: Restudy: A, Solo",
                bool(decks) and decks[0][1] == "Restudy: A, Solo", decks)
        t.check("the reviewer opens", t.wait_until(lambda: mw.state == "review", timeout=10), mw.state)

        # 3. selected cards in the table --------------------------------------------
        br = open_browse()
        if br is None:
            t.check("Browse reopens", False, "")
            return
        br.search_for("cid:" + ",".join(str(c) for c in b_cards))
        t.wait_until(lambda: br.table.len() == len(b_cards), timeout=10)
        br.table.select_all()
        t.pump(100)
        captured.clear()
        br.table._on_context_menu(QPoint(5, 5))
        t.check("the cards' Restudy item runs", trigger_restudy(captured[1] if len(captured) > 1 else None),
                captured[:1])
        decks = restudy_decks()
        t.check("still one Restudy deck, the same one", [d for d, _ in decks] == [did], decks)
        t.check("it is named after the cards' deck: Restudy: B",
                bool(decks) and decks[0][1] == "Restudy: B", decks)
        t.check("the reviewer opens", t.wait_until(lambda: mw.state == "review", timeout=10), mw.state)

        # 4. what the deck list shows ----------------------------------------------
        mw.moveToState("deckBrowser")
        t.pump(300)
        mw.deckBrowser.refresh()
        t.pump(500)
        text = t.js("document.body.innerText") or ""
        t.check("the deck list shows Restudy: B", "Restudy: B" in text, text[-300:])
    finally:
        QMenu.exec = orig_exec
