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
    for prefix in cfg.course_notetype_prefixes:
        if notetype.startswith(prefix):
            return "deck", f"notetype {notetype!r} starts with course prefix {prefix!r}"

    text = " ".join(str(ticket.get(k) or "") for k in ("note", "expected")).lower()

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
