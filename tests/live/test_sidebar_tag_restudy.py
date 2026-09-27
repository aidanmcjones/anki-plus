"""Live: right-click a tag (or a selection of tags) in the Browse sidebar,
"Restudy N tags".

Request (2026-09-24), after "Restudy N selected cards" on the card table:
"also should do the same for right clicking on a tag or group of tags".

Tags the fixture notes (Parent::A alpha, Parent::B beta, Parent::C
beta::kid, Solo gamma), opens the real inline Browse, selects tags in the
real sidebar with the real mouse (click, then Cmd-click), and right-clicks
through `SidebarTreeView.onContextMenu` (what a right-click runs; QMenu.exec
is swapped for a stand-in that records the menu instead of blocking
offscreen). Checks what the user gets: "Restudy 2 tags" at the top of the
menu with a separator under it, a filtered deck holding every card that
carries either tag, child tags included, the reviewer on one of them, and a
tooltip that counts them; then a single tag with one suspended card: one
card in, "1 left out" in the tooltip.

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
    # these flat fixture tags must stay flat: keep the tag organizer off
    _cfg = mw.addonManager.getConfig("anki-design") or {}
    _cfg["auto_organize_tags"] = False
    mw.addonManager.writeConfig("anki-design", _cfg)
    embed = _module(".browse_embed")

    def nids(deck):
        return sorted({col.get_card(c).nid for c in col.find_cards(f'deck:"{deck}"')})

    col.tags.bulk_add(nids("Parent::A"), "alpha")
    col.tags.bulk_add(nids("Parent::B"), "beta")
    col.tags.bulk_add(nids("Parent::C"), "beta::kid")
    col.tags.bulk_add(nids("Solo"), "gamma")
    want_ab = sorted(col.find_cards('deck:"Parent::A" or deck:"Parent::B" or deck:"Parent::C"'))
    solo = sorted(col.find_cards('deck:"Solo"'))

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
                if it is not None and it.full_name == full and it.item_type.name == "TAG":
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
        win = mw.windowHandle()
        Q.mouseClick(win, Qt.MouseButton.LeftButton, mods, sb.viewport().mapTo(mw, p))
        t.pump(80)
        return True

    def right_click(sb, full):
        captured.clear()
        sb.onContextMenu(point(sb, full))
        return captured[1] if len(captured) > 1 else None

    try:
        br = open_browse()
        t.check("Browse opens inline", br is not None, embed and embed._state)
        if br is None:
            return
        sb = br.sidebar
        ok = t.wait_until(lambda: find(sb, "beta::kid") is not None, timeout=10)
        t.check("the sidebar lists the new tags", ok, "")
        if not ok:
            return

        # select alpha and beta with the real mouse: click, then Cmd-click
        click(sb, "alpha")
        click(sb, "beta", Qt.KeyboardModifier.ControlModifier)
        picked = sorted(sb._selected_tags())
        t.check("alpha and beta are selected", picked == ["alpha", "beta"], picked)

        menu = right_click(sb, "beta")
        texts = captured[0] if captured else []
        t.check("the right-click menu opens", menu is not None, texts)
        t.check("Restudy is the first item, counting the whole selection",
                bool(texts) and texts[0] == "Restudy 2 tags", texts[:4])
        t.check("a separator follows it", len(texts) > 1 and texts[1] == "", texts[:4])
        t.check("Anki's own tag items are still there",
                "Add to Selected Notes" in texts and "Delete" in texts, texts)
        act = next((a for a in menu.actions() if a.text().startswith("Restudy")), None) if menu else None
        if act is None:
            return
        tips.clear()
        act.trigger()
        t.pump(300)

        rs = _module(".restudy")
        did = rs.find_restudy_deck(col)
        t.check("the deck is titled for the tags", did and col.decks.name(did) == "Restudy: alpha, beta", did and col.decks.name(did))
        deck = col.decks.get(did) if did else None
        t.check("a filtered deck named Restudy exists", bool(deck and deck.get("dyn")),
                deck and deck.get("name"))
        in_deck = sorted(col.find_cards(f"did:{did}")) if did else []
        t.check("it holds every card tagged alpha, beta or beta::kid (6)",
                in_deck == want_ab, (len(in_deck), in_deck, want_ab))
        t.check("no untagged or gamma card came along",
                not (set(in_deck) & set(solo)), in_deck)
        t.check("the tooltip counts the cards", any("Restudying 6 cards in Restudy: " in x for x in tips), tips)
        ok = t.wait_until(lambda: mw.state == "review", timeout=10)
        t.check("the app is in the reviewer", ok, mw.state)
        rc = getattr(mw.reviewer, "card", None)
        t.check("the reviewer shows one of those cards", rc is not None and rc.id in want_ab,
                rc and rc.id)

        # --- one tag, one of its cards suspended ---------------------------
        col.sched.suspend_cards([solo[1]])
        br = open_browse()
        if br is None:
            t.check("Browse reopens", False, "")
            return
        sb = br.sidebar
        t.wait_until(lambda: find(sb, "gamma") is not None, timeout=10)
        click(sb, "gamma")
        t.check("gamma is selected", sb._selected_tags() == ["gamma"], sb._selected_tags())
        menu = right_click(sb, "gamma")
        texts = captured[0] if captured else []
        t.check("a single tag says 1 tag", bool(texts) and texts[0] == "Restudy 1 tag", texts[:3])
        act = next((a for a in menu.actions() if a.text().startswith("Restudy")), None) if menu else None
        if act is None:
            return
        tips.clear()
        act.trigger()
        t.pump(300)
        did2 = rs.find_restudy_deck(col)
        t.check("the same Restudy deck is reused", did2 == did, (did, did2))
        in_deck = sorted(col.find_cards(f"did:{did2}"))
        t.check("it holds only gamma's unsuspended card", in_deck == [solo[0]], in_deck)
        t.check("the tooltip says one was left out",
                any("Restudying 1 card in Restudy: " in x and "1 left out" in x for x in tips), tips)
        t.check("reviewer again", t.wait_until(lambda: mw.state == "review", timeout=10), mw.state)
    finally:
        QMenu.exec = orig_exec
        aqt.utils.tooltip = orig_tip
