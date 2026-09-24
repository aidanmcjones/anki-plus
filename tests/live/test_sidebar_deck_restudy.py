"""Live: right-click a deck (or a selection of decks) in the Browse sidebar,
"Restudy N decks".

Request (2026-09-24), after "Restudy N tags": "now apply these same
changes to the decks section above it".

Adds two decks whose names only differ where a deck search reads a
wildcard ("Star*Deck" and "StarXDeck", one card each), opens the real
inline Browse, selects decks in the real sidebar with the real mouse
(click, Cmd-click) and right-clicks through `SidebarTreeView.onContextMenu`
(what a right-click runs; QMenu.exec is swapped for a stand-in that
records the menu instead of blocking offscreen). Checks what the user
gets:

  1. Parent alone: "Restudy 1 deck" first, a separator under it, Anki's
     deck items still there; the Restudy filtered deck holds Parent's six
     cards (all from its subdecks A, B, C), nothing else; the reviewer
     shows one of them; the tooltip counts them
  2. Parent::A + Star*Deck (Cmd-click): "Restudy 2 decks", exactly A's two
     cards and Star*Deck's one (StarXDeck, which an unescaped `*` would
     match, stays out), the same Restudy deck reused
  3. the Restudy filtered deck + Solo: Solo's two cards go in, the
     tooltip says the filtered deck was left out and why
  4. the Restudy deck alone: refused with a tooltip, no reviewer, the
     deck's cards untouched

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
    from aqt.qt import QMenu, QModelIndex, Qt

    mw, Q = t.mw, t.QTest
    col = mw.col
    embed = _module(".browse_embed")

    def add_card(deck_name, front):
        did = col.decks.id(deck_name)
        note = col.new_note(col.models.by_name("Basic"))
        note["Front"], note["Back"] = front, "b"
        col.add_note(note, did)
        return did

    add_card("Star*Deck", "star")
    add_card("StarXDeck", "starx")

    parent6 = sorted(col.find_cards('deck:"Parent"'))
    a2 = sorted(col.find_cards('deck:"Parent::A"'))
    star = sorted(col.find_cards(f"did:{col.decks.id_for_name('Star*Deck')}"))
    starx = sorted(col.find_cards(f"did:{col.decks.id_for_name('StarXDeck')}"))
    solo = sorted(col.find_cards('deck:"Solo"'))
    t.note(f"parent={len(parent6)} a={len(a2)} star={star} starx={starx} solo={len(solo)}")

    captured = []
    tips = []
    orig_exec, orig_tip = QMenu.exec, aqt.utils.tooltip

    def fake_exec(self, *a, **k):
        captured.clear()
        captured.append([act.text() for act in self.actions()])
        captured.append(self)
        return None

    def fake_tip(msg, *a, **k):
        tips.append(str(msg))
        return orig_tip(msg, *a, **k)

    QMenu.exec = fake_exec
    aqt.utils.tooltip = fake_tip

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

    def find(sb, full):
        m = sb.model()
        if m is None:
            return None

        def walk(parent):
            for r in range(m.rowCount(parent)):
                c = m.index(r, 0, parent)
                it = m.item_for_index(c)
                if it is not None and it.full_name == full and it.item_type.name == "DECK":
                    return c
                hit = walk(c)
                if hit is not None:
                    return hit
            return None

        return walk(QModelIndex())

    def point(sb, full):
        idx = find(sb, full)
        if idx is None:
            return None
        sb.scrollTo(idx)
        t.pump(30)
        return sb.visualRect(idx).center()

    def click(sb, full, mods=Qt.KeyboardModifier.NoModifier):
        p = point(sb, full)
        if p is None:
            return False
        t.wait_until(lambda: sb.state() != sb.State.AnimatingState, timeout=5)
        Q.mouseClick(mw.windowHandle(), Qt.MouseButton.LeftButton, mods,
                     sb.viewport().mapTo(mw, p))
        t.pump(80)
        return True

    def picked(sb):
        return sorted(col.decks.name(d) for d in sb._selected_decks())

    def right_click(sb, full):
        captured.clear()
        sb.onContextMenu(point(sb, full))
        return captured[1] if len(captured) > 1 else None

    def restudy_act(menu):
        return next((a for a in menu.actions() if a.text().startswith("Restudy")), None) if menu else None

    rs = _module(".restudy")

    def restudy_did():
        return rs.find_restudy_deck(col)

    def rname():
        did = restudy_did()
        return col.decks.name(did) if did else "Restudy"

    def in_restudy():
        did = restudy_did()
        return sorted(col.find_cards(f"did:{did}")) if did else []

    CMD = Qt.KeyboardModifier.ControlModifier
    try:
        # 1. Parent alone: its subdecks' cards ----------------------------------
        br = open_browse()
        t.check("Browse opens inline", br is not None, embed and embed._state)
        if br is None:
            return
        sb = br.sidebar
        ok = t.wait_until(lambda: find(sb, "Parent::C") is not None, timeout=10)
        t.check("the sidebar lists the fixture decks", ok, "")
        if not ok:
            return
        click(sb, "Parent")
        t.check("Parent is selected", picked(sb) == ["Parent"], picked(sb))
        menu = right_click(sb, "Parent")
        texts = captured[0] if captured else []
        t.check("the right-click menu opens", menu is not None, texts)
        t.check("Restudy is the first item: Restudy 1 deck",
                bool(texts) and texts[0] == "Restudy 1 deck", texts[:4])
        t.check("a separator follows it", len(texts) > 1 and texts[1] == "", texts[:4])
        t.check("Anki's own deck items are still there", "Delete" in texts, texts)
        act = restudy_act(menu)
        if act is None:
            return
        tips.clear()
        act.trigger()
        t.pump(300)
        did = restudy_did()
        deck = col.decks.get(did) if did else None
        t.check("a filtered deck named Restudy exists", bool(deck and deck.get("dyn")),
                deck and deck.get("name"))
        t.check("it holds Parent's six cards, all from its subdecks",
                in_restudy() == parent6 and len(parent6) == 6, (in_restudy(), parent6))
        t.check("no Solo or Star card came along",
                not (set(in_restudy()) & set(solo + star + starx)), in_restudy())
        t.check("the tooltip counts the cards", any("Restudying 6 cards in Restudy: " in x for x in tips), tips)
        ok = t.wait_until(lambda: mw.state == "review", timeout=10)
        t.check("the app is in the reviewer", ok, mw.state)
        rc = getattr(mw.reviewer, "card", None)
        t.check("the reviewer shows one of them", rc is not None and rc.id in parent6, rc and rc.id)

        # 2. two decks, one with a wildcard in its name -------------------------
        br = open_browse()
        if br is None:
            t.check("Browse reopens", False, "")
            return
        sb = br.sidebar
        t.wait_until(lambda: find(sb, "Star*Deck") is not None, timeout=10)
        click(sb, "Parent::A")
        click(sb, "Star*Deck", CMD)
        t.check("Parent::A and Star*Deck are selected",
                picked(sb) == ["Parent::A", "Star*Deck"], picked(sb))
        menu = right_click(sb, "Star*Deck")
        texts = captured[0] if captured else []
        t.check("the item counts both: Restudy 2 decks",
                bool(texts) and texts[0] == "Restudy 2 decks", texts[:3])
        act = restudy_act(menu)
        if act is None:
            return
        tips.clear()
        act.trigger()
        t.pump(300)
        t.check("the same Restudy deck is reused", restudy_did() == did, (did, restudy_did()))
        t.check("it holds A's two cards and Star*Deck's one, exactly",
                in_restudy() == sorted(a2 + star), (in_restudy(), a2, star))
        t.check("StarXDeck stays out (the * in the name is not a wildcard)",
                not (set(in_restudy()) & set(starx)), in_restudy())
        t.check("the tooltip counts 3", any("Restudying 3 cards in Restudy: " in x for x in tips), tips)
        t.check("reviewer again", t.wait_until(lambda: mw.state == "review", timeout=10), mw.state)

        # 3. the Restudy filtered deck + Solo --------------------------------------
        br = open_browse()
        if br is None:
            t.check("Browse reopens", False, "")
            return
        sb = br.sidebar
        t.wait_until(lambda: find(sb, rname()) is not None, timeout=10)
        click(sb, rname())
        click(sb, "Solo", CMD)
        t.check("Restudy and Solo are selected", picked(sb) == [rname(), "Solo"], picked(sb))
        menu = right_click(sb, "Solo")
        texts = captured[0] if captured else []
        t.check("Restudy 2 decks on top", bool(texts) and texts[0] == "Restudy 2 decks", texts[:3])
        act = restudy_act(menu)
        if act is None:
            return
        tips.clear()
        act.trigger()
        t.pump(300)
        t.check("only Solo's two cards go in", in_restudy() == solo, (in_restudy(), solo))
        t.check("the deck is titled for what it holds", rname() == "Restudy: Solo", rname())
        t.check("the tooltip says the filtered deck was left out, and why",
                any("Restudying 2 cards in Restudy: " in x and "1 filtered deck left out" in x
                    and "can't go into another filtered deck" in x for x in tips), tips)
        t.check("reviewer again", t.wait_until(lambda: mw.state == "review", timeout=10), mw.state)

        # 4. the Restudy deck alone: refused ------------------------------------------
        br = open_browse()
        if br is None:
            t.check("Browse reopens", False, "")
            return
        sb = br.sidebar
        t.wait_until(lambda: find(sb, rname()) is not None, timeout=10)
        click(sb, rname())
        menu = right_click(sb, rname())
        texts = captured[0] if captured else []
        t.check("Restudy 1 deck on the filtered deck's menu too",
                bool(texts) and texts[0] == "Restudy 1 deck", texts[:3])
        act = restudy_act(menu)
        if act is None:
            return
        tips.clear()
        before = in_restudy()
        act.trigger()
        t.pump(300)
        t.check("a filtered deck alone is refused, with the reason",
                any("filtered deck" in x and "can't be restudied" in x for x in tips), tips)
        t.check("the Restudy deck keeps its cards", in_restudy() == before, (in_restudy(), before))
        t.check("the app stays in Browse, no reviewer", mw.state != "review", mw.state)
    finally:
        QMenu.exec = orig_exec
        aqt.utils.tooltip = orig_tip
