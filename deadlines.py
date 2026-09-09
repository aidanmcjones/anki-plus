"""Anki Design — per-deck memorization deadlines.

"Add a memorized-by-date calendar input so I can set a hard deadline for
when I need to know every single card in each deck. This should then play
into how the different card difficulty selections and timeframes are
built out — if a deck needs to be memorized by next Tuesday but Easy puts
the card 2 weeks out, that will not work."

Enforcement mechanism
---------------------
Anki already has exactly the right lever: the deck preset's **maximum
interval**. Set it to the number of days left and every ease button —
including Easy — is clamped by the v3 scheduler before it ever reaches
the answer buttons, so the *previewed* times shrink too. We don't have to
second-guess the scheduler, only tell it the ceiling.

Two consequences fall out of that choice, and both are handled here:

  * **Presets are shared.** Capping the preset a deck happens to use
    would silently cap every other deck sharing it. So a deadlined deck
    gets its own clone ("<preset> — deadline"), and only if it doesn't
    already have one. The original preset id is remembered so clearing
    the deadline puts the deck back on it.
  * **The cap has to shrink.** Days-left changes every night, so the
    ceiling is recomputed on profile open and on the first entry into the
    reviewer each day.

Pacing
------
A ceiling on intervals only solves half of "know every single card": if
there are 212 new cards and 6 days at 20/day, the deck cannot be seen
through in time, never mind memorised. The dialog does that arithmetic
and offers to raise the limit; the daily refresh re-checks it and says so
once when it stops adding up.

Storage
-------
The add-on's own config, keyed by deck id — per deck, not per preset, and
it survives restarts without touching the collection schema:

    "deck_deadlines": {
        "<did>": {
            "date": "2026-09-15",     # ISO, local dates throughout
            "orig_conf": 1,           # preset to restore on clear
            "cloned_conf": 173…,      # preset we made (0 = none)
            "orig_max_ivl": 36500     # ceiling to restore on clear
        }
    }
"""

from __future__ import annotations

import datetime
import math
from typing import Any, Dict, List, Optional, Tuple

from aqt import mw


ADDON = __name__.split(".")[0]

CLONE_SUFFIX = " — deadline"
DEFAULT_MAX_IVL = 36500
STATE_KEY = "deck_deadlines"
LAST_CHECK_KEY = "deck_deadlines_last_check"


# --------------------------------------------------------------------------- #
# Config plumbing
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


def enabled() -> bool:
    return bool(_config().get("deck_deadlines", True))


def _state() -> Dict[str, Any]:
    raw = _config().get(STATE_KEY)
    return dict(raw) if isinstance(raw, dict) else {}


def _save_state(state: Dict[str, Any]) -> None:
    cfg = _config()
    cfg[STATE_KEY] = state
    _write(cfg)


def _today() -> datetime.date:
    """Anki's day, not the calendar's — a 4am rollover means "today" for
    scheduling purposes lags the wall clock in the small hours, and the
    countdown should agree with the scheduler."""
    try:
        # `day_cutoff` is the epoch second at which Anki's today *ends* —
        # the next rollover, so on the evening of the 9th with a 4am
        # rollover it already reads as the 10th. Stepping back a full day
        # lands on the previous rollover, whose date is Anki's today for
        # any rollover hour.
        cutoff = mw.col.sched.day_cutoff
        return datetime.date.fromtimestamp(cutoff - 86400)
    except Exception:
        return datetime.date.today()


def days_left(deadline: datetime.date) -> int:
    return (deadline - _today()).days


def parse(value: str) -> Optional[datetime.date]:
    try:
        return datetime.date.fromisoformat(value)
    except Exception:
        return None


def get(did: int) -> Optional[datetime.date]:
    entry = _state().get(str(did))
    if not isinstance(entry, dict):
        return None
    return parse(str(entry.get("date", "")))


def label_for(did: int) -> str:
    """Gear-menu label: the date plus how long is left."""
    deadline = get(did)
    if deadline is None:
        return "Memorize by…"
    left = days_left(deadline)
    stamp = deadline.strftime("%b %-d") if hasattr(deadline, "strftime") else str(deadline)
    if left < 0:
        return f"Memorize by {stamp} — passed"
    if left == 0:
        return f"Memorize by {stamp} — today"
    return f"Memorize by {stamp} — {left}d"


def menu_payload() -> Dict[str, str]:
    """`{did: label}` for the deck list, so the gear menu can show the
    countdown without a round-trip per deck."""
    if not enabled():
        return {}
    out: Dict[str, str] = {}
    for did in list(_state().keys()):
        try:
            out[str(did)] = label_for(int(did))
        except Exception:
            continue
    return out


# --------------------------------------------------------------------------- #
# Preset handling
# --------------------------------------------------------------------------- #
def _decks_using(col: Any, conf_id: int, exclude: int) -> List[int]:
    out = []
    for entry in col.decks.all_names_and_ids():
        if entry.id == exclude:
            continue
        try:
            deck = col.decks.get(entry.id)
            if not deck or deck.get("dyn"):
                continue
            if int(deck.get("conf", 1)) == int(conf_id):
                out.append(entry.id)
        except Exception:
            continue
    return out


def _ensure_own_preset(col: Any, did: int, entry: Dict[str, Any]) -> Dict[str, Any]:
    """Give this deck a preset it can have its ceiling changed on.

    If it already sits on the clone we made for it, reuse that. If its
    preset is used by nothing else, cap it in place — cloning would just
    litter the preset list. Otherwise clone, so the decks sharing the
    original are untouched.

    The clone is identified by the id we stored, never by its name. An
    earlier version looked one up by name, which quietly reunited decks
    that must not share: two decks on "Default" with different deadlines
    both landed on a single "Default — deadline" and overwrote each
    other's ceiling — the nearer deadline won, then lost, depending on
    iteration order. The name now carries the deck too, but that's for
    the human reading the preset list; the id is what we trust.
    """
    conf = col.decks.config_dict_for_deck_id(did)
    conf_id = int(conf["id"])
    cloned = int(entry.get("cloned_conf") or 0)
    if cloned and conf_id == cloned:
        return conf
    entry.setdefault("orig_conf", conf_id)
    entry.setdefault("orig_max_ivl", int(conf.get("rev", {}).get("maxIvl", DEFAULT_MAX_IVL)))
    if not _decks_using(col, conf_id, exclude=did):
        entry["cloned_conf"] = 0
        return conf
    leaf = str(col.decks.name(did)).split("::")[-1]
    name = f"{conf.get('name', 'Preset')} — {leaf}{CLONE_SUFFIX}"
    new_id = int(col.decks.add_config_returning_id(name, clone_from=conf))
    col.decks.set_config_id_for_deck_dict(col.decks.get(did), new_id)
    entry["cloned_conf"] = new_id
    return col.decks.config_dict_for_deck_id(did)


def _set_max_interval(col: Any, conf: Dict[str, Any], days: int) -> None:
    conf.setdefault("rev", {})["maxIvl"] = max(1, int(days))
    col.decks.update_config(conf)


# --------------------------------------------------------------------------- #
# Pacing
# --------------------------------------------------------------------------- #
def pacing(did: int) -> Optional[Dict[str, int]]:
    """Can this deck actually be *seen* before the deadline?

    None when there's no deadline or nothing new left. Otherwise the
    numbers behind "212 new cards, 6 days → needs ≥36/day; limit 20"."""
    col = getattr(mw, "col", None)
    deadline = get(did)
    if col is None or deadline is None:
        return None
    try:
        from anki.collection import SearchNode

        search = col.build_search_string(SearchNode(deck=col.decks.name(did)))
        remaining = len(col.find_cards(f"{search} is:new -is:suspended"))
    except Exception:
        return None
    if not remaining:
        return None
    left = max(1, days_left(deadline))
    try:
        per_day = int(col.decks.config_dict_for_deck_id(did)["new"]["perDay"])
    except Exception:
        per_day = 20
    needed = int(math.ceil(remaining / left))
    return {
        "remaining": remaining,
        "days": left,
        "needed": needed,
        "per_day": per_day,
        "short": 1 if needed > per_day else 0,
    }


def set_new_per_day(did: int, value: int) -> None:
    col = getattr(mw, "col", None)
    if col is None:
        return
    entry = _state().get(str(did))
    state = _state()
    if isinstance(entry, dict):
        conf = _ensure_own_preset(col, did, entry)
        state[str(did)] = entry
        _save_state(state)
    else:
        conf = col.decks.config_dict_for_deck_id(did)
    conf.setdefault("new", {})["perDay"] = max(1, int(value))
    col.decks.update_config(conf)


# --------------------------------------------------------------------------- #
# Set / clear / refresh
# --------------------------------------------------------------------------- #
def set_deadline(did: int, deadline: datetime.date) -> None:
    col = getattr(mw, "col", None)
    if col is None:
        return
    state = _state()
    entry = dict(state.get(str(did)) or {})
    entry["date"] = deadline.isoformat()
    conf = _ensure_own_preset(col, did, entry)
    _set_max_interval(col, conf, max(1, days_left(deadline)))
    state[str(did)] = entry
    _save_state(state)


def clear_deadline(did: int, notify: bool = False) -> None:
    """Put the deck back exactly where it was: original preset, original
    ceiling. The clone is left in the preset list rather than deleted —
    another deadlined deck may be sharing it, and removing a preset is a
    schema-mod operation that would force a full sync."""
    col = getattr(mw, "col", None)
    state = _state()
    entry = state.pop(str(did), None)
    _save_state(state)
    if col is None or not isinstance(entry, dict):
        return
    try:
        orig_conf = int(entry.get("orig_conf") or 0)
        cloned = int(entry.get("cloned_conf") or 0)
        if cloned and orig_conf:
            col.decks.set_config_id_for_deck_dict(col.decks.get(did), orig_conf)
        else:
            conf = col.decks.config_dict_for_deck_id(did)
            _set_max_interval(
                col, conf, int(entry.get("orig_max_ivl") or DEFAULT_MAX_IVL)
            )
    except Exception as exc:
        _log(f"clear deadline {did}: {exc!r}")
    if notify:
        _tooltip(f"Deadline for “{_deck_name(did)}” has passed — limits restored")


def refresh_all(force: bool = False) -> int:
    """Recompute every deadlined deck's ceiling. Cheap, and idempotent."""
    col = getattr(mw, "col", None)
    if col is None or not enabled():
        return 0
    today = _today().isoformat()
    cfg = _config()
    if not force and cfg.get(LAST_CHECK_KEY) == today:
        return 0
    touched = 0
    for did_s in list(_state().keys()):
        try:
            did = int(did_s)
        except Exception:
            continue
        deadline = get(did)
        if deadline is None:
            continue
        if not col.decks.get(did):
            clear_deadline(did)
            continue
        left = days_left(deadline)
        if left < 0:
            clear_deadline(did, notify=True)
            continue
        state = _state()
        entry = dict(state.get(did_s) or {})
        conf = _ensure_own_preset(col, did, entry)
        _set_max_interval(col, conf, max(1, left))
        state[did_s] = entry
        _save_state(state)
        touched += 1
        pace = pacing(did)
        if pace and pace["short"]:
            _tooltip(
                f"“{_deck_name(did)}”: {pace['remaining']} new in "
                f"{pace['days']}d needs {pace['needed']}/day, "
                f"limit is {pace['per_day']}"
            )
    cfg = _config()
    cfg[LAST_CHECK_KEY] = today
    _write(cfg)
    return touched


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _deck_name(did: int) -> str:
    try:
        return mw.col.decks.name(did)
    except Exception:
        return str(did)


def _log(msg: str) -> None:
    try:
        from . import _dev_cmd_log

        _dev_cmd_log(msg)
    except Exception:
        pass


def _tooltip(msg: str) -> None:
    try:
        from aqt.utils import tooltip

        tooltip(msg, parent=mw, period=6000)
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# Dialog
# --------------------------------------------------------------------------- #
def show_dialog(did: int) -> None:
    from aqt.qt import (
        QCalendarWidget,
        QDate,
        QDialog,
        QHBoxLayout,
        QLabel,
        QPushButton,
        QVBoxLayout,
        Qt,
    )

    from . import addcard as _addcard

    palette, _dark = _addcard._resolve_palette()
    accent = _config().get("accent", "#6c8cff")
    name = _deck_name(did)

    dlg = QDialog(mw)
    dlg.setWindowTitle("Memorize by")
    dlg.setObjectName("ba-deadline")
    dlg.setStyleSheet(_dialog_qss(palette, accent))

    root = QVBoxLayout(dlg)
    root.setContentsMargins(22, 20, 22, 18)
    root.setSpacing(12)

    title = QLabel(name)
    title.setProperty("role", "title")
    root.addWidget(title)

    sub = QLabel("Every card in this deck should be known by:")
    sub.setProperty("role", "sub")
    root.addWidget(sub)

    cal = QCalendarWidget()
    cal.setGridVisible(False)
    cal.setVerticalHeaderFormat(
        QCalendarWidget.VerticalHeaderFormat.NoVerticalHeader
    )
    cal.setMinimumDate(QDate.currentDate())
    current = get(did)
    if current:
        cal.setSelectedDate(QDate(current.year, current.month, current.day))
    root.addWidget(cal)

    note = QLabel("")
    note.setProperty("role", "note")
    note.setWordWrap(True)
    root.addWidget(note)

    raise_btn = QPushButton("Raise limit")
    raise_btn.setProperty("role", "quiet")
    raise_btn.hide()
    pace_state: Dict[str, int] = {}

    def refresh_note() -> None:
        sel = cal.selectedDate()
        chosen = datetime.date(sel.year(), sel.month(), sel.day())
        left = max(1, (chosen - _today()).days)
        col = mw.col
        try:
            from anki.collection import SearchNode

            search = col.build_search_string(SearchNode(deck=name))
            remaining = len(col.find_cards(f"{search} is:new -is:suspended"))
            per_day = int(col.decks.config_dict_for_deck_id(did)["new"]["perDay"])
        except Exception:
            note.setText("")
            return
        needed = int(math.ceil(remaining / left)) if remaining else 0
        day_s = "day" if left == 1 else "days"
        lines = [f"Intervals will be capped at {left} {day_s}."]
        if remaining and needed > per_day:
            lines.append(
                f"{remaining} new cards in {left} {day_s} needs "
                f"{needed}/day — this deck's limit is {per_day}."
            )
            pace_state["needed"] = needed
            raise_btn.setText(f"Raise limit to {needed}/day")
            raise_btn.show()
        else:
            if remaining:
                lines.append(
                    f"{remaining} new cards at {per_day}/day fits in "
                    f"{int(math.ceil(remaining / per_day))} days."
                )
            raise_btn.hide()
        note.setText("  ".join(lines))

    cal.selectionChanged.connect(refresh_note)
    refresh_note()

    row = QHBoxLayout()
    row.setSpacing(8)
    clear_btn = QPushButton("Clear deadline")
    clear_btn.setProperty("role", "quiet")
    row.addWidget(clear_btn)
    row.addWidget(raise_btn)
    row.addStretch(1)
    cancel = QPushButton("Cancel")
    cancel.setProperty("role", "quiet")
    row.addWidget(cancel)
    save = QPushButton("Set deadline")
    save.setProperty("role", "primary")
    save.setDefault(True)
    row.addWidget(save)
    root.addLayout(row)

    def on_raise() -> None:
        if pace_state.get("needed"):
            set_new_per_day(did, pace_state["needed"])
            refresh_note()

    def on_save() -> None:
        sel = cal.selectedDate()
        set_deadline(did, datetime.date(sel.year(), sel.month(), sel.day()))
        dlg.accept()
        _tooltip(f"“{name}” must be known by {sel.toString('MMM d')}")
        _refresh_deck_browser()

    def on_clear() -> None:
        clear_deadline(did)
        dlg.accept()
        _tooltip(f"Deadline cleared for “{name}”")
        _refresh_deck_browser()

    raise_btn.clicked.connect(on_raise)
    save.clicked.connect(on_save)
    cancel.clicked.connect(dlg.reject)
    clear_btn.clicked.connect(on_clear)
    clear_btn.setEnabled(current is not None)

    dlg.setWindowModality(Qt.WindowModality.ApplicationModal)
    dlg.resize(420, 480)
    dlg.show()


def _refresh_deck_browser() -> None:
    try:
        if getattr(mw, "state", "") == "deckBrowser":
            mw.deckBrowser.refresh()
    except Exception:
        pass


def _dialog_qss(p: Dict[str, str], accent: str) -> str:
    from . import addcard as _addcard

    return f"""
QDialog#ba-deadline {{ background: {p['paper']}; }}
QDialog#ba-deadline QLabel {{
    color: {p['ink']};
    font-family: {_addcard.SANS};
    font-size: 11pt;
    background: transparent;
}}
QDialog#ba-deadline QLabel[role="title"] {{
    font-family: {_addcard.SERIF};
    font-size: 19pt;
    font-weight: 500;
}}
QDialog#ba-deadline QLabel[role="sub"] {{ color: {p['ink_dim']}; }}
QDialog#ba-deadline QLabel[role="note"] {{
    color: {p['ink_dim']};
    font-size: 10pt;
    padding: 2px 0;
}}
QDialog#ba-deadline QCalendarWidget QWidget {{
    background: {p['panel']};
    color: {p['ink']};
    alternate-background-color: {p['panel']};
}}
QDialog#ba-deadline QCalendarWidget QAbstractItemView:enabled {{
    background: {p['panel']};
    color: {p['ink']};
    selection-background-color: {accent};
    selection-color: {p['paper']};
    outline: 0;
    font-size: 10.5pt;
}}
QDialog#ba-deadline QCalendarWidget QAbstractItemView:disabled {{
    color: {p['ink_faint']};
}}
QDialog#ba-deadline QCalendarWidget QToolButton {{
    background: transparent;
    color: {p['ink']};
    border: 0;
    border-radius: 6px;
    padding: 5px 10px;
    font-size: 11pt;
}}
QDialog#ba-deadline QCalendarWidget QToolButton:hover {{ background: {p['hover']}; }}
QDialog#ba-deadline QCalendarWidget QMenu {{ background: {p['panel']}; color: {p['ink']}; }}
QDialog#ba-deadline QCalendarWidget QSpinBox {{
    background: {p['field_bg']}; color: {p['ink']}; border: 1px solid {p['line']};
}}
QDialog#ba-deadline QPushButton[role="primary"] {{
    background: {p['ink']};
    color: {p['paper']};
    border: 1px solid {p['ink']};
    border-radius: 6px;
    padding: 7px 16px;
    font-family: {_addcard.SANS};
    font-size: 10.5pt;
    font-weight: 500;
}}
QDialog#ba-deadline QPushButton[role="primary"]:hover {{
    background: {p['ink_dim']}; border-color: {p['ink_dim']};
}}
QDialog#ba-deadline QPushButton[role="quiet"] {{
    background: transparent;
    color: {p['ink_dim']};
    border: 1px solid {p['line']};
    border-radius: 6px;
    padding: 7px 14px;
    font-family: {_addcard.SANS};
    font-size: 10.5pt;
}}
QDialog#ba-deadline QPushButton[role="quiet"]:hover {{
    color: {p['ink']}; border-color: {p['line2']};
}}
QDialog#ba-deadline QPushButton[role="quiet"]:disabled {{ color: {p['ink_faint']}; }}
"""
