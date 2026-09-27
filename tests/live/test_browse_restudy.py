"""Live: select cards in Browse, right-click, "Restudy N selected cards".

Request (2026-09-24): "I should be able to select a group of cards and
right click and have there be a restudy option where I can study the
selected cards."

Drives the real Browse table context menu (Table._on_context_menu, the
same code a right-click runs; QMenu.exec is swapped for a stand-in that
records the menu instead of blocking offscreen) and checks what the user
gets: the Restudy item at the top of the menu, a filtered deck holding
exactly the selected cards, the reviewer showing one of them, Browse
closed. A second restudy of a different selection must rebuild the same
deck (previous cards back home), and a suspended card is left out.

Fixture: Parent::{A,B,C} with two new Basic cards each, and Solo.
"""

import sys


def _module(suffix):
    for name, mod in list(sys.modules.items()):
        if mod is not None and name.startswith("anki-design") and name.endswith(suffix):
            return mod
    return None


def run(t):
    from aqt.qt import QMenu, QPoint

    mw = t.mw
    col = mw.col
    embed = _module(".browse_embed")
    t.check("restudy module is loaded", _module(".restudy") is not None,
            [k for k in sys.modules if k.startswith("anki-design")][:30])

    captured = []
    orig_exec = QMenu.exec

    def fake_exec(self, *a, **k):
        captured.append([act.text() for act in self.actions()])
        captured.append(self)
        return None

    QMenu.exec = fake_exec

    def right_click(br):
        captured.clear()
        br.table._on_context_menu(QPoint(5, 5))
        return captured[1] if len(captured) > 1 else None

    def select_rows(br, cids):
        br.search_for("cid:" + ",".join(str(c) for c in cids))
        t.wait_until(lambda: br.table.len() == len(cids), timeout=10)
        br.table.select_all()
        t.pump(100)
        return sorted(br.selected_cards())

    try:
        a_cids = sorted(col.find_cards('deck:"Parent::A"'))
        b_cids = sorted(col.find_cards('deck:"Parent::B"'))
        solo = sorted(col.find_cards('deck:"Solo"'))
        home = {c: col.get_card(c).did for c in a_cids + b_cids + solo}

        # --- first restudy: A's two cards + one of B's -------------------
        mw.onBrowse()
        br = t.wait_until(lambda: embed._state.get("browser"), timeout=20)
        t.check("Browse opens inline", br is not None, embed._state if embed else None)
        if br is None:
            return
        t.wait_until(lambda: embed._state.get("curtain") is None, timeout=10)
        pick = a_cids + b_cids[:1]
        sel = select_rows(br, pick)
        t.check("three cards selected", sel == sorted(pick), sel)

        menu = right_click(br)
        texts = captured[0] if captured else []
        t.check("the right-click menu opens", menu is not None, captured)
        t.check("Restudy is the first item, with the count",
                bool(texts) and texts[0] == "Restudy 3 selected cards", texts[:4])
        act = next((a for a in menu.actions() if a.text().startswith("Restudy")), None) if menu else None
        if act is None:
            return
        act.trigger()
        t.pump(300)

        rs = _module(".restudy")
        did = rs.find_restudy_deck(col)
        deck = col.decks.get(did) if did else None
        t.check("a Restudy filtered deck exists", bool(deck and deck.get("dyn")), deck and deck.get("name"))
        t.check("titled with the count when the cards span decks",
                bool(deck) and deck["name"] == "Restudy: 3 cards", deck and deck["name"])
        in_deck = sorted(col.find_cards(f"did:{did}")) if did else []
        t.check("it holds exactly the selected cards", in_deck == sorted(pick), in_deck)
        t.check("it is the current deck", col.decks.current()["id"] == did, col.decks.current()["name"])
        t.check("reschedule is on (answers count)", bool(deck and deck.get("resched")), deck and deck.get("resched"))
        ok = t.wait_until(lambda: mw.state == "review", timeout=10)
        t.check("the app is in the reviewer", ok, mw.state)
        rc = getattr(mw.reviewer, "card", None)
        t.check("the reviewer shows a selected card", rc is not None and rc.id in pick, rc and rc.id)
        t.check("inline Browse closed", embed._state.get("browser") is None, embed._state.get("browser"))

        # --- second restudy: Solo's cards; one suspended -----------------
        col.sched.suspend_cards([solo[1]])
        mw.onBrowse()
        br = t.wait_until(lambda: embed._state.get("browser"), timeout=20)
        t.wait_until(lambda: embed._state.get("curtain") is None, timeout=10)
        sel = select_rows(br, solo)
        t.check("both Solo cards selected", sel == solo, sel)
        menu = right_click(br)
        texts = captured[0] if captured else []
        t.check("second menu says 2 cards", bool(texts) and texts[0] == "Restudy 2 selected cards", texts[:3])
        act = next((a for a in menu.actions() if a.text().startswith("Restudy")), None) if menu else None
        if act is None:
            return
        act.trigger()
        t.pump(300)

        did2 = rs.find_restudy_deck(col)
        t.check("retitled for Solo's cards", col.decks.name(did2) == "Restudy: Solo, 2 cards", col.decks.name(did2))
        t.check("the same Restudy deck is reused", did2 == did, (did, did2))
        in_deck = sorted(col.find_cards(f"did:{did2}"))
        t.check("it now holds only the unsuspended Solo card", in_deck == [solo[0]], in_deck)
        back = {c: col.get_card(c).did for c in pick}
        t.check("the first selection went back to its home decks",
                all(back[c] == home[c] for c in pick), back)
        names = [n.name for n in col.decks.all_names_and_ids()]
        t.check("only one Restudy deck in the list",
                sum(1 for n in names if n.startswith("Restudy")) == 1, names)
        t.check("reviewer again", t.wait_until(lambda: mw.state == "review", timeout=10), mw.state)
    finally:
        QMenu.exec = orig_exec
