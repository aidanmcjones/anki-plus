"""Browse: right-click a selection -> "Restudy N selected cards", and
right-click a tag (or several) in the sidebar -> "Restudy N tags".

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


# Anki refuses a filtered deck search term with a larger limit.
MAX_LIMIT = 99999

LEFT_OUT = "suspended, buried, or already in another filtered deck"


def build_deck_from_search(search: str, limit: int) -> tuple[int, int]:
    """Create or rebuild the Restudy filtered deck from `search`, taking at
    most `limit` cards (capped at Anki's 99999).

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
    term.search = search
    term.limit = max(1, min(int(limit), MAX_LIMIT))
    term.order = FilteredDeckConfig.SearchTerm.Order.DUE
    out = col.sched.add_or_update_filtered_deck(deck)
    new_did = int(out.id)
    got = len(col.find_cards(f"did:{new_did}"))
    return new_did, got


def build_restudy_deck(cids: List[int]) -> tuple[int, int]:
    """Create or rebuild the Restudy filtered deck holding `cids`.

    Returns (deck id, number of cards that made it in)."""
    return build_deck_from_search(
        "cid:" + ",".join(str(int(c)) for c in cids), len(cids)
    )


def restudy_search(search: str, total: int, nothing: str) -> None:
    """Build the Restudy deck from `search` (`total` cards match it), say
    how many made it in and how many were left out, and open the reviewer.
    `nothing` is the tooltip when none of them can be studied."""
    if not search or total <= 0 or getattr(mw, "col", None) is None:
        return
    try:
        did, got = build_deck_from_search(search, total)
    except Exception as exc:
        _log(f"build failed: {exc!r}")
        _tooltip(f"Couldn't build the Restudy deck: {exc}")
        return
    left_out = total - got
    if got == 0:
        _tooltip(nothing)
        return
    mw.col.decks.select(did)
    msg = f"Restudying {got} card{'s' if got != 1 else ''}."
    if left_out:
        msg += f" {left_out} left out ({LEFT_OUT})."
    _tooltip(msg)
    # leaving Browse for the reviewer: the inline Browse tears itself down
    # on the state change
    mw.moveToState("review")


def restudy(cids: List[int]) -> None:
    if not cids:
        return
    ids = list(cids)
    restudy_search(
        "cid:" + ",".join(str(int(c)) for c in ids),
        len(ids),
        f"None of the selected cards can be restudied ({LEFT_OUT}).",
    )


def tag_search(col: Any, tags: List[str]) -> str:
    """One search for every card carrying any of `tags`. `tag:a` already
    matches `a::*`, so child tags come along."""
    from anki.collection import SearchNode

    nodes = [SearchNode(tag=t) for t in tags if t]
    if not nodes:
        return ""
    return col.build_search_string(*nodes, joiner="OR")


def restudy_tags(tags: List[str]) -> None:
    """Restudy every card that carries any of `tags` (children included)."""
    col = getattr(mw, "col", None)
    if not tags or col is None:
        return
    try:
        search = tag_search(col, list(tags))
        total = len(col.find_cards(search)) if search else 0
    except Exception as exc:
        _log(f"tag search failed: {exc!r}")
        _tooltip(f"Couldn't search those tags: {exc}")
        return
    if total == 0:
        _tooltip("No cards carry " + ("that tag." if len(tags) == 1 else "those tags."))
        return
    restudy_search(
        search,
        total,
        f"None of the cards with {'that tag' if len(tags) == 1 else 'those tags'} "
        f"can be restudied ({LEFT_OUT}).",
    )


def _insert_top(menu: QMenu, label: str) -> Any:
    """A new action at the top of `menu`, followed by a separator."""
    first = menu.actions()[0] if menu.actions() else None
    if first is not None:
        # top of the menu, where a study action belongs
        from aqt.qt import QAction

        act = QAction(label, menu)
        menu.insertAction(first, act)
        menu.insertSeparator(first)
    else:
        act = menu.addAction(label)
    return act


def on_browser_will_show_context_menu(browser: Any, menu: QMenu) -> None:
    try:
        cids = list(browser.selected_cards())
    except Exception:
        return
    if not cids:
        return
    n = len(cids)
    act = _insert_top(menu, f"Restudy {n} selected card{'s' if n != 1 else ''}")
    act.setToolTip(
        "Study just these cards now, in a filtered deck named Restudy. "
        "Answers count toward their normal schedules."
    )
    act.triggered.connect(lambda _checked=False, _c=cids: restudy(_c))


def on_sidebar_context_menu(sidebar: Any, menu: QMenu, item: Any, index: Any) -> None:
    """Browse sidebar: right-click a tag (or one of several selected tags)
    -> "Restudy N tags", every card carrying any of them, children too."""
    try:
        from aqt.browser.sidebar.item import SidebarItemType

        if item is None or item.item_type is not SidebarItemType.TAG:
            return
        tags: List[str] = []
        for it in sidebar._selected_items():
            if it is not None and it.item_type is SidebarItemType.TAG:
                if it.full_name not in tags:
                    tags.append(it.full_name)
        if item.full_name not in tags:
            tags.append(item.full_name)
    except Exception:
        return
    n = len(tags)
    act = _insert_top(menu, f"Restudy {n} tag{'s' if n != 1 else ''}")
    act.setToolTip(
        "Study every card with these tags (child tags included) now, in a "
        "filtered deck named Restudy. Answers count toward their normal schedules."
    )
    act.triggered.connect(lambda _checked=False, _t=tags: restudy_tags(_t))


def register() -> None:
    from aqt import gui_hooks

    try:
        gui_hooks.browser_will_show_context_menu.append(
            on_browser_will_show_context_menu
        )
    except Exception as exc:
        _log(f"register failed: {exc!r}")
    try:
        gui_hooks.browser_sidebar_will_show_context_menu.append(
            on_sidebar_context_menu
        )
    except Exception as exc:
        _log(f"sidebar register failed: {exc!r}")
