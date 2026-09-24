"""Browse: right-click a selection -> "Restudy N selected cards".

Builds (or rebuilds) one filtered deck, "Restudy", from exactly the
selected card ids and starts studying it. Answers count toward the cards'
real schedules (reschedule on), the same as studying them in their own
decks. Reusing one deck keeps the deck list clean: every restudy empties
the previous selection back to its home decks first.

Filtered decks cannot take suspended or buried cards, or cards already
sitting in another filtered deck; those are left out and the tooltip says
how many.
"""

from __future__ import annotations

from typing import Any, List

from aqt import mw
from aqt.qt import QMenu

DECK_NAME = "Restudy"
# used only when the user already has a NORMAL deck called "Restudy"
FALLBACK_NAME = "Restudy (selected cards)"


def _log(msg: str) -> None:
    print(f"[anki-design] restudy: {msg}", flush=True)


def _tooltip(msg: str) -> None:
    try:
        from aqt.utils import tooltip

        tooltip(msg, period=4000)
    except Exception:
        _log(msg)


def _target_deck_id(col: Any) -> tuple[int, str]:
    """(existing filtered deck id or 0, name to use)."""
    for name in (DECK_NAME, FALLBACK_NAME):
        did = col.decks.id_for_name(name)
        if not did:
            return 0, name
        deck = col.decks.get(did)
        if deck and deck.get("dyn"):
            return int(did), name
    return 0, FALLBACK_NAME


def build_restudy_deck(cids: List[int]) -> tuple[int, int]:
    """Create or rebuild the Restudy filtered deck holding `cids`.

    Returns (deck id, number of cards that made it in)."""
    from anki.decks import FilteredDeckConfig

    col = mw.col
    did, name = _target_deck_id(col)
    if did:
        # put the previous selection back first, so cards that are only in
        # "Restudy" because of the last restudy are free to be chosen again
        col.sched.empty_filtered_deck(did)
    deck = col.sched.get_or_create_filtered_deck(did)
    deck.name = name
    deck.allow_empty = True
    cfg = deck.config
    cfg.reschedule = True
    del cfg.search_terms[:]
    term = cfg.search_terms.add()
    term.search = "cid:" + ",".join(str(int(c)) for c in cids)
    term.limit = len(cids)
    term.order = FilteredDeckConfig.SearchTerm.Order.DUE
    out = col.sched.add_or_update_filtered_deck(deck)
    new_did = int(out.id)
    got = len(col.find_cards(f"did:{new_did}"))
    return new_did, got


def restudy(cids: List[int]) -> None:
    if not cids or getattr(mw, "col", None) is None:
        return
    try:
        did, got = build_restudy_deck(list(cids))
    except Exception as exc:
        _log(f"build failed: {exc!r}")
        _tooltip(f"Couldn't build the Restudy deck: {exc}")
        return
    left_out = len(cids) - got
    if got == 0:
        _tooltip(
            "None of the selected cards can be restudied "
            "(suspended, buried, or already in another filtered deck)."
        )
        return
    mw.col.decks.select(did)
    msg = f"Restudying {got} card{'s' if got != 1 else ''}."
    if left_out:
        msg += (
            f" {left_out} left out (suspended, buried, or already in "
            "another filtered deck)."
        )
    _tooltip(msg)
    # leaving Browse for the reviewer: the inline Browse tears itself down
    # on the state change
    mw.moveToState("review")


def on_browser_will_show_context_menu(browser: Any, menu: QMenu) -> None:
    try:
        cids = list(browser.selected_cards())
    except Exception:
        return
    if not cids:
        return
    n = len(cids)
    label = f"Restudy {n} selected card{'s' if n != 1 else ''}"
    first = menu.actions()[0] if menu.actions() else None
    if first is not None:
        # top of the menu, where a study action belongs
        from aqt.qt import QAction

        act = QAction(label, menu)
        menu.insertAction(first, act)
        menu.insertSeparator(first)
    else:
        act = menu.addAction(label)
    act.setToolTip(
        "Study just these cards now, in a filtered deck named Restudy. "
        "Answers count toward their normal schedules."
    )
    act.triggered.connect(lambda _checked=False, _c=cids: restudy(_c))


def register() -> None:
    from aqt import gui_hooks

    try:
        gui_hooks.browser_will_show_context_menu.append(
            on_browser_will_show_context_menu
        )
    except Exception as exc:
        _log(f"register failed: {exc!r}")
