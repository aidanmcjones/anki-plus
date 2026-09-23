"""Decide whether a ticket is an app bug (anki-design add-on) or a deck bug."""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

from .config import Config


def _count(text: str, words: List[str]) -> List[str]:
    hits = []
    for w in words:
        if re.search(r"(?<![a-z0-9])" + re.escape(w.lower()) + r"(?![a-z0-9])", text):
            hits.append(w)
    return hits


def classify(ticket: Dict[str, Any], cfg: Config, override: Optional[str] = None) -> Tuple[str, str]:
    """Return (kind, reason). kind is 'app' or 'deck'."""
    if override:
        if override not in ("app", "deck"):
            raise ValueError("--kind must be app or deck")
        return override, f"--kind {override}"

    reviewer = ticket.get("reviewer") or {}
    notetype = reviewer.get("notetype") or ""
    text = " ".join(str(ticket.get(k) or "") for k in ("note", "expected")).lower()

    course_prefix = next((p for p in cfg.course_notetype_prefixes if notetype.startswith(p)), None)
    if course_prefix:
        # The notetype prefix is evidence a course card was on screen when the
        # ticket was captured, not a verdict on what the ticket is about: a
        # ticket captured while a course card happened to be showing (e.g.
        # "right-click an image and add copy/cut/paste to the pop-up menu")
        # can still be an app feature request. Content/rendering words about
        # the card win first (a stray UI-ish word like "image" or "cut" in
        # "the image on this card is cut off" must not outrun an explicit
        # reference to the card itself); only when the note says nothing
        # about the card's content do UI verbs/nouns route it to app; with
        # neither, the prefix's original default (deck) stands.
        content_hits = _count(text, cfg.course_content_keywords)
        if content_hits:
            return "deck", (
                f"notetype {notetype!r} (course prefix {course_prefix!r}); "
                f"note is about the card's content/rendering {content_hits}"
            )
        app_signal_hits = _count(text, cfg.course_app_signal_keywords)
        if app_signal_hits:
            return "app", (
                f"notetype {notetype!r} (course prefix {course_prefix!r}) is evidence, not a "
                f"verdict; note has app signals {app_signal_hits}"
            )
        return "deck", f"notetype {notetype!r} starts with course prefix {course_prefix!r}; no signals either way"

    # longest-first so "deck browser" is consumed before "deck" is counted
    app_hits = _count(text, sorted(cfg.app_keywords, key=len, reverse=True))
    stripped = text
    for phrase in sorted(cfg.app_phrases, key=len, reverse=True):
        stripped = re.sub(re.escape(phrase.lower()), " ", stripped)
    deck_hits = _count(stripped, cfg.deck_keywords)
    # collapse nested hits ("image on card" also contains "card")
    deck_hits = [h for h in deck_hits if not any(h != o and h in o for o in deck_hits)]
    app_hits = [h for h in app_hits if not any(h != o and h in o for o in app_hits)]

    capture = ticket.get("capture") or {}
    app_score: float = len(app_hits) + (1 if capture.get("console_errors") else 0)
    deck_score = len(deck_hits)
    # not captured on a card (no notetype, reviewer not showing a question or
    # answer): lean app, but less than one deck word
    on_card = bool(notetype) or (
        reviewer.get("state") in ("question", "answer")
        and ("card_id" not in reviewer or reviewer.get("card_id") is not None)
    )
    lean = "" if on_card else "; not captured on a card, leaning app"
    if not on_card:
        app_score += cfg.no_card_app_bias

    if deck_score > app_score:
        return "deck", f"note mentions {deck_hits} (app hints: {app_hits}{lean})"
    if app_hits or capture.get("console_errors"):
        return "app", f"note mentions {app_hits or 'console errors'} (deck hints: {deck_hits}{lean})"
    if deck_hits:
        return "app", f"deck hints {deck_hits} do not outweigh app lean{lean}; defaulting to app"
    return "app", "no course notetype and no deck keywords; defaulting to app"
