"""Anki Design: decks in the Browse sidebar select, drag and sort like tags.

Reported as: "now apply these same changes to the decks section above it",
after tags got the Browse table's card rules (sidebar_tags.py). The
gestures are the tags' own, served by the same mouse filter and drop
target in sidebar_tags.py; this module answers what differs for decks:

  - plain click                    : search the deck (sidebar_select.py)
  - Cmd/Shift-click                : add to / extend the selection
  - press on an UNSELECTED deck and
    sweep up or down               : the deck rows crossed become the
                                     selection; nothing is dragged
  - press on an unselected deck and
    pull sideways                  : that one deck is dragged
  - press on a SELECTED deck and
    pull                           : the whole selection is dragged
  - right-click                    : "Restudy N decks" (restudy.py) and
                                     Anki's menu, on the whole selection

Where a drag of decks lands, by where in a deck row the cursor is:

  - top quarter    : an insertion line above the row; the decks become its
                     siblings (reparented through Anki's `reparent_decks`
                     if they came from another parent), just before it, in
                     the order they are shown in
  - bottom quarter : the same, just after it
  - the middle     : the row is boxed; the decks nest inside it (Anki's own
                     reparent, as a stock drag does)
  - the Decks header: nest at the top level

A deck never goes into its own subtree, and a child dragged along with its
parent moves with the parent rather than on its own. A filtered deck takes
no children, so its middle is not a target.

The order is NOT a second store: it is the add-on's existing `deck_order`
(parent deck id as a string, "0" for the top level -> child ids in order),
the one the home deck list sorts by (`__init__._deck_order_state` and
friends). A line drop writes the target parent's whole sibling list there
and takes the moved ids out of the lists of the parents they left; deck
ids survive a reparent, so nothing is renamed. The sidebar shows the same
order through `browser_will_build_tree` at the DECKS stage: Anki's own
`_deck_tree` builds the section, then each sibling group is sorted with
the home list's `_apply_deck_order`.
"""

from __future__ import annotations

import sys
from typing import Any, Dict, List, Optional

from aqt import mw
from aqt.qt import QMimeData

from . import sidebar_tags as _st

ADDON = __name__.split(".")[0]

MIME = "application/x-anki-design-decks"


def _log(msg: str) -> None:
    print(f"[anki-design] sidebar_decks: {msg}", flush=True)


def _pkg() -> Any:
    return sys.modules[ADDON]


# --------------------------------------------------------------------------- #
# Decks by id
# --------------------------------------------------------------------------- #
def _name(did: int) -> str:
    try:
        deck = mw.col.decks.get(int(did), default=False)
        return str(deck["name"]) if deck else ""
    except Exception:
        return ""


def _is_filtered(did: int) -> bool:
    try:
        deck = mw.col.decks.get(int(did), default=False)
        return bool(deck and deck.get("dyn"))
    except Exception:
        return False


def parent_id(did: int) -> int:
    """The deck's parent id, 0 for a top-level deck."""
    name = _name(did)
    if "::" not in name:
        return 0
    try:
        return int(mw.col.decks.id_for_name(name.rsplit("::", 1)[0]) or 0)
    except Exception:
        return 0


def within(did: int, ancestor: int) -> bool:
    """`did` is `ancestor` or sits somewhere inside it. The top level (0)
    is inside nothing."""
    if not did or not ancestor:
        return False
    if int(did) == int(ancestor):
        return True
    n, a = _name(did), _name(ancestor)
    return bool(n and a) and _st.is_within(n, a)


def prune(dids: List[int]) -> List[int]:
    """Drop decks whose ancestor is also in the list (it carries them), and
    duplicates. Order kept."""
    out: List[int] = []
    for d in dids:
        d = int(d)
        if d in out:
            continue
        if any(int(o) != d and within(d, int(o)) for o in dids):
            continue
        out.append(d)
    return out


# --------------------------------------------------------------------------- #
# The shared order (`deck_order`, see __init__.DECK_ORDER_KEY)
# --------------------------------------------------------------------------- #
def order_state() -> Dict[str, list]:
    try:
        return _pkg()._deck_order_state()
    except Exception:
        return {}


def _write_state(state: Dict[str, list]) -> None:
    key = getattr(_pkg(), "DECK_ORDER_KEY", "deck_order")
    cfg = mw.addonManager.getConfig(ADDON) or {}
    cfg[key] = {k: v for k, v in state.items() if v}
    mw.addonManager.writeConfig(ADDON, cfg)


def moved_state(
    state: Dict[str, list], moved: List[int], new_parent: int
) -> Dict[str, list]:
    """`state` with the `moved` ids taken out of every parent's list but
    `new_parent`'s: the parents they left forget them."""
    gone = {int(d) for d in moved}
    out: Dict[str, list] = {}
    for k, v in state.items():
        if not isinstance(v, list):
            continue
        if str(k) == str(int(new_parent)):
            out[k] = list(v)
            continue
        kept = []
        for x in v:
            try:
                if int(x) in gone:
                    continue
            except (TypeError, ValueError):
                continue
            kept.append(x)
        out[k] = kept
    return out


def sort_deck_items(section: Any, order: Dict[str, list]) -> None:
    """Sort every deck sibling group under the Decks section root in place,
    by the home list's rule. Non-deck rows (Current Deck) keep their places
    in front."""
    apply = _pkg()._apply_deck_order

    def is_deck(k: Any) -> bool:
        return getattr(k.item_type, "name", "") == "DECK"

    def visit(node: Any, parent: int) -> None:
        kids = list(node.children)
        decks = [k for k in kids if is_deck(k)]
        if len(decks) > 1:
            ranked = order.get(str(parent))
            if ranked:
                decks = apply(decks, ranked, did_of=lambda k: int(k.id or 0))
                it = iter(decks)
                node.children = [next(it) if is_deck(k) else k for k in kids]
        for k in decks:
            visit(k, int(k.id or 0))

    visit(section, 0)


def on_will_build_tree(handled: bool, root: Any, stage: Any, browser: Any) -> bool:
    """`browser_will_build_tree`: build the Decks section with Anki's own
    `_deck_tree`, then put the shared `deck_order` on it."""
    try:
        from aqt.browser.sidebar.tree import SidebarStage

        if handled or stage is not SidebarStage.DECKS:
            return handled
        order = order_state()
        if not order:
            return handled
        sidebar = getattr(browser, "sidebar", None)
        if sidebar is None:
            return handled
    except Exception:
        return handled
    before = len(root.children)
    try:
        sidebar._deck_tree(root)
    except Exception as exc:
        _log(f"deck tree failed: {exc!r}")
        return len(root.children) != before
    try:
        for section in root.children[before:]:
            if getattr(section.item_type, "name", "") == "DECK_ROOT":
                sort_deck_items(section, order)
    except Exception as exc:
        _log(f"sort failed: {exc!r}")
    return True


# --------------------------------------------------------------------------- #
# The sidebar's deck rows
# --------------------------------------------------------------------------- #
def deck_section(sidebar: Any) -> Any:
    try:
        for item in sidebar.model().root.children:
            if getattr(item.item_type, "name", "") == "DECK_ROOT":
                return item
    except Exception:
        pass
    return None


def _deck_rows(node: Any) -> List[Any]:
    return [k for k in node.children if getattr(k.item_type, "name", "") == "DECK"]


def find_deck(sidebar: Any, did: int) -> Any:
    """The deck's SidebarItem (0 is the Decks section root), or None."""
    section = deck_section(sidebar)
    if section is None or not did:
        return section

    def walk(node: Any) -> Any:
        for k in _deck_rows(node):
            if int(k.id or 0) == int(did):
                return k
            hit = walk(k)
            if hit is not None:
                return hit
        return None

    return walk(section)


def children_of(sidebar: Any, parent: int) -> List[int]:
    """Ids of the deck rows under `parent`, as shown."""
    node = find_deck(sidebar, parent)
    if node is None:
        return []
    return [int(k.id) for k in _deck_rows(node)]


def decks_in_display_order(sidebar: Any, dids: List[int]) -> List[int]:
    want = {int(d) for d in dids}
    out: List[int] = []
    section = deck_section(sidebar)

    def walk(node: Any) -> None:
        for k in _deck_rows(node):
            if int(k.id or 0) in want and int(k.id) not in out:
                out.append(int(k.id))
            walk(k)

    if section is not None:
        walk(section)
    for d in dids:
        if int(d) not in out:
            out.append(int(d))
    return out


def selected_decks(sidebar: Any) -> List[int]:
    try:
        dids = [
            int(it.id)
            for it in sidebar._selected_items()
            if it is not None and getattr(it.item_type, "name", "") == "DECK" and it.id
        ]
    except Exception:
        return []
    return decks_in_display_order(sidebar, dids)


# --------------------------------------------------------------------------- #
# Moves
# --------------------------------------------------------------------------- #
def _count(n: int) -> str:
    return "1 deck" if n == 1 else f"{n} decks"


def _render_home() -> None:
    try:
        if getattr(mw, "state", "") == "deckBrowser":
            mw.deckBrowser.refresh()
    except Exception:
        pass


def _refresh_home() -> None:
    """Re-render the home deck list so it shows the new order. While the
    inline Browse covers it, the re-render waits until Browse closes: the
    page is hidden anyway, and repainting a covered page on every drop
    only churns the web view."""
    try:
        from . import browse_embed as _embed

        _embed.after_close(_render_home)
    except Exception:
        _render_home()


def _refresh_all(sidebar: Any) -> None:
    _st._refresh(sidebar)
    _refresh_home()


def _reparent(sidebar: Any, dids: List[int], new_parent: int, done: str) -> None:
    """One `reparent_decks` for the lot: one op, one undo (Anki's own op,
    the one its stock drag runs). Refreshed by hand afterwards: the inline
    Browse is never the focused window an op's own refresh waits for."""
    from anki.decks import DeckId
    from aqt.operations import CollectionOp

    browser = getattr(sidebar, "browser", None) or mw

    def ok(_out: Any) -> None:
        _st._tooltip(sidebar, done)
        _refresh_all(sidebar)

    CollectionOp(
        browser,
        lambda col: col.decks.reparent(
            deck_ids=[DeckId(d) for d in dids], new_parent=DeckId(new_parent)
        ),
    ).success(ok).run_in_background()


def nest(sidebar: Any, dids: List[int], new_parent: int) -> bool:
    """Middle-of-row drop: the decks go inside `new_parent` (0 = top level)."""
    if new_parent and _is_filtered(new_parent):
        return False
    moving = [
        d
        for d in prune(dids)
        if not within(new_parent, d) and parent_id(d) != int(new_parent)
    ]
    if not moving:
        return False
    try:
        _write_state(moved_state(order_state(), moving, new_parent))
    except Exception as exc:
        _log(f"order update failed: {exc!r}")
    where = _name(new_parent) if new_parent else "the top level"
    _reparent(sidebar, moving, new_parent, f"{_count(len(moving))} moved into {where}")
    return True


def place(sidebar: Any, dids: List[int], target: int, after: bool) -> bool:
    """Line drop: the decks sit just before (or after) `target`, under its
    parent, in the order they are shown in."""
    parent = parent_id(target)
    moving = [d for d in prune(dids) if not within(parent, d)]
    if not moving:
        return False
    moving = decks_in_display_order(sidebar, moving)
    siblings = [str(d) for d in children_of(sidebar, parent)]
    placed = _st.placed_order(siblings, [str(d) for d in moving], str(int(target)), after)
    order = [int(d) for d in placed]
    to_move = [d for d in moving if parent_id(d) != parent]
    try:
        state = moved_state(order_state(), moving, parent)
        state[str(parent)] = order
        _write_state(state)
    except Exception as exc:
        _log(f"order write failed: {exc!r}")
        return False
    done = f"{_count(len(moving))} moved"
    if to_move:
        _reparent(sidebar, to_move, parent, done)
    else:
        _st._tooltip(sidebar, done)
        _refresh_all(sidebar)
    return True


# --------------------------------------------------------------------------- #
# The deck rows' answers to the shared gestures (see sidebar_tags._TagKind)
# --------------------------------------------------------------------------- #
def encode_decks(dids: List[int]) -> QMimeData:
    return _st.encode_payload(MIME, [int(d) for d in dids])


def decode_decks(mime: Any) -> List[int]:
    out: List[int] = []
    for d in _st.decode_payload(mime, MIME):
        try:
            out.append(int(d))
        except (TypeError, ValueError):
            continue
    return out


class DeckKind:
    ITEM = "DECK"
    ROOT = "DECK_ROOT"
    MIME = MIME

    def decode(self, mime: Any) -> List[Any]:
        return decode_decks(mime)

    def encode(self, items: List[Any]) -> QMimeData:
        return encode_decks(items)

    def selected(self, sidebar: Any) -> List[Any]:
        return selected_decks(sidebar)

    def count(self, n: int) -> str:
        return _count(n)

    def root_target(self, dragged: List[Any]) -> Optional[tuple]:
        if all(parent_id(d) == 0 for d in prune(dragged)):
            return None
        return ("into", 0)

    def edge_target(self, dragged: List[Any], item: Any, zone: str) -> Optional[tuple]:
        did = int(item.id or 0)
        if not did:
            return None
        parent = parent_id(did)
        if not [d for d in prune(dragged) if not within(parent, d)]:
            return None
        return (zone, did)

    def middle_target(self, dragged: List[Any], item: Any) -> Optional[tuple]:
        did = int(item.id or 0)
        if not did or _is_filtered(did):
            return None
        # not into itself or its own subtree
        if any(within(did, d) for d in dragged):
            return None
        return ("into", did)

    def perform(self, sidebar: Any, items: List[Any], target: Any) -> bool:
        zone, did = target
        if zone == "into":
            return nest(sidebar, items, int(did))
        return place(sidebar, items, int(did), zone == "below")


DECKS = DeckKind()


def register() -> None:
    from aqt import gui_hooks

    gui_hooks.browser_will_build_tree.append(on_will_build_tree)
