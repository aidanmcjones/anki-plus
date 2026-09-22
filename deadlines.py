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
    reviewer each day (`refresh_all`).
  * **The moment it's crossed has to fire promptly.** A deadline is a
    point in time, not just a date (see `_deadline_moment`), so waiting
    for the once-a-day `refresh_all` to notice would leave a deck capped
    for hours after e.g. an 8am deadline on a day Anki was opened at 7am.
    `check_due_transitions` is the cheap, event-driven counterpart that
    handles that — wired to deck-list renders, app state changes, and a
    single-shot timer armed for the next deadline moment (never a polling
    timer). `is_passed`/`has_passed` are the shared "is this deadline
    behind us right now" check both paths — and `label_for` — run through,
    so the label and the actual scheduling state can't disagree.
  * **The flag that marks a crossing "handled" has to earn that.**
    `notified_passed` gates both the one-time tooltip and, indirectly,
    whether `check_due_transitions` bothers re-applying a deck it's
    already looked at — so if it latched the moment the restore *code
    path ran*, rather than the moment the restore was confirmed to have
    actually landed on the collection, a write that silently didn't stick
    would strand a deck capped forever while its own bookkeeping insisted
    it was fine. `_apply` reports back whether it verified the restore
    (`_verify_max_interval`, a re-read straight from the collection, not
    the dict already in hand); `refresh_all` and `check_due_transitions`
    only call `_announce_crossing` — the thing that sets the flag — when
    that verification passed, and explicitly clear the flag otherwise so
    the next cheap check retries instead of giving up silently.

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
            "time": "16:30",          # optional, 24h "HH:MM"; absent means
                                       # "that date's collection rollover
                                       # hour" — the meaning every entry had
                                       # before the time picker existed, and
                                       # what old entries with no "time" key
                                       # keep meaning forever (see
                                       # `_deadline_moment`)
            "mode": "pause",          # optional; what the deck does once the
                                       # deadline is behind it. Written only
                                       # when the user actually answers that
                                       # question in the dialog — an entry
                                       # without it follows the
                                       # `deadline_passed_mode` config key
                                       # (default "pause") every time it is
                                       # read, so the default stays a default
                                       # rather than being frozen into state
                                       # (see `entry_mode`)
            "orig_conf": 1,           # preset to restore on clear
            "cloned_conf": 173…,      # preset we made (0 = none)
            "orig_max_ivl": 36500,    # ceiling to restore on clear
            "inherited_from": 17…     # optional; present when this entry
                                       # was written by a cascade from an
                                       # ancestor deck's deadline (see
                                       # "Cascade" below). Absent = set on
                                       # this deck directly.
        }
    }

Cascade
-------
A deadline on "Exam 1" has to reach "Exam 1::03 Protein Folding": the
ceiling lives on each deck's own preset, so capping the parent alone
caps a deck that holds no cards. `set_deadline` therefore writes the
same date/time/mode onto every descendant, marked `inherited_from`, and
`set_mode` / `clear_deadline` on the ancestor follow those marked
entries (and only those: a deadline the user set on a subdeck directly
is its own and is left alone). `refresh_all` re-runs the cascade daily,
so a subdeck created after the deadline was set picks it up too.
"""

from __future__ import annotations

import datetime
import math
from typing import Any, Dict, List, Optional, Tuple

from aqt import mw


ADDON = __name__.split(".")[0]

CLONE_SUFFIX = " — deadline"
# What a deck does once its deadline is behind it.
MODE_MAINTAIN = "maintain"   # cap comes off, FSRS upkeep carries on
MODE_PAUSE = "pause"         # deck stops presenting anything, reversibly
DEFAULT_MAX_IVL = 36500
STATE_KEY = "deck_deadlines"
LAST_CHECK_KEY = "deck_deadlines_last_check"
# Config key holding what a passed deadline does when the deck itself never
# recorded a choice. Its default is PAUSE, deliberately: a deadline that
# keeps presenting cards afterwards isn't a deadline. See `default_mode`.
PASSED_MODE_KEY = "deadline_passed_mode"
DEFAULT_PASSED_MODE = MODE_PAUSE


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


def _rollover_hour() -> int:
    """The collection's day-cutoff hour (0-23), e.g. 4 for a 4am rollover.

    Reuses the package's own `_rollover_hour` (in `__init__.py`, already
    relied on by the heatmap's `_day_shift_seconds`) rather than
    re-deriving it — that one reads
    `mw.col.get_preferences().scheduling.rollover` directly, with a
    `col.conf` fallback for older collections. Importing it keeps this
    module and the heatmap agreeing on the same rollover hour instead of
    risking two derivations drifting apart."""
    try:
        from . import _rollover_hour as _pkg_rollover_hour

        return _pkg_rollover_hour()
    except Exception:
        return 4


def _deadline_moment(
    deadline: datetime.date, time_of_day: Optional[datetime.time] = None
) -> datetime.datetime:
    """A deadline as a point in time, not just a calendar square.

    `time_of_day` is the value stored in a `deck_deadlines` entry's
    optional "time" key (see `get_time`). When it's absent — every entry
    created before the time picker existed, and any entry the user never
    touches the time control on — the date alone has no inherent clock
    time, and what it means for scheduling is concrete: a card is either
    seen before that date's rollover or it isn't, since rollover is the
    instant Anki's "today" changes. So "known by <date>" with no explicit
    time is read as "known by that date's rollover", the same boundary
    `_today()` already uses to decide which calendar day we're on. This
    fallback is permanent, not a migration step — old entries keep this
    meaning forever unless the user opens the dialog and picks a time.
    """
    return datetime.datetime.combine(
        deadline, time_of_day or datetime.time(hour=_rollover_hour())
    )


def _stamp(deadline: datetime.date, time_of_day: Optional[datetime.time] = None) -> str:
    """Compact local timestamp for deck-list display: "Sep 16, 4:00 AM".

    Time of day comes from the stored `time_of_day` when the entry has
    one, else the collection's actual rollover hour (see
    `_deadline_moment`), not a hardcoded midnight/4am, so it tracks the
    user's real day-cutoff preference and stays consistent with `_today()`.
    """
    try:
        moment = _deadline_moment(deadline, time_of_day)
        return moment.strftime("%b %-d, %-I:%M %p")
    except Exception:
        return deadline.strftime("%b %-d") if hasattr(deadline, "strftime") else str(deadline)


def parse(value: str) -> Optional[datetime.date]:
    try:
        return datetime.date.fromisoformat(value)
    except Exception:
        return None


def _parse_time(value: Any) -> Optional[datetime.time]:
    """Tolerant "HH:MM" (24h) parse for the optional "time" entry key.

    Anything that isn't a clean two-field 0-23 / 0-59 string — missing,
    malformed, corrupted by hand-editing meta.json — falls back to None,
    which callers treat as "no stored time" (i.e. the rollover-hour
    default), never as an error.
    """
    if not isinstance(value, str) or ":" not in value:
        return None
    try:
        hh, mm = value.split(":", 1)
        return datetime.time(hour=int(hh), minute=int(mm))
    except Exception:
        return None


def get(did: int) -> Optional[datetime.date]:
    entry = _state().get(str(did))
    if not isinstance(entry, dict):
        return None
    return parse(str(entry.get("date", "")))


def get_time(did: int) -> Optional[datetime.time]:
    """The stored time-of-day for a deck's deadline, or None if the entry
    predates the time picker (or never had its time touched) — in which
    case the deadline still means "that date's rollover hour"."""
    entry = _state().get(str(did))
    if not isinstance(entry, dict):
        return None
    return _parse_time(entry.get("time"))


def default_mode() -> str:
    """What a passed deadline does when its deck never recorded a choice.

    Read from the `deadline_passed_mode` config key, default **pause** —
    which is a deliberate divergence from how this feature originally
    shipped (implicit "maintain"). The reasoning is the user's: a deadline
    that keeps presenting cards after the date is not a deadline, it's a
    label. Six decks with a deadline of today still offering 385 reviews
    the evening it passed is the concrete failure that motivated it.

    Anything unrecognised in the config — a typo, a hand-edited
    meta.json — reads as the default rather than as an error, so a
    malformed key can never leave a deck in an undefined mode.
    """
    value = _config().get(PASSED_MODE_KEY, DEFAULT_PASSED_MODE)
    if isinstance(value, str) and value in (MODE_MAINTAIN, MODE_PAUSE):
        return value
    return DEFAULT_PASSED_MODE


def entry_mode(entry: Any) -> str:
    """Resolve one state entry's post-deadline mode.

    The single place that answers "what does this deck do now" — every
    caller (`get_mode`, `paused_deck_ids`, `_apply`, `_announce_crossing`)
    routes through it, so the label, the scheduler state and the sidebar
    exclusions can't disagree about a deck the way they could when each
    site inlined its own `entry.get("mode", MODE_MAINTAIN)`.

    An explicit per-deck choice always wins: if the entry records a valid
    `mode`, that is the answer and the config default is not consulted.
    Only an entry with no `mode` key (or an unrecognised one) falls back
    to `default_mode()` — which means the fallback stays *live*, so
    flipping the config key retroactively changes every deck that never
    made a choice of its own, without rewriting any state.
    """
    if isinstance(entry, dict):
        mode = entry.get("mode")
        if mode in (MODE_MAINTAIN, MODE_PAUSE):
            return str(mode)
    return default_mode()


def get_mode(did: int) -> str:
    return entry_mode(_state().get(str(did)))


def is_passed(deadline: datetime.date, time_of_day: Optional[datetime.time] = None) -> bool:
    """Whether a deadline is behind us *right now*, as a moment in time —
    not just a calendar date.

    This is the single source of truth for "has this deadline passed".
    `label_for`, `paused_deck_ids`, and the preset-restore transition in
    `_apply` all route through it (directly or via `has_passed`), so the
    label a user reads and what the scheduler actually does can never
    disagree — which is exactly what went wrong before: the label used
    `_deadline_moment` but `_apply`/`paused_deck_ids` still used bare
    `days_left() >= 0`, so a deadline of "today at 8am" read as "passed"
    in the gear menu for hours while the interval cap stayed on.

    `days_left` stays calendar-date based and keeps driving interval-cap
    *sizing* (`max(1, left)`) and the "Nd" countdown text — both only care
    about whole days, and shrinking the cap a day at a time as the
    deadline approaches is correct even before the deadline is passed.
    This function only answers the go/no-go question of whether the
    deadline has been crossed.
    """
    return _deadline_moment(deadline, time_of_day) <= datetime.datetime.now()


def has_passed(did: int) -> bool:
    """`is_passed` for a deck's stored deadline (False if it has none)."""
    deadline = get(did)
    if deadline is None:
        return False
    return is_passed(deadline, get_time(did))


def label_for(did: int) -> str:
    """Gear-menu label: the date (with time of day, from the stored time or
    the rollover) plus how long is left.

    `left` (calendar-day difference from `days_left`) is the single source
    of truth for the "Nd" countdown text and for interval-cap *sizing* —
    unchanged from before the time picker. Whether the deadline has been
    crossed — the "passed"/"today" distinction here, and the scheduling
    transition in `_apply` — is decided by `is_passed`, the real deadline
    moment against wall-clock now, so this label can never disagree with
    what `_apply`/`paused_deck_ids` actually do.
    """
    deadline = get(did)
    if deadline is None:
        return "Memorize by…"
    left = days_left(deadline)
    time_of_day = get_time(did)
    stamp = _stamp(deadline, time_of_day)
    passed = is_passed(deadline, time_of_day)
    if passed:
        # Past tense on purpose: the deck was memorized by that date, and
        # the row should read as an achievement rather than a miss.
        if get_mode(did) == MODE_PAUSE:
            return f"Memorized by {stamp} — paused"
        return f"Memorized by {stamp} — passed ✓"
    if left == 0:
        return f"Memorize by {stamp} — today"
    return f"Memorize by {stamp} — {left}d"


def paused_deck_ids() -> List[int]:
    """Decks whose deadline has passed and that chose "pause reviews".

    A pause is expressed as a per-day limit of 0, which is how Anki itself
    parks a deck — but per-day limits are a *deck queue* concept and a
    filtered deck ignores them entirely. So anything that gathers
    across the whole collection (the sidebar's Due/New/Learning queues) has
    to subtract these decks by hand, or the one feature that promises "stop
    showing me this deck" gets overruled by the one that promises "show me
    everything due".
    """
    if not enabled():
        return []
    out: List[int] = []
    for key, entry in _state().items():
        if not isinstance(entry, dict):
            continue
        if entry_mode(entry) != MODE_PAUSE:
            continue
        deadline = parse(str(entry.get("date", "")))
        if deadline is None:
            continue
        if not is_passed(deadline, _parse_time(entry.get("time"))):
            continue
        try:
            out.append(int(key))
        except Exception:
            continue
    return out


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


def descendants(col: Any, did: int) -> List[int]:
    """Ids of every deck nested under `did` (any depth), by name prefix."""
    try:
        prefix = str(col.decks.name(did)) + "::"
    except Exception:
        return []
    out: List[int] = []
    for row in col.decks.all_names_and_ids():
        try:
            if int(row.id) != int(did) and str(row.name).startswith(prefix):
                out.append(int(row.id))
        except Exception:
            continue
    return out


def _inherited_from(entry: Any, did: int) -> bool:
    return isinstance(entry, dict) and str(entry.get("inherited_from", "")) == str(did)


def _cascade(col: Any, did: int, parent: Dict[str, Any], state: Dict[str, Any]) -> int:
    """Write `parent`'s deadline onto every descendant of `did` that does not
    hold a deadline of its own, apply it, and record the result in `state`
    (the caller saves). Returns how many descendants were touched.

    The descendant keeps its own `orig_*` / `cloned_conf` bookkeeping — those
    describe *its* preset — and only takes date, time and mode from the
    ancestor. A descendant whose entry has no `inherited_from` was set by
    the user directly and is skipped."""
    touched = 0
    for child in descendants(col, did):
        cur = state.get(str(child))
        if isinstance(cur, dict) and cur.get("date") and "inherited_from" not in cur:
            continue  # the user's own deadline on this subdeck wins
        entry = dict(cur or {})
        entry["date"] = parent["date"]
        entry.pop("time", None)
        if parent.get("time"):
            entry["time"] = parent["time"]
        entry.pop("mode", None)
        if parent.get("mode") in (MODE_MAINTAIN, MODE_PAUSE):
            entry["mode"] = parent["mode"]
        entry["inherited_from"] = int(did)
        try:
            entry, verified = _apply(col, child, entry)
            if not verified:
                _log(f"cascade {did}->{child}: preset restore did not verify")
        except Exception as exc:
            _log(f"cascade {did}->{child}: {exc!r}")
            continue
        state[str(child)] = entry
        touched += 1
    return touched


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
    # Captured here, before anything is changed, so "Pause reviews" has
    # real numbers to put back rather than a guess at Anki's defaults.
    entry.setdefault("orig_new_per_day", int(conf.get("new", {}).get("perDay", 20)))
    entry.setdefault("orig_rev_per_day", int(conf.get("rev", {}).get("perDay", 200)))
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


def _verify_max_interval(col: Any, did: int, expected: int) -> bool:
    """Re-reads the deck's *actual* preset straight back from the
    collection — never trusting the dict `_set_max_interval` already had
    in hand — so a write that silently didn't stick (wrong conf object,
    stale id, an `update_config` whose effect never reached this deck)
    is caught before a caller latches `notified_passed` and loses the
    chance to retry. See `_apply`'s "passed" branch."""
    try:
        conf = col.decks.config_dict_for_deck_id(did)
        return int(conf.get("rev", {}).get("maxIvl", -1)) == int(expected)
    except Exception:
        return False


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
    per_day = effective_new_per_day(col, did)
    needed = int(math.ceil(remaining / left))
    return {
        "remaining": remaining,
        "days": left,
        "needed": needed,
        "per_day": per_day,
        "short": 1 if needed > per_day else 0,
    }


def effective_new_per_day(col: Any, did: int) -> int:
    """How many new cards this deck will actually introduce today.

    The v3 scheduler reads the *deck's* own limit first and only falls
    back to the preset — `current_new_limit(deck).unwrap_or(config
    .new_per_day)` in rslib's decks/limits.rs. Reading the preset alone,
    as this module used to, reports a number the scheduler may not be
    using, and then the dialog's pacing arithmetic is about the wrong
    limit.
    """
    try:
        deck = col.decks.get(did, default=False)
        if deck:
            today = col.sched.today
            day = deck.get("newLimitToday") or {}
            if day and int(day.get("today", -1)) == int(today):
                return int(day.get("limit", 0))
            own = deck.get("newLimit")
            if own is not None:
                return int(own)
    except Exception:
        pass
    try:
        return int(col.decks.config_dict_for_deck_id(did)["new"]["perDay"])
    except Exception:
        return 20


_cascading: List[int] = []   # re-entrancy guard for set_new_per_day's cascade


def set_new_per_day(did: int, value: int) -> bool:
    """Raise (or lower) how many new cards this deck introduces a day.

    Written as the deck's own `newLimit` — Anki's "This deck" override,
    the field deck options writes under Daily limits — and *not* as the
    preset's `new/perDay`, which is what this used to do. Two reasons,
    both of which broke the "Raise limit to N/day" button:

      * A preset is shared. The deck you set a deadline on is very often
        still on Default along with everything else (24 other decks, in
        the demo collection), so raising Default to 36/day to make one
        deck's deadline reachable quietly raised 24 unrelated decks —
        and made *their* remembered "original" limit 36 as well.
      * The deck's own limit is the one the scheduler consults first, so
        it holds whatever preset the deck later ends up on, including the
        `— deadline` clone this module makes for it. A preset write could
        be — and was — undone by the next `_apply`.

    `newLimitToday` is cleared with it. Anki's "increase today's limit"
    writes that, and it outranks the persistent limit for the rest of the
    day; leaving it in place would mean pressing "Raise limit to 36/day"
    changed nothing at all today, which is the exact complaint.

    Returns True if the limit is now `value`.
    """
    col = getattr(mw, "col", None)
    if col is None:
        return False
    limit = max(1, int(value))
    state = _state()
    entry = dict(state.get(str(did)) or {})
    paused = isinstance(entry.get("paused_limits"), dict)
    try:
        deck = col.decks.get(did, default=False)
        if not deck:
            return False
        if paused:
            # The deck is parked at 0/0 and its real limits are in the
            # snapshot `_set_limits` took. Write there, or resuming would
            # restore the old number over this one.
            entry["paused_limits"]["newLimit"] = limit
            state[str(did)] = entry
            _save_state(state)
        else:
            deck["newLimit"] = limit
            deck.pop("newLimitToday", None)
            col.decks.update_dict(deck)
    except Exception as exc:
        _log(f"set new/day {did}: {exc!r}")
        return False
    # Also remembered as the deck's "original" limit, which is what the
    # legacy preset-restore migration in `_set_limits` puts back.
    state = _state()
    if str(did) in state:
        cur = dict(state.get(str(did)) or {})
        cur["orig_new_per_day"] = limit
        if paused and isinstance(cur.get("paused_limits"), dict):
            cur["paused_limits"]["newLimit"] = limit
        state[str(did)] = cur
        _save_state(state)
    # Subdecks that inherited this deadline get the same limit: each
    # deck's own limit caps what it can contribute when the parent is
    # studied, so raising the parent alone can still fall short.
    if not _cascading:
        _cascading.append(did)
        try:
            for child in descendants(col, did):
                if _inherited_from(_state().get(str(child)), did):
                    set_new_per_day(child, limit)
        finally:
            _cascading.clear()
    return paused or effective_new_per_day(col, did) == limit


# --------------------------------------------------------------------------- #
# Set / clear / refresh
# --------------------------------------------------------------------------- #
def _set_limits(col: Any, conf: Dict[str, Any], entry: Dict[str, Any],
                did: int, paused: bool) -> None:
    """Stop or resume a deck without touching a single card.

    A per-day limit of 0 is how you park a deck in Anki: the cards keep
    their due dates, their intervals and their history, and nothing needs
    unsuspending afterwards — the deck simply stops presenting anything.
    Suspending 400 cards to achieve the same thing would be destructive
    and would lose the distinction between "parked by me" and "parked by a
    deadline".

    The zero goes on the *deck* (`newLimit` / `reviewLimit`, Anki's "This
    deck" overrides), not on the preset. Two things were wrong with the
    preset:

      * it's shared, so parking one deck parked every deck sitting on the
        same preset;
      * resuming meant writing a remembered number back over `perDay`, and
        `_apply` runs on every profile open and every deadline edit. So
        any limit the user set afterwards — by hand in deck options, or
        with this dialog's own "Raise limit to N/day" button — was
        reverted at the next refresh, back to a snapshot taken when the
        deadline was created (or to a hardcoded 20 for entries written
        before that snapshot existed). That is the bug behind "the raise
        button does nothing": it did something, and then this undid it.

    Only a state change is written, so a deck that isn't paused is left
    entirely alone.
    """
    deck = None
    try:
        deck = col.decks.get(did, default=False)
    except Exception:
        deck = None
    if paused:
        if deck is None:
            return
        # Snapshot the deck's own overrides *once*, before zeroing them —
        # keyed on presence, not truthiness, or a second pass over an
        # already-paused deck would snapshot the zeroes and resuming would
        # restore a parked deck.
        if "paused_limits" not in entry:
            entry["paused_limits"] = {
                "newLimit": deck.get("newLimit"),
                "reviewLimit": deck.get("reviewLimit"),
            }
        if deck.get("newLimit") != 0 or deck.get("reviewLimit") != 0:
            deck["newLimit"] = 0
            deck["reviewLimit"] = 0
            deck.pop("newLimitToday", None)
            deck.pop("reviewLimitToday", None)
            col.decks.update_dict(deck)
        return
    # Resuming — or never paused, in which case there is nothing to do.
    saved = entry.pop("paused_limits", None)
    if saved is not None and deck is not None:
        # Back to exactly what the deck had, which is not necessarily
        # "no override": a limit raised to hit the deadline is a choice
        # the user made and pausing shouldn't quietly spend it.
        if not isinstance(saved, dict):
            saved = {}
        for key in ("newLimit", "reviewLimit"):
            val = saved.get(key)
            if val is None:
                deck.pop(key, None)
            else:
                deck[key] = int(val)
        col.decks.update_dict(deck)
    # Migration: an older build paused by zeroing the preset instead, and
    # a preset stuck at 0 shows no cards at all. Put back what we
    # remembered — once; after that the branch can't fire again.
    new = conf.setdefault("new", {})
    rev = conf.setdefault("rev", {})
    if int(new.get("perDay", 0) or 0) == 0 or int(rev.get("perDay", 0) or 0) == 0:
        new["perDay"] = int(entry.get("orig_new_per_day") or 20)
        rev["perDay"] = int(entry.get("orig_rev_per_day") or 200)
        col.decks.update_config(conf)


def _apply(col: Any, did: int, entry: Dict[str, Any]) -> Tuple[Dict[str, Any], bool]:
    """The single place that maps (deadline, mode) onto preset settings.

    Called by set_deadline, set_mode, and both refresh paths
    (`refresh_all`'s once-a-day pass and `check_due_transitions`'s
    event-driven one), so a deck's state can never depend on which of
    those last ran.

    Whether the deck is "still ahead" or "passed" is decided by
    `is_passed` — the real deadline moment, not just the calendar date —
    which is what makes a same-day deadline (e.g. "today at 8am") take
    effect at 8am instead of sitting capped until the calendar date
    changes. The interval cap's *size* while still ahead is still the
    day-based `left`, since a ceiling in days smaller than 1 makes no
    sense before the deadline has actually arrived.

    Returns `(entry, verified)`. `verified` only matters for the "passed"
    branch: it's the result of reading the preset straight back from the
    collection after the restore, so a caller can tell "the code path
    that restores the ceiling ran" from "the ceiling is actually
    restored" — those turned out to be different things once (see
    `_verify_max_interval`), and a caller that only checks the former
    before latching `notified_passed` can strand a deck capped forever
    with its own bookkeeping insisting it was handled. The "still ahead"
    branch always reports True; there's no restore to verify there.
    """
    deadline = parse(str(entry.get("date", "")))
    if deadline is None:
        return entry, True
    time_of_day = _parse_time(entry.get("time"))
    left = days_left(deadline)
    conf = _ensure_own_preset(col, did, entry)
    if not is_passed(deadline, time_of_day):
        # Still ahead: cap intervals at the days remaining, normal limits.
        _set_max_interval(col, conf, max(1, left))
        _set_limits(col, conf, entry, did, paused=False)
        entry.pop("notified_passed", None)
        return entry, True
    # Passed. The cap has done its job and comes off either way — a card
    # answered after the deadline is memory upkeep, not a failure to hit
    # the date.
    target = int(entry.get("orig_max_ivl") or DEFAULT_MAX_IVL)
    _set_max_interval(col, conf, target)
    _set_limits(col, conf, entry, did, paused=(entry_mode(entry) == MODE_PAUSE))
    verified = _verify_max_interval(col, did, max(1, target))
    return entry, verified


def set_deadline(did: int, deadline: datetime.date,
                 mode: Optional[str] = None,
                 time_of_day: Optional[datetime.time] = None) -> None:
    col = getattr(mw, "col", None)
    if col is None:
        return
    state = _state()
    entry = dict(state.get(str(did)) or {})
    entry["date"] = deadline.isoformat()
    if time_of_day is not None:
        # Written as 24h "HH:MM" — see module docstring. Only written when
        # the dialog actually hands one over (i.e. every save made through
        # the new picker); an entry this call never touches keeps whatever
        # "time" key (or absence of one) it already had.
        entry["time"] = f"{time_of_day.hour:02d}:{time_of_day.minute:02d}"
    if mode in (MODE_MAINTAIN, MODE_PAUSE):
        entry["mode"] = mode
    # Deliberately *not* `setdefault("mode", …)`. Materialising a mode the
    # user never picked is what made this feature's default impossible to
    # change later: every entry the dialog saved carried an explicit
    # "maintain" that then looked exactly like a deliberate choice. An
    # entry with no `mode` key resolves through `entry_mode` → config
    # every time it's read, so the deck follows `deadline_passed_mode`
    # until the user actually answers the question in the dialog.
    # Set here directly, so this deck's deadline is its own from now on,
    # whatever an ancestor's cascade wrote before.
    entry.pop("inherited_from", None)
    entry, verified = _apply(col, did, entry)
    if not verified:
        _log(f"set_deadline {did}: preset restore did not verify")
    state[str(did)] = entry
    _cascade(col, did, entry, state)
    _save_state(state)
    _arm_next_deadline_timer()


def set_mode(did: int, mode: str) -> None:
    """Switch a passed deadline between maintenance and paused."""
    col = getattr(mw, "col", None)
    if col is None or mode not in (MODE_MAINTAIN, MODE_PAUSE):
        return
    state = _state()
    entry = dict(state.get(str(did)) or {})
    if not entry.get("date"):
        return
    entry["mode"] = mode
    entry, verified = _apply(col, did, entry)
    if not verified:
        _log(f"set_mode {did}: preset restore did not verify")
    state[str(did)] = entry
    _cascade(col, did, entry, state)
    _save_state(state)
    _arm_next_deadline_timer()


def _drop_entry(did: int) -> None:
    """Forget a deadline without trying to restore a deck that's gone."""
    state = _state()
    state.pop(str(did), None)
    _save_state(state)


def clear_deadline(did: int, notify: bool = False) -> None:
    """Put the deck back exactly where it was: original preset, original
    ceiling. The clone is left in the preset list rather than deleted —
    another deadlined deck may be sharing it, and removing a preset is a
    schema-mod operation that would force a full sync."""
    col = getattr(mw, "col", None)
    if col is not None:
        # Descendants that only had this deck's deadline go back with it.
        for child in descendants(col, did):
            if _inherited_from(_state().get(str(child)), did):
                clear_deadline(child)
    state = _state()
    entry = state.pop(str(did), None)
    _save_state(state)
    if col is None or not isinstance(entry, dict):
        return
    try:
        orig_conf = int(entry.get("orig_conf") or 0)
        cloned = int(entry.get("cloned_conf") or 0)
        if cloned and orig_conf:
            # Put the limits back on the clone first: another deck could
            # be pointed at it later, and leaving a zeroed preset lying
            # around is a trap.
            conf = col.decks.config_dict_for_deck_id(did)
            _set_max_interval(
                col, conf, int(entry.get("orig_max_ivl") or DEFAULT_MAX_IVL)
            )
            _set_limits(col, conf, entry, did, paused=False)
            col.decks.set_config_id_for_deck_dict(col.decks.get(did), orig_conf)
        else:
            conf = col.decks.config_dict_for_deck_id(did)
            _set_max_interval(
                col, conf, int(entry.get("orig_max_ivl") or DEFAULT_MAX_IVL)
            )
            _set_limits(col, conf, entry, did, paused=False)
    except Exception as exc:
        _log(f"clear deadline {did}: {exc!r}")
    _arm_next_deadline_timer()
    if notify:
        _tooltip(f"Deadline for “{_deck_name(did)}” has passed — limits restored")


def _announce_crossing(did: int, entry: Dict[str, Any]) -> None:
    """Tooltip the first time a deck's deadline is found passed. Guarded by
    `notified_passed`, which `_apply`'s "still ahead" branch always clears
    — so this fires exactly once per crossing, however many times a refresh
    path notices it after that."""
    if entry.get("notified_passed"):
        return
    entry["notified_passed"] = True
    if entry_mode(entry) == MODE_PAUSE:
        _tooltip(f"“{_deck_name(did)}” reached its deadline — reviews paused")
    else:
        _tooltip(
            f"“{_deck_name(did)}” reached its deadline — "
            "interval cap lifted, upkeep continues"
        )


def refresh_all(force: bool = False) -> int:
    """Recompute every deadlined deck's ceiling. Cheap, and idempotent.

    Gated to once per Anki-day (`LAST_CHECK_KEY`) because most of what it
    does — shrinking the cap as calendar days tick by, the pacing "needs
    N/day" tooltip — is inherently daily. Passing this gate is *not* what
    makes a deadline's cap come off on time, though: `check_due_transitions`
    (event-driven, below) handles that, since a deadline can be crossed at
    any moment during a day this function has already run for.
    """
    col = getattr(mw, "col", None)
    if col is None or not enabled():
        return 0
    today = _today().isoformat()
    cfg = _config()
    if not force and cfg.get(LAST_CHECK_KEY) == today:
        return 0
    touched = 0
    # Subdecks created since a deadline was set on their ancestor inherit
    # it here (the cascade skips decks that already carry it).
    state = _state()
    for did_s, entry in list(state.items()):
        if not isinstance(entry, dict) or not entry.get("date") or "inherited_from" in entry:
            continue
        try:
            did = int(did_s)
        except Exception:
            continue
        if not col.decks.get(did, default=False):
            continue
        fresh = [c for c in descendants(col, did) if not (state.get(str(c)) or {}).get("date")]
        if fresh and _cascade(col, did, entry, state):
            _save_state(state)
    for did_s in list(_state().keys()):
        try:
            did = int(did_s)
        except Exception:
            continue
        deadline = get(did)
        if deadline is None:
            continue
        # `default=False` matters: `decks.get(did)` hands back the *default
        # deck* for an unknown id rather than None, so without it a state
        # entry for a deleted deck sails past this guard and then trips the
        # assert inside `config_dict_for_deck_id`. Deleted decks are normal
        # — the deck a deadline was set on can be renamed away or removed.
        if not col.decks.get(did, default=False):
            _drop_entry(did)
            continue
        state = _state()
        entry = dict(state.get(did_s) or {})
        passed = is_passed(deadline, get_time(did))
        entry, verified = _apply(col, did, entry)
        if passed:
            if verified:
                # Announce the crossing once, in the language of whichever
                # mode the deck is in. A passed deadline is now a state
                # the deck keeps, not something we quietly delete.
                _announce_crossing(did, entry)
            else:
                # The restore didn't stick — don't latch `notified_passed`,
                # or nothing will ever retry it and the deck stays capped
                # forever while its own bookkeeping insists it's fine.
                entry.pop("notified_passed", None)
                _log(f"refresh_all {did}: preset restore did not verify")
        state[did_s] = entry
        _save_state(state)
        touched += 1
        if passed:
            continue
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
    _arm_next_deadline_timer()
    return touched


def check_due_transitions() -> int:
    """Event-driven counterpart to `refresh_all`: has any deadlined deck's
    *moment* newly passed since we last applied it? Only those decks are
    touched.

    This is what makes the cap actually come off within moments of the
    deadline instead of sitting applied until the next calendar day (which
    is all the once-a-day `refresh_all` gate can promise). It is meant to
    be called from cheap, frequent-but-not-continuous hooks — deck-list
    render, app state changes, the single-shot timer armed by
    `_arm_next_deadline_timer` — without becoming a poll: for any entry
    whose passed/not-passed state hasn't changed since the last time we
    looked (`is_passed(...) == bool(entry.get("notified_passed"))`, the
    same flag `_apply`'s "still ahead" branch clears and the crossing
    announcement sets), this does nothing but a couple of dict lookups —
    no config write, no tooltip. Nothing here runs on a timer loop; the one
    QTimer involved is single-shot and only ever armed for the next actual
    deadline moment.
    """
    col = getattr(mw, "col", None)
    if col is None or not enabled():
        return 0
    touched = 0
    rearm = False
    for did_s in list(_state().keys()):
        try:
            did = int(did_s)
        except Exception:
            continue
        state = _state()
        entry = dict(state.get(did_s) or {})
        deadline = parse(str(entry.get("date", "")))
        if deadline is None:
            continue
        if not col.decks.get(did, default=False):
            _drop_entry(did)
            continue
        time_of_day = _parse_time(entry.get("time"))
        passed = is_passed(deadline, time_of_day)
        already_applied = bool(entry.get("notified_passed"))
        if passed == already_applied:
            continue
        entry, verified = _apply(col, did, entry)
        if passed:
            if verified:
                _announce_crossing(did, entry)
            else:
                # Leave `notified_passed` unset so `already_applied` stays
                # False and the next cheap check (deck-list render, state
                # change, the armed timer) tries the restore again instead
                # of a failed write latching as permanently "handled".
                entry.pop("notified_passed", None)
                _log(f"check_due_transitions {did}: preset restore did not verify")
        state[did_s] = entry
        _save_state(state)
        touched += 1
        rearm = True
    if rearm:
        _arm_next_deadline_timer()
    return touched


# --------------------------------------------------------------------------- #
# One-time legacy-corruption repair (2026-09-15)
# --------------------------------------------------------------------------- #
# Three decks — Microbiology::Exam 1 Content::Week 1/2/3 — already had
# `deck_deadlines` bookkeeping whose `orig_conf` pointed at an already-capped
# "<preset> — deadline" clone from an earlier, buggy deadline round (not the
# deck's true pre-deadline preset), with `orig_max_ivl: 5` and 9999/9999
# daily limits recorded alongside it. `_apply`'s restore path trusts those
# `orig_*` numbers verbatim, so even a fully-working restore would leave
# these three decks capped at a 5-day ceiling with absurd daily limits
# forever — the bookkeeping itself was wrong, not just unapplied.
#
# The true original is recoverable with evidence, not a guess: every clone
# here is named "Default — Week N — deadline" (cloned *from* "Default"), and
# sibling decks under the very same parent (Exam 1 Content, Historical
# Figures) that were *not* corrupted recorded `orig_conf: 1` — the stock
# "Default" preset, currently maxIvl 36500 / new 200/day / rev 2000/day.
#
# This repairs exactly those three decks' already-identified clone presets
# and bookkeeping, once (`_LEGACY_REPAIR_KEY` guards re-running), and
# nothing else — it does not generalize into a heuristic that could "fix" a
# preset it hasn't specifically been told is corrupt. It also removes one
# confirmed-orphaned duplicate "Exam 1 Content — deadline" clone left over
# from an earlier re-clone, but only because no deck references it.
_LEGACY_REPAIR_KEY = "deck_deadlines_legacy_repair_20260915"
_LEGACY_BAD_CONFS: Dict[int, int] = {
    1788448366678: 1788996123168,  # Microbiology::Exam 1 Content::Week 1
    1788448366681: 1788996116337,  # Microbiology::Exam 1 Content::Week 2
    1788988170896: 1788996173074,  # Microbiology::Exam 1 Content::Week 3
}
_LEGACY_ORPHAN_CONF = 1789058167467  # duplicate "Exam 1 Content — deadline"
_LEGACY_TRUE_ORIG_CONF = 1
_LEGACY_TRUE_MAX_IVL = 36500
_LEGACY_TRUE_NEW_PER_DAY = 200
_LEGACY_TRUE_REV_PER_DAY = 2000


def repair_legacy_corruption() -> None:
    """Run the one-time repair described above. Safe to call on every
    startup — the very first thing it does is check whether it already
    ran, and each deck is only touched if it's still sitting on exactly
    the conf id already identified as corrupt (if the user has since
    changed that deck's preset by hand, this leaves it alone rather than
    overwriting a newer, deliberate choice)."""
    col = getattr(mw, "col", None)
    if col is None:
        return
    cfg = _config()
    if cfg.get(_LEGACY_REPAIR_KEY):
        return
    state = _state()
    changed = False
    for did, conf_id in _LEGACY_BAD_CONFS.items():
        try:
            deck = col.decks.get(did, default=False)
            if not deck or int(deck.get("conf", -1)) != conf_id:
                # Deck's gone, or its preset has moved on since diagnosis —
                # don't second-guess whatever it's on now.
                continue
            conf = col.decks.get_config(conf_id)
            conf["id"] = conf_id
            conf.setdefault("rev", {})["maxIvl"] = _LEGACY_TRUE_MAX_IVL
            conf.setdefault("new", {})["perDay"] = _LEGACY_TRUE_NEW_PER_DAY
            conf.setdefault("rev", {})["perDay"] = _LEGACY_TRUE_REV_PER_DAY
            col.decks.update_config(conf)
            verified = _verify_max_interval(col, did, _LEGACY_TRUE_MAX_IVL)
            if not verified:
                _log(f"legacy repair {did}: preset write did not verify")
                continue
            entry = dict(state.get(str(did)) or {})
            entry["orig_conf"] = _LEGACY_TRUE_ORIG_CONF
            entry["orig_max_ivl"] = _LEGACY_TRUE_MAX_IVL
            entry["orig_new_per_day"] = _LEGACY_TRUE_NEW_PER_DAY
            entry["orig_rev_per_day"] = _LEGACY_TRUE_REV_PER_DAY
            # The deck is already privately on this clone and nothing else
            # uses it (checked below) — recording it as `cloned_conf`
            # matches what Exam 1 Content / Historical Figures already do
            # and lets a future deadline round on this deck reuse it
            # instead of re-deriving bad "orig" numbers from it again.
            entry["cloned_conf"] = conf_id
            state[str(did)] = entry
            changed = True
        except Exception as exc:
            _log(f"legacy repair {did}: {exc!r}")
    try:
        if not _decks_using(col, _LEGACY_ORPHAN_CONF, exclude=-1):
            col.decks.remove_config(_LEGACY_ORPHAN_CONF)
    except Exception as exc:
        _log(f"legacy repair orphan clone {_LEGACY_ORPHAN_CONF}: {exc!r}")
    if changed:
        _save_state(state)
    cfg = _config()
    cfg[_LEGACY_REPAIR_KEY] = True
    _write(cfg)


# --------------------------------------------------------------------------- #
# One-time migration to the pause-by-default post-deadline mode (2026-09-15)
# --------------------------------------------------------------------------- #
# Until this change, a deck whose deadline passed carried on presenting cards
# unless the user went and picked "Pause reviews" — the default was maintain,
# and `set_deadline` wrote that default into the entry as though it were a
# choice. The default is now `deadline_passed_mode` ("pause"), but flipping it
# only helps decks that cross their deadline *after* this ships: entries that
# already passed are sitting on the old default and would keep presenting.
#
# Eligibility — deliberately narrow, because the one thing this must never do
# is overrule a real decision:
#
#   * the entry's deadline has **already passed** (`is_passed`, the same
#     moment-in-time check the scheduler paths use). A deadline still ahead is
#     left completely alone: it hasn't chosen anything yet, and when it does
#     cross it will resolve through `entry_mode` → config and pause anyway.
#   * the entry is **not** explicitly paused already (nothing to do), and
#   * the entry is on the old implicit default — either it has no `mode` key
#     at all (three of the six decks here), or it has `mode: "maintain"`
#     *written before this change*. There is no timestamp on an entry, so the
#     migration flag itself is what dates them: this runs exactly once, on the
#     first profile open after the upgrade, before the user can have opened
#     the dialog even once under the new default. Every "maintain" it sees is
#     therefore an old, unasked-for one. A "maintain" the user picks
#     deliberately *after* this ships is written later, when the flag is
#     already set and this function returns immediately — it can never be
#     flipped.
#
# The fix is to *remove* the stale `mode` rather than write "pause" into it:
# that puts the entry back to "no choice recorded", so it follows
# `deadline_passed_mode` live — including back to maintain if the user sets
# the config key that way — instead of freezing today's default into state.
_PASSED_MODE_MIGRATION_KEY = "deck_deadlines_passed_mode_default_20260915"


def _is_implicit_maintain(entry: Any) -> bool:
    """Is this entry on the pre-change implicit "maintain" default?

    Pure predicate over one state dict — no collection, no clock — so the
    eligibility rule above is testable on its own. The "already passed"
    half of eligibility is deliberately *not* folded in here: that needs a
    real deadline moment, and keeping it separate means the two halves can
    be exercised independently.
    """
    if not isinstance(entry, dict):
        return False
    mode = entry.get("mode")
    if mode == MODE_PAUSE:
        return False          # explicit pause — already what we want
    # Missing, unrecognised, or the materialised "maintain" the old
    # `set_deadline` wrote for every entry whether or not it was asked for.
    return True


def migrate_passed_mode_default() -> int:
    """Apply the new pause default to deadlines that already passed.

    Runs once (`_PASSED_MODE_MIGRATION_KEY`). Returns how many decks it
    actually paused, for the caller's one-line summary.
    """
    col = getattr(mw, "col", None)
    if col is None or not enabled():
        # Not a no-op-and-latch: without a collection there is nothing to
        # apply, and with the feature off it would be applying scheduling
        # changes the user turned off. Leave the flag unset so this runs
        # properly next time instead of being permanently skipped.
        return 0
    cfg = _config()
    if cfg.get(_PASSED_MODE_MIGRATION_KEY):
        return 0
    state = _state()
    paused: List[str] = []
    for did_s in list(state.keys()):
        try:
            did = int(did_s)
        except Exception:
            continue
        entry = dict(state.get(did_s) or {})
        deadline = parse(str(entry.get("date", "")))
        if deadline is None:
            continue
        if not is_passed(deadline, _parse_time(entry.get("time"))):
            continue
        if not _is_implicit_maintain(entry):
            continue
        if not col.decks.get(did, default=False):
            continue
        entry.pop("mode", None)
        if entry_mode(entry) != MODE_PAUSE:
            # The config key says "maintain" — the user (or a future
            # default) wants the old behavior, so there is nothing to
            # apply. The stale key is still dropped so the entry follows
            # the config from here.
            state[did_s] = entry
            continue
        try:
            entry, verified = _apply(col, did, entry)
            if not verified:
                _log(f"passed-mode migration {did}: preset restore did not verify")
        except Exception as exc:
            _log(f"passed-mode migration {did}: {exc!r}")
            continue
        state[did_s] = entry
        paused.append(_deck_name(did))
    _save_state(state)
    cfg = _config()
    cfg[_PASSED_MODE_MIGRATION_KEY] = True
    _write(cfg)
    if paused:
        count = len(paused)
        _tooltip(
            f"{count} deck{'s' if count != 1 else ''} past their deadline "
            "paused — reopen “Memorize by…” to keep reviewing instead"
        )
    return len(paused)


# --------------------------------------------------------------------------- #
# One-time repair of "— deadline" clones parked at 0/day (2026-09-15)
# --------------------------------------------------------------------------- #
# An older build of this module implemented "Pause reviews" by zeroing the
# *preset's* `new/perDay` and `rev/perDay` (today it zeroes the deck's own
# limits instead — see `_set_limits`). `_set_limits` still carries a
# resume-side migration for that, but it only fires for a deck that still has
# a `deck_deadlines` entry to resume from. A deck whose entry was dropped
# while it was paused that way is stranded: it sits on a "<preset> — deadline"
# clone whose per-day limits are 0, presents nothing at all, and nothing in
# the codebase will ever look at it again.
#
# That is exactly the state of "Default — Amino Acids — deadline" in this
# collection: 0 new/day, 0 reviews/day, one deck on it, no state entry.
#
# Repaired conservatively and once: only a preset whose name ends in
# CLONE_SUFFIX (i.e. one this module made), only when a per-day limit is
# actually 0, and only with numbers there is evidence for — the remembered
# `orig_*` from a state entry that still points at the clone, else the
# current values of the preset the clone was named after ("Default — Amino
# Acids — deadline" → "Default"). With no evidence, it is left alone and
# logged rather than guessed at.
_ZEROED_CLONE_REPAIR_KEY = "deck_deadlines_zeroed_clone_repair_20260915"


def _clone_source_name(clone_name: str) -> Optional[str]:
    """"Default — Amino Acids — deadline" → "Default".

    Mirrors the name `_ensure_own_preset` builds ("<preset> — <leaf> —
    deadline"). Names are never trusted to decide *which* clone belongs to
    a deck (that's `cloned_conf`, by id); this only reads a name to find a
    plausible source for limits the clone lost, and a miss just means the
    preset is skipped.
    """
    if not isinstance(clone_name, str) or not clone_name.endswith(CLONE_SUFFIX):
        return None
    head = clone_name[: -len(CLONE_SUFFIX)]
    sep = " — "
    if sep not in head:
        return None
    return head.rsplit(sep, 1)[0] or None


def repair_zeroed_clone_presets() -> int:
    """Un-park "— deadline" clones left at 0/day by the old pause. Once."""
    col = getattr(mw, "col", None)
    if col is None:
        return 0
    cfg = _config()
    if cfg.get(_ZEROED_CLONE_REPAIR_KEY):
        return 0
    fixed = 0
    try:
        confs = list(col.decks.all_config())
    except Exception as exc:
        _log(f"zeroed clone repair: {exc!r}")
        return 0
    by_name = {str(c.get("name", "")): c for c in confs}
    state = _state()
    for conf in confs:
        name = str(conf.get("name", ""))
        if not name.endswith(CLONE_SUFFIX):
            continue
        new = conf.get("new", {}) or {}
        rev = conf.get("rev", {}) or {}
        if int(new.get("perDay", 0) or 0) and int(rev.get("perDay", 0) or 0):
            continue
        conf_id = int(conf.get("id", 0) or 0)
        new_per_day = rev_per_day = None
        for entry in state.values():
            if not isinstance(entry, dict):
                continue
            if int(entry.get("cloned_conf") or 0) != conf_id:
                continue
            new_per_day = entry.get("orig_new_per_day")
            rev_per_day = entry.get("orig_rev_per_day")
            break
        if new_per_day is None or rev_per_day is None:
            source = by_name.get(_clone_source_name(name) or "")
            if not source:
                _log(f"zeroed clone repair: no evidence for {name!r}, left alone")
                continue
            new_per_day = (source.get("new", {}) or {}).get("perDay")
            rev_per_day = (source.get("rev", {}) or {}).get("perDay")
        try:
            new_per_day = int(new_per_day or 0)
            rev_per_day = int(rev_per_day or 0)
        except Exception:
            continue
        if new_per_day <= 0 or rev_per_day <= 0:
            _log(f"zeroed clone repair: source for {name!r} is itself 0, left alone")
            continue
        try:
            conf.setdefault("new", {})["perDay"] = new_per_day
            conf.setdefault("rev", {})["perDay"] = rev_per_day
            col.decks.update_config(conf)
            fixed += 1
            _log(f"zeroed clone repair: {name!r} → {new_per_day}/{rev_per_day} per day")
        except Exception as exc:
            _log(f"zeroed clone repair {name!r}: {exc!r}")
    cfg = _config()
    cfg[_ZEROED_CLONE_REPAIR_KEY] = True
    _write(cfg)
    return fixed


# A single pending single-shot timer, re-armed for the soonest upcoming
# deadline moment every time a deadline is set/cleared or a check runs.
# Never a recurring/polling timer, and never more than one pending.
_next_deadline_timer: Any = None


def _arm_next_deadline_timer() -> None:
    global _next_deadline_timer
    try:
        from aqt.qt import QTimer
    except Exception:
        return
    if mw is None:
        return
    now = datetime.datetime.now()
    soonest: Optional[datetime.datetime] = None
    for entry in _state().values():
        if not isinstance(entry, dict):
            continue
        deadline = parse(str(entry.get("date", "")))
        if deadline is None:
            continue
        moment = _deadline_moment(deadline, _parse_time(entry.get("time")))
        if moment <= now:
            continue
        if soonest is None or moment < soonest:
            soonest = moment
    if _next_deadline_timer is not None:
        try:
            _next_deadline_timer.stop()
        except Exception:
            pass
        _next_deadline_timer = None
    if soonest is None:
        return
    # A little slack so we fire just after, never just before, the moment;
    # capped at a day out (and re-armed every time this runs, including
    # from that fired timer's own callback) rather than trusting a huge
    # single delay — QTimer's interval is a 32-bit ms count and multi-day
    # deadlines are common here.
    delay_ms = int((soonest - now).total_seconds() * 1000) + 1000
    delay_ms = max(1000, min(delay_ms, 24 * 60 * 60 * 1000))
    try:
        timer = QTimer(mw)
        timer.setSingleShot(True)
        timer.timeout.connect(lambda: check_due_transitions())
        timer.start(delay_ms)
        _next_deadline_timer = timer
    except Exception:
        _next_deadline_timer = None


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
        QTime,
        QTimeEdit,
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

    col = getattr(mw, "col", None)
    kids = len(descendants(col, did)) if col is not None else 0
    origin = (_state().get(str(did)) or {}).get("inherited_from")
    if origin:
        sub_text = (f"Inherited from “{_deck_name(int(origin))}”. Setting a date here "
                    "gives this deck its own deadline.")
    elif kids:
        sub_text = (f"Every card in this deck and its {kids} subdeck"
                    f"{'s' if kids != 1 else ''} should be known by:")
    else:
        sub_text = "Every card in this deck should be known by:"
    sub = QLabel(sub_text)
    sub.setProperty("role", "sub")
    sub.setWordWrap(True)
    root.addWidget(sub)

    cal = QCalendarWidget()
    cal.setGridVisible(False)
    cal.setVerticalHeaderFormat(
        QCalendarWidget.VerticalHeaderFormat.NoVerticalHeader
    )
    # No minimum date. A deadline in the past is a legitimate thing to
    # record — "this set needed to be known by Tuesday, and it was" — and
    # refusing it was the bug this revision fixes.
    current = get(did)
    if current:
        cal.setSelectedDate(QDate(current.year, current.month, current.day))
    root.addWidget(cal)

    # Time of day, right under the calendar. Defaults to whatever this
    # deadline already means: the stored time if there is one, else the
    # collection's rollover hour on the hour — so opening and re-saving an
    # existing entry (or a fresh one, which has no meaning yet beyond "the
    # rollover") shows the same moment it already stood for, and Save
    # doesn't silently move it unless the user actually turns the dial.
    stored_time = get_time(did)
    default_time = stored_time or datetime.time(hour=_rollover_hour())

    time_row = QHBoxLayout()
    time_row.setSpacing(8)
    time_label = QLabel("at")
    time_label.setProperty("role", "sub")
    time_row.addWidget(time_label)
    time_edit = QTimeEdit()
    time_edit.setObjectName("ba-deadline-time")
    time_edit.setDisplayFormat("h:mm AP")
    time_edit.setTime(QTime(default_time.hour, default_time.minute))
    time_edit.setAccelerated(True)
    time_row.addWidget(time_edit)
    time_row.addStretch(1)
    root.addLayout(time_row)

    note = QLabel("")
    note.setProperty("role", "note")
    note.setWordWrap(True)
    root.addWidget(note)

    # Shown only for a date that has already passed: what should the deck
    # do now that it is meant to be known?
    from aqt.qt import QButtonGroup, QRadioButton, QWidget

    past_box = QWidget()
    past_col = QVBoxLayout(past_box)
    past_col.setContentsMargins(0, 2, 0, 0)
    past_col.setSpacing(4)
    past_head = QLabel("That date has passed. From here:")
    past_head.setProperty("role", "sub")
    past_col.addWidget(past_head)
    rb_maintain = QRadioButton("Maintain long-term — keep reviewing to hold it")
    rb_pause = QRadioButton("Pause reviews — stop showing this deck for now")
    group = QButtonGroup(past_box)
    group.addButton(rb_maintain)
    group.addButton(rb_pause)
    past_col.addWidget(rb_maintain)
    past_col.addWidget(rb_pause)
    past_why = QLabel(
        "Cards still coming due after the deadline are memory upkeep, not a "
        "missed target — that's how spaced repetition holds something you "
        "already know. Pausing is reversible and suspends nothing."
    )
    past_why.setProperty("role", "note")
    past_why.setWordWrap(True)
    past_col.addWidget(past_why)
    root.addWidget(past_box)
    # Pre-selects the deck's own recorded choice when it has one, and the
    # `deadline_passed_mode` default when it doesn't (`get_mode` resolves
    # both) — so a deck that has never answered this question arrives with
    # Pause ticked, matching what it would do if the user just hit Save.
    (rb_pause if get_mode(did) == MODE_PAUSE else rb_maintain).setChecked(True)
    past_box.hide()

    raise_btn = QPushButton("Raise limit")
    raise_btn.setProperty("role", "quiet")
    raise_btn.hide()
    pace_state: Dict[str, int] = {}

    def chosen_date() -> datetime.date:
        sel = cal.selectedDate()
        return datetime.date(sel.year(), sel.month(), sel.day())

    def chosen_time() -> datetime.time:
        t = time_edit.time()
        return datetime.time(hour=t.hour(), minute=t.minute())

    def refresh_note() -> None:
        chosen = chosen_date()
        delta = (chosen - _today()).days
        col = mw.col
        if delta < 0:
            past_box.show()
            raise_btn.hide()
            ago = -delta
            note.setText(
                f"{ago} day{'s' if ago != 1 else ''} ago. No interval cap is "
                "applied for a date that has already passed."
            )
            save.setText("Save")
            return
        past_box.hide()
        left = max(1, delta)
        try:
            from anki.collection import SearchNode

            search = col.build_search_string(SearchNode(deck=name))
            remaining = len(col.find_cards(f"{search} is:new -is:suspended"))
            per_day = effective_new_per_day(col, did)
        except Exception:
            note.setText("")
            return
        needed = int(math.ceil(remaining / left)) if remaining else 0
        day_s = "day" if left == 1 else "days"
        lines = [f"Intervals will be capped at {left} {day_s}."]
        if pace_state.get("raised"):
            lines.append(f"Limit raised to {pace_state['raised']}/day ✓")
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
        save.setText("Set deadline")

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
        wanted = pace_state.get("needed")
        if not wanted:
            return
        if set_new_per_day(did, wanted):
            # Say so in the dialog. Previously the only evidence a raise
            # had happened was the button disappearing, which reads much
            # more like a control that gave up than one that worked.
            pace_state["raised"] = wanted
            _tooltip(f"“{name}” — new cards raised to {wanted}/day")
            _refresh_deck_browser()
        else:
            _tooltip("Couldn't change that deck's daily limit")
        refresh_note()

    def on_save() -> None:
        chosen = chosen_date()
        time_val = chosen_time()
        past = (chosen - _today()).days < 0
        mode = (MODE_PAUSE if rb_pause.isChecked() else MODE_MAINTAIN) if past else None
        set_deadline(did, chosen, mode=mode, time_of_day=time_val)
        dlg.accept()
        stamp = _stamp(chosen, time_val)
        if past:
            if mode == MODE_PAUSE:
                _tooltip(f"“{name}” memorized by {stamp} — reviews paused")
            else:
                _tooltip(f"“{name}” memorized by {stamp} — upkeep continues")
        else:
            _tooltip(f"“{name}” must be known by {stamp}")
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

    # Wired after the buttons exist — refresh_note retitles Save and can
    # only run once there is a Save to retitle.
    cal.selectionChanged.connect(refresh_note)
    rb_maintain.toggled.connect(lambda _on: None)
    refresh_note()

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
    from . import settings as _settings

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
QDialog#ba-deadline QTimeEdit {{
    background: {p['field_bg']};
    color: {p['ink']};
    border: 1px solid {p['line2']};
    border-radius: 8px;
    padding: 6px 26px 6px 10px;
    font-family: {_addcard.SANS};
    font-size: 11pt;
    selection-background-color: {accent};
    selection-color: {p['paper']};
}}
QDialog#ba-deadline QTimeEdit:focus {{ border-color: {accent}; }}
QDialog#ba-deadline QTimeEdit::up-button {{
    subcontrol-origin: border;
    subcontrol-position: top right;
    width: 18px;
    border: 0;
    background: transparent;
}}
QDialog#ba-deadline QTimeEdit::down-button {{
    subcontrol-origin: border;
    subcontrol-position: bottom right;
    width: 18px;
    border: 0;
    background: transparent;
}}
QDialog#ba-deadline QTimeEdit::up-arrow {{
    image: url({_settings._icon_url("chevron-up.svg")});
    width: 10px; height: 6px;
}}
QDialog#ba-deadline QTimeEdit::down-arrow {{
    image: url({_settings._icon_url("chevron-down.svg")});
    width: 10px; height: 6px;
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
