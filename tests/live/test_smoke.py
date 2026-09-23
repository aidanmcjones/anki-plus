"""Live smoke test: the real app started, the add-on loaded, and the home
deck list rendered the fixture decks (read from the webview DOM)."""

import sys


def run(t):
    mw = t.mw
    t.check("app is on the deck list", mw.state == "deckBrowser", mw.state)
    t.check("anki-design add-on loaded",
            any(k.startswith("anki-design") for k in sys.modules), "")
    ok = t.wait_js("document.querySelectorAll('.ad-list-row').length > 0", timeout=20)
    t.check("add-on deck list is in the home webview", ok,
            t.js("document.body ? document.body.className : ''"))
    names = t.deck_list_names()
    for want in ("Parent", "A", "B", "C", "Solo"):
        t.check(f"deck list shows {want}", want in names, names)
    t.check("children follow their parent in order",
            names[names.index("Parent") + 1: names.index("Parent") + 4] == ["A", "B", "C"]
            if "Parent" in names else False, names)
