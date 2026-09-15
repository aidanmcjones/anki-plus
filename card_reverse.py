"""Anki Design — reversible cards.

"I want to select cards in Browse and reverse them — show the back first
and the front as the answer." A toggle on the Browse context menu, purely
presentational: the card's actual fields, templates and scheduling are
never touched.

Storage
-------
A set of reversed card ids, in the add-on's own config (``mw.addonManager
.getConfig``/``writeConfig`` — the same mechanism `deadlines.py` uses for
its per-deck state, NOT the ``config.json`` source file):

    "reversed_cards": {"<card id>": true, …}

Why per-card config rather than a tag:
  * A tag lives on the *note*, so tagging would flip every card the note
    generates — one card of a 5-card cloze note reversed would reverse
    all 5. The user asked to reverse *cards* they select in Browse, and
    Browse selection is card-granular (a multi-card note's row expands to
    all of its cards — see `selected_cards()` — but a Cards-mode row is
    exactly one card).
  * It's fully non-destructive: nothing is written to the collection, so
    it can never desync scheduling, can't conflict with sync, and needs
    no schema migration.
  * It degrades safely: an id for a since-deleted card just sits unused
    in the dict forever (a few bytes of dead config, never touched again
    since we only ever look cards up by id starting from a live
    `Card`/`CardId` the collection just handed us — never the reverse).

Presentation
------------
Applied in `on_card_will_show` (the same `card_will_show` hook
`editreviewer.py` uses), which receives the ALREADY RENDERED question/
answer HTML for a card. For a reversed card we don't touch that text —
we go back to the card and pull its *other* side's rendered HTML instead:

  * reversed question := the back-only portion of `card.answer()` (i.e.
    the real Back content, with the redundant `{{FrontSide}}` echo that
    Anki's stock Answer template starts with stripped off — see
    `_split_answer`).
  * reversed answer := that same back-only content, followed by the
    ORIGINAL front (`card.question()`) underneath a fresh `<hr id=answer>`
    — exactly the shape Anki's own "and reversed card" Card 2 produces
    natively, just computed for an arbitrary single card instead of a
    second notetype-generated card.

Cards we refuse to reverse (`eligibility`), and why:
  * Cloze notetypes (`note_type()["type"] == MODEL_CLOZE`) — this also
    covers Image Occlusion, which ships as a cloze notetype under the
    hood (`rslib/src/image_occlusion/notetype.rs`). A cloze's "question"
    and "answer" aren't two sides of a card, they're the same text with
    one deletion hidden vs. shown (and, for Image Occlusion, the masked
    image); swapping them would show the fully-revealed content as the
    question, defeating the point rather than reversing anything
    meaningful.
  * Templates whose rendered answer has no `<hr id=answer>` marker — we
    have no reliable way to separate "echoed front" from "actual back"
    without it, and guessing would risk rendering nonsense.
  * An empty front or empty back (nothing to show on one side).

Eligibility is re-checked at render time too (`on_card_will_show`), not
just when the menu toggle is applied — if a note's template changes after
a card was flagged, we silently fall back to the normal, unreversed
rendering rather than show something broken.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Sequence, Tuple

from aqt import mw
from aqt.qt import QMenu

ADDON = __name__.split(".")[0]
STATE_KEY = "reversed_cards"

# The "Card"/"Card Type" browser column's backend key is "template" (see
# `Column::Cards` in rslib/src/browser_table.rs — it serialises to
# "template", not "card"). In Cards mode it holds the template name
# ("Card 1"); in Notes mode it holds the note's card count ("3", "4"…).
CARD_COLUMN_KEY = "template"
REVERSED_MARK = "↺"  # ↺


# --------------------------------------------------------------------------- #
# Config plumbing (mirrors deadlines.py's pattern).
# --------------------------------------------------------------------------- #
def _config() -> Dict[str, Any]:
    try:
        return mw.addonManager.getConfig(ADDON) or {}
    except Exception:
        return {}


def _write(cfg: Dict[str, Any]) -> None:
    try:
        mw.addonManager.writeConfig(ADDON, cfg)
    except Exception:
        pass


def _state() -> Dict[str, bool]:
    raw = _config().get(STATE_KEY)
    return dict(raw) if isinstance(raw, dict) else {}


def _save_state(state: Dict[str, bool]) -> None:
    cfg = _config()
    cfg[STATE_KEY] = state
    _write(cfg)


def is_reversed(card_id: int) -> bool:
    return bool(_state().get(str(card_id)))


def _log(msg: str) -> None:
    try:
        from . import _dev_cmd_log
        _dev_cmd_log(f"card_reverse: {msg}")
    except Exception:
        pass


def _tooltip(msg: str) -> None:
    try:
        from aqt.utils import tooltip
        tooltip(msg, parent=mw)
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# Front/back extraction.
# --------------------------------------------------------------------------- #
_HR_ANSWER_RE = re.compile(r'<hr\s+id=["\']?answer["\']?[^>]*>', re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]+>")


def _strip_tags(text: str) -> str:
    return _TAG_RE.sub("", text or "")


def _split_answer(answer_html: str) -> Optional[Tuple[str, str]]:
    """Split a rendered `card.answer()` string at the `<hr id=answer>`
    marker stock templates use to separate the echoed front from the real
    back. Returns (front_echo, back_only), or None if the marker isn't
    present (a customised template we can't safely reason about)."""
    if not answer_html:
        return None
    m = _HR_ANSWER_RE.search(answer_html)
    if not m:
        return None
    return answer_html[: m.start()], answer_html[m.end():]


def eligibility(card: Any) -> Optional[str]:
    """None if `card` is safe to reverse; otherwise a human-readable
    reason it isn't."""
    try:
        nt = card.note_type()
    except Exception:
        return "Couldn't read this card's note type."
    try:
        from anki.consts import MODEL_CLOZE
    except Exception:
        MODEL_CLOZE = 1
    if nt is not None and nt.get("type") == MODEL_CLOZE:
        return (
            "Cloze cards (including Image Occlusion) don't have separate "
            "front/back sides — reversing one would just show the fully "
            "revealed card as the question."
        )
    try:
        question = card.question()
        answer = card.answer()
    except Exception:
        return "Couldn't render this card."
    split = _split_answer(answer)
    if split is None:
        return (
            "This card's template doesn't use the usual answer marker "
            "(<hr id=answer>), so the back can't be reliably separated "
            "from the front."
        )
    _, back_only = split
    if not _strip_tags(question).strip():
        return "This card's front is empty."
    if not _strip_tags(back_only).strip():
        return "This card's back is empty."
    return None


def _reversed_question(card: Any) -> Optional[str]:
    try:
        answer = card.answer()
    except Exception:
        return None
    split = _split_answer(answer)
    if split is None:
        return None
    _, back_only = split
    return back_only


def _reversed_answer(card: Any) -> Optional[str]:
    try:
        question = card.question()
        answer = card.answer()
    except Exception:
        return None
    split = _split_answer(answer)
    if split is None:
        return None
    _, back_only = split
    return f"{back_only}\n\n<hr id=answer>\n\n{question}"


# --------------------------------------------------------------------------- #
# card_will_show — the actual reversal.
# --------------------------------------------------------------------------- #
def on_card_will_show(text: str, card: Any, kind: str) -> str:
    if kind not in ("reviewQuestion", "reviewAnswer"):
        return text
    try:
        cid = card.id
    except Exception:
        return text
    if not is_reversed(cid):
        return text
    # Re-checked here (not just at toggle time): if the note's template
    # changed since this card was flagged, fall back to the normal render
    # rather than risk showing something broken.
    if eligibility(card) is not None:
        return text
    try:
        if kind == "reviewQuestion":
            swapped = _reversed_question(card)
        else:
            swapped = _reversed_answer(card)
    except Exception as exc:
        _log(f"render: {exc!r}")
        return text
    return swapped if swapped is not None else text


# --------------------------------------------------------------------------- #
# Browse: toggle on the Notes context menu.
# --------------------------------------------------------------------------- #
def apply_toggle(cards: Sequence[Any], reverse: bool) -> Tuple[int, Dict[str, int]]:
    """Flip `reverse` for each card. Returns (applied_count, {reason:
    skipped_count}) — `reverse=False` never skips anything (removing the
    flag is always safe)."""
    state = _state()
    applied = 0
    skipped: Dict[str, int] = {}
    for card in cards:
        try:
            cid = str(card.id)
        except Exception:
            continue
        if reverse:
            reason = eligibility(card)
            if reason:
                skipped[reason] = skipped.get(reason, 0) + 1
                continue
            if cid not in state:
                state[cid] = True
                applied += 1
        else:
            if cid in state:
                del state[cid]
                applied += 1
    _save_state(state)
    return applied, skipped


def _report(applied: int, skipped: Dict[str, int], reverse: bool) -> None:
    verb = "Reversed" if reverse else "Un-reversed"
    parts = [f"{verb} {applied} card{'s' if applied != 1 else ''}"]
    total_skipped = sum(skipped.values())
    if total_skipped:
        parts.append(f"skipped {total_skipped}")
    msg = ", ".join(parts) + "."
    if skipped:
        # One line per distinct reason keeps this readable even when a
        # mixed selection hits more than one refusal.
        for reason, n in skipped.items():
            msg += f"\n• {n}: {reason}"
    _tooltip(msg)


def _refresh_browser(browser: Any) -> None:
    try:
        browser.table.redraw_cells()
    except Exception:
        pass


def on_browser_will_show_context_menu(browser: Any, menu: QMenu) -> None:
    """`gui_hooks.browser_will_show_context_menu` — the Notes context menu
    (Add Notes / Create Copy / Change Note Type / Cards ▸ …)."""
    try:
        cids = browser.selected_cards()
    except Exception:
        return
    if not cids:
        return
    col = getattr(mw, "col", None)
    if col is None:
        return
    cards: List[Any] = []
    for cid in cids:
        try:
            cards.append(col.get_card(cid))
        except Exception:
            continue
    if not cards:
        return

    try:
        state = _state()
        n_reversed = sum(1 for c in cards if str(c.id) in state)
        all_reversed = n_reversed == len(cards)
        plural = "cards" if len(cards) != 1 else "card"
        label = f"Un-reverse {plural}" if all_reversed else f"Reverse {plural}"

        menu.addSeparator()
        act = menu.addAction(label)
        act.setCheckable(True)
        act.setChecked(all_reversed)
        act.setToolTip(
            "Show this card's back as the question and its front as the "
            "answer. Presentation only — fields and scheduling are "
            "untouched."
        )

        want_reverse = not all_reversed

        def _run(_checked: bool = False, _cards=cards, _reverse=want_reverse,
                  _browser=browser) -> None:
            applied, skipped = apply_toggle(_cards, _reverse)
            _refresh_browser(_browser)
            _report(applied, skipped, _reverse)

        act.triggered.connect(_run)
    except Exception as exc:
        _log(f"context menu: {exc!r}")


# --------------------------------------------------------------------------- #
# Browse: show reversed state in the "Card"/"Card Type" column.
# --------------------------------------------------------------------------- #
def on_browser_did_fetch_row(
    item_id: Any, is_note: bool, row: Any, columns: Sequence[str]
) -> None:
    try:
        idx = list(columns).index(CARD_COLUMN_KEY)
    except ValueError:
        return
    state = _state()
    if not state:
        return
    try:
        if is_note:
            cids = mw.col.card_ids_of_note(item_id)
            hit = any(str(cid) in state for cid in cids)
        else:
            hit = str(item_id) in state
    except Exception:
        return
    if not hit:
        return
    try:
        cell = row.cells[idx]
        cell.text = f"{cell.text} {REVERSED_MARK}" if cell.text else REVERSED_MARK
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# Registration.
# --------------------------------------------------------------------------- #
def register() -> None:
    from aqt import gui_hooks

    # Registered ahead of editreviewer's card_will_show handler (see
    # __init__.py) so field-wrap markers get applied to the text WE
    # produce, not the original — inline editing keeps working on
    # reversed cards.
    try:
        gui_hooks.card_will_show.append(on_card_will_show)
    except Exception:
        pass
    try:
        gui_hooks.browser_will_show_context_menu.append(
            on_browser_will_show_context_menu
        )
    except Exception:
        pass
    try:
        gui_hooks.browser_did_fetch_row.append(on_browser_did_fetch_row)
    except Exception:
        pass
