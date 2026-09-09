"""Anki Design — two additions to the reviewer's "More" menu.

**Randomize Set** shuffles the new cards of the deck you're studying
where they stand, by rewriting their positions — no new deck, nothing
moved. It's also on the deck list's gear menu (`web/deckopts.js`), where
it shuffles that deck instead of the one being studied.

**Search Google for "…"** appears on the webview's own right-click menu
whenever text is selected on a card, in the reviewer or the Browse
preview pane.

**Cue…** lists every tag actually present in the deck you're studying and
turns any of them into an immediate study queue — the thing you reach for
when a lecture, a chapter or a card type suddenly needs drilling. Cueing
a *subset* genuinely needs a filtered deck (there is nowhere else for a
temporary selection to live), so that one still goes through
`build_study_deck()` in `__init__.py`, the same
rescheduling-filtered-deck path the sidebar's Due / New / Learning totals
use, and answers count exactly as normal reviews do.

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
    """Shuffle the deck you're studying, in place."""
    src = _source_deck(reviewer)
    if src is None:
        _tooltip("No deck to randomize from here")
        return
    randomize_deck(src[0], from_reviewer=True)


def randomize_deck(did: int, from_reviewer: bool = False) -> None:
    """Shuffle a deck's new cards where they stand.

    Anki already keeps an order for new cards — the position in the `due`
    column — and the scheduler's default gather priority reads new cards
    straight off it (`Deck` → `LowestPosition` → `ORDER BY due ASC`). So
    "randomize this deck" needs nothing more than rewriting those
    positions: `reposition_new_cards(..., randomize=True)`, the same
    backend call as the Browser's Reposition dialog, run through a
    CollectionOp so Ctrl+Z puts the old order back.

    This replaces an earlier version that built a filtered "Study: Mix"
    deck. That worked, but it moved every card out of the deck to do it —
    a lot of machinery, and a lot of surprise, for "shuffle these". The
    user's instinct was right: it is very simple.

    `shift_existing=False`: we rewrite this deck's positions from 0
    anyway, and nothing outside the deck needs to move out of the way.
    """
    col = getattr(mw, "col", None)
    if col is None:
        return
    try:
        name = col.decks.name(did)
    except Exception:
        _tooltip("That deck no longer exists")
        return
    try:
        # Only new cards carry a position. Suspended ones are left alone:
        # they aren't in the queue, and shuffling them would be an
        # invisible change to something parked deliberately.
        cids = col.find_cards(f"{_deck_search(col, name)} is:new -is:suspended")
    except Exception:
        cids = []
    if not cids:
        _tooltip(f"No new cards to shuffle in “{name}”")
        return

    def on_done(out: Any) -> None:
        count = getattr(out, "count", 0) or len(cids)
        _tooltip(f"Shuffled {count} cards in “{name}”")
        if from_reviewer:
            _refresh_queue()

    try:
        from aqt.operations.scheduling import reposition_new_cards

        reposition_new_cards(
            parent=mw,
            card_ids=cids,
            starting_from=0,
            step_size=1,
            randomize=True,
            shift_existing=False,
        ).success(on_done).run_in_background()
    except Exception as exc:
        _log(f"randomize {name}: {exc!r}")
        _tooltip("Couldn't shuffle that deck")


def _refresh_queue() -> None:
    """Rebuild the study queue so the *next* card reflects the shuffle.

    Without this the reviewer keeps the card it already fetched and the
    queue it already built, so shuffling mid-session appears to do
    nothing until you leave and come back. `mw.reset()` is the supported
    way to say "the queues are stale" — it fires `operation_did_execute`,
    which is what makes the reviewer rebuild.

    `nextCard()` on top of that is deliberate. Anki keeps the card you're
    looking at across a reset, which is right for almost every operation
    and wrong for this one: you pressed Randomize *because* you were tired
    of the card in front of you, and leaving it there is exactly the "the
    button did nothing" impression this feature has already been bitten
    by. The card is unanswered, so it just goes back in the shuffled
    queue.
    """
    try:
        if getattr(mw, "state", "") != "review":
            return
        mw.reset()
        rv = getattr(mw, "reviewer", None)
        if rv is not None:
            rv.nextCard()
    except Exception as exc:
        _log(f"refresh queue: {exc!r}")


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


def _log(msg: str) -> None:
    try:
        from . import _dev_cmd_log

        _dev_cmd_log(msg)
    except Exception:
        pass


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


# --------------------------------------------------------------------------- #
# Look up selected text
# --------------------------------------------------------------------------- #
# Google's URL is fine with long queries but the menu label is not, and
# neither is anyone's patience: show enough to recognise what you picked.
LOOKUP_LABEL_CHARS = 40
LOOKUP_QUERY_CHARS = 300


def lookup_enabled() -> bool:
    return bool(_config().get("reviewer_text_lookup", True))


def search_google(text: str) -> None:
    from urllib.parse import quote_plus

    from aqt.qt import QDesktopServices, QUrl

    query = " ".join(text.split())[:LOOKUP_QUERY_CHARS]
    if not query:
        return
    QDesktopServices.openUrl(
        QUrl("https://www.google.com/search?q=" + quote_plus(query))
    )


def _card_webview(webview: Any) -> bool:
    """True for the webviews that show a rendered card.

    The reviewer draws into `mw.web` (kind MAIN), which is also the deck
    list and the overview — hence the state check. PREVIEWER covers both
    Anki's own preview window and the Browse tab's pane.
    """
    try:
        from aqt.webview import AnkiWebViewKind

        kind = getattr(webview, "_kind", None)
        if kind == AnkiWebViewKind.PREVIEWER:
            return True
        if kind == AnkiWebViewKind.MAIN:
            return getattr(mw, "state", "") == "review"
    except Exception:
        pass
    return False


def on_webview_will_show_context_menu(webview: Any, menu: QMenu) -> None:
    """Right-clicking a selection on a card offers to look it up.

    "Allow me to highlight text in a card so if I want I can go look
    something up on Google." The highlighting half is CSS (theme.css used
    to set `user-select: none` on `body`, which reached the card); this is
    the other half — the two-click path from "that word is unfamiliar" to
    a search, without leaving the card or retyping it.
    """
    if not lookup_enabled() or not _card_webview(webview):
        return
    try:
        if not webview.hasSelection():
            return
        text = " ".join((webview.selectedText() or "").split())
        if not text:
            return
        label = text
        if len(label) > LOOKUP_LABEL_CHARS:
            label = label[: LOOKUP_LABEL_CHARS - 1].rstrip() + "…"
        if menu.actions():
            menu.addSeparator()
        act = menu.addAction(f'Search Google for “{label}”')
        act.triggered.connect(lambda _checked=False, t=text: search_google(t))
    except Exception as exc:
        _log(f"lookup menu: {exc!r}")


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
        act.setToolTip("Shuffle this deck's new cards into a random order")
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
