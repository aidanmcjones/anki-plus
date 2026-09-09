"""Anki Design — two additions to the reviewer's "More" menu.

**Randomize Set** rebuilds the deck you're studying as one shuffled queue,
so the next card is drawn at random from everything you'd study today
rather than in Anki's new/learning/review order.

**Cue…** lists every tag actually present in the deck you're studying and
turns any of them into an immediate study queue — the thing you reach for
when a lecture, a chapter or a card type suddenly needs drilling.

Both go through `build_study_deck()` in `__init__.py`, the same
rescheduling-filtered-deck path the sidebar's Due / New / Learning totals
use, so answers count exactly as normal reviews do and repeated use
refreshes one queue instead of littering the deck list.

Why the deck names replace `::` with ` / `
------------------------------------------
`::` is Anki's *deck* separator. A filtered deck literally named
"Study: Micro::15_Eukarya" would be created as "15_Eukarya" nested under
a new "Study: Micro" parent, which is not what anyone means. Substituting
a separator keeps the full tag path visible (a bare leaf would collide
across hierarchies — `Type::Chart` and `Week3::Chart` are different
things) while staying a single flat deck.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from aqt import mw
from aqt.qt import QMenu


ADDON = __name__.split(".")[0]

MIX_DECK = "Study: Mix"
# One flat level of nesting is where a tag menu stops helping and starts
# being a maze; deeper paths are flattened into their parent's submenu
# with the remaining path shown inline.
MAX_MENU_DEPTH = 2


def _config() -> Dict[str, Any]:
    try:
        return mw.addonManager.getConfig(ADDON) or {}
    except Exception:
        return {}


def enabled() -> bool:
    return bool(_config().get("reviewer_menu_extras", True))


# --------------------------------------------------------------------------- #
# Which deck are we actually studying?
# --------------------------------------------------------------------------- #
def _source_deck(reviewer: Any) -> Optional[Tuple[int, str]]:
    """(deck id, deck name) of the deck the current card really lives in.

    `col.decks.current()` is the *selected* deck, which after one use of
    Randomize or Cue is one of our own filtered decks — cueing a tag out of
    that would be circular and would shrink the pool every time. A card in
    a filtered deck remembers its home deck in `odid`, so the card itself
    is the honest source, and it needs no state that a restart could lose.
    """
    col = getattr(mw, "col", None)
    if col is None:
        return None
    card = getattr(reviewer, "card", None)
    did = 0
    if card is not None:
        did = int(getattr(card, "odid", 0) or 0) or int(getattr(card, "did", 0) or 0)
    if not did:
        try:
            did = int(col.decks.get_current_id())
        except Exception:
            return None
    try:
        deck = col.decks.get(did)
    except Exception:
        return None
    if not deck:
        return None
    if deck.get("dyn"):
        # A filtered deck that isn't ours (or a card with no odid): there is
        # no sensible source deck to scope to.
        return None
    return did, deck["name"]


def _deck_search(col: Any, deck_name: str) -> str:
    """`deck:"..."` for a deck and its subdecks, escaped by the backend
    rather than by us — deck names contain quotes, colons and asterisks."""
    from anki.collection import SearchNode

    return col.build_search_string(SearchNode(deck=deck_name))


# --------------------------------------------------------------------------- #
# Tags present in a deck
# --------------------------------------------------------------------------- #
def deck_tags(col: Any, deck_name: str) -> List[str]:
    """Every tag on a note that has a card in this deck (or a subdeck).

    `col.tags.all()` is collection-wide, which for this user would list
    every tag in every course. Scoping means going through the notes, and
    reading `notes.tags` in one query beats loading N Note objects on a
    menu-open."""
    try:
        nids = col.find_notes(_deck_search(col, deck_name))
    except Exception:
        return []
    if not nids:
        return []
    rows: List[str] = []
    try:
        from anki.utils import ids2str

        rows = col.db.list(
            f"select distinct tags from notes where id in {ids2str(nids)}"
        )
    except Exception:
        # DBProxy is not part of the stable API; fall back to the slow but
        # supported path rather than losing the feature.
        try:
            rows = [" ".join(col.get_note(nid).tags) for nid in nids]
        except Exception:
            return []
    tags = set()
    for row in rows:
        for tag in (row or "").split():
            if tag:
                tags.add(tag)
    return sorted(tags, key=lambda s: s.lower())


def _tag_tree(tags: List[str]) -> Dict[str, Any]:
    """Nest `a::b::c` into {a: {b: {c: {}}}}, preserving sort order."""
    root: Dict[str, Any] = {}
    for tag in tags:
        node = root
        for part in tag.split("::"):
            if not part:
                continue
            node = node.setdefault(part, {})
    return root


# --------------------------------------------------------------------------- #
# Actions
# --------------------------------------------------------------------------- #
def randomize(reviewer: Any) -> None:
    """Shuffle everything you'd study today in this deck into one queue."""
    src = _source_deck(reviewer)
    col = getattr(mw, "col", None)
    if src is None or col is None:
        _tooltip("No deck to randomize from here")
        return
    _did, name = src
    search = (
        f"{_deck_search(col, name)} (is:due OR is:new) "
        "-is:suspended -is:buried"
    )
    _build(
        MIX_DECK,
        search,
        "RANDOM",
        log_tag="randomize",
        empty="Nothing left to study in this deck",
    )


def cue_tag(reviewer: Any, tag: str) -> None:
    """Pull every card in this deck carrying `tag` into a study queue.

    Deliberately not limited to due/new: the point of cueing a tag is to
    drill it now. `reschedule = True` means an early answer is scheduled
    the way Anki schedules any early answer, rather than being discarded.

    Anki's `tag:` search already matches child tags (the backend compiles
    it to `.* tag(::| ).*`), so cueing a parent cues everything under it.
    """
    src = _source_deck(reviewer)
    col = getattr(mw, "col", None)
    if src is None or col is None:
        _tooltip("No deck to cue from here")
        return
    _did, name = src
    from anki.collection import SearchNode

    tag_term = col.build_search_string(SearchNode(tag=tag))
    search = (
        f"{_deck_search(col, name)} {tag_term} -is:suspended -is:buried"
    )
    _build(
        "Study: " + tag.replace("::", " / "),
        search,
        "DUE",
        log_tag=f"cue {tag}",
        empty=f"No cards tagged {tag} here",
    )


def _build(name: str, search: str, order: str, log_tag: str, empty: str) -> None:
    from . import build_study_deck

    build_study_deck(
        name, search, order, log_tag=log_tag, empty_message=empty
    )


def _tooltip(msg: str) -> None:
    try:
        from aqt.utils import tooltip

        tooltip(msg, parent=mw)
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# Menu
# --------------------------------------------------------------------------- #
def _populate_tag_menu(
    menu: QMenu, node: Dict[str, Any], prefix: str, reviewer: Any, depth: int
) -> None:
    for name, children in node.items():
        full = f"{prefix}::{name}" if prefix else name
        if children and depth < MAX_MENU_DEPTH:
            sub = menu.addMenu(name)
            # The parent tag is itself cueable — and cues its children with
            # it — so it gets the first row rather than being unreachable.
            act = sub.addAction(f"All of {name}")
            act.triggered.connect(
                lambda _checked=False, t=full: cue_tag(reviewer, t)
            )
            sub.addSeparator()
            _populate_tag_menu(sub, children, full, reviewer, depth + 1)
        elif children:
            # Past the nesting cap: flatten, showing the remaining path.
            act = menu.addAction(name)
            act.triggered.connect(
                lambda _checked=False, t=full: cue_tag(reviewer, t)
            )
            for leaf in _flatten(children, name):
                a = menu.addAction("    " + leaf)
                a.triggered.connect(
                    lambda _checked=False, t=f"{full}::{leaf}": cue_tag(
                        reviewer, t
                    )
                )
        else:
            act = menu.addAction(name)
            act.triggered.connect(
                lambda _checked=False, t=full: cue_tag(reviewer, t)
            )


def _flatten(node: Dict[str, Any], prefix: str) -> List[str]:
    out: List[str] = []
    for name, children in node.items():
        if children:
            out.extend(f"{name}::{rest}" for rest in _flatten(children, name))
        else:
            out.append(name)
    return out


def on_will_show_context_menu(reviewer: Any, menu: QMenu) -> None:
    """`gui_hooks.reviewer_will_show_context_menu` — the "More" menu."""
    if not enabled():
        return
    col = getattr(mw, "col", None)
    if col is None:
        return
    try:
        menu.addSeparator()
        act = menu.addAction("Randomize Set")
        act.setToolTip(
            "Shuffle everything due in this deck into one random queue"
        )
        act.triggered.connect(
            lambda _checked=False: randomize(reviewer)
        )

        cue = menu.addMenu("Cue…")
        src = _source_deck(reviewer)
        tags = deck_tags(col, src[1]) if src else []
        if not tags:
            empty = cue.addAction(
                "No tags in this deck" if src else "No deck to cue from"
            )
            empty.setEnabled(False)
            return
        _populate_tag_menu(cue, _tag_tree(tags), "", reviewer, 0)
    except Exception as exc:  # never take the More menu down with us
        try:
            from . import _dev_cmd_log

            _dev_cmd_log(f"reviewer_menu: {exc!r}")
        except Exception:
            pass
