# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""Scheduled, automatic FSRS parameter optimization.

Periodically (and on request, via Tools > Optimize FSRS Now), this module
re-runs FSRS parameter optimization for the user's current deck preset,
mirroring what "Optimize All Presets" does on the deck options screen.

The gate-checking logic that decides whether an optimization run should
happen lives in the pure function `_should_run()` below. It has no
dependency on Qt or the Anki collection backend, so it can be exercised on
its own, without a built `aqt`/`anki` environment, e.g. via:

    python3 -m doctest qt/aqt/auto_optimize.py -v

A small pytest-style test also lives at qt/tests/test_auto_optimize.py,
for use with the project's normal aqt-enabled test environment.

Everything else in this file (`setup`, `maybe_auto_optimize`, `_run_optimize`,
...) is the Qt/collection "glue" that gathers live inputs from `AnkiQt` and
acts on the decision `_should_run()` makes; those functions import aqt/anki
lazily so that importing this module (as opposed to calling into it) never
requires Qt or a running collection.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import aqt.main

# Collection config key that stores all settings for this module.
_CONFIG_KEY = "autoOptimize"

_DEFAULT_CONFIG: dict = {
    "enabled": True,
    "intervalDays": 7,
    "minReviews": 400,
    "autoApplyRetention": False,
    "maxRetentionStep": 0.03,
    "presets": {},
}


def _should_run(
    config: dict,
    state: str,
    busy: bool,
    fsrs_enabled: bool,
    days_since: int,
    review_count: int,
    force: bool,
) -> tuple[bool, str]:
    """Decide whether an automatic optimization run should happen now.

    Returns (should_run, skip_reason); skip_reason is "" when should_run is
    True. `force` (the "Optimize FSRS Now" menu action) bypasses the
    enabled/interval/review-count gates, but never the state/busy/FSRS-enabled
    gates -- those reflect things that would make running unsafe or
    meaningless, not just "it's not time yet".

    >>> cfg = dict(_DEFAULT_CONFIG)

    Happy path:

    >>> _should_run(cfg, "deckBrowser", False, True, 7, 400, False)
    (True, '')

    Disabled in config:

    >>> _should_run({**cfg, "enabled": False}, "deckBrowser", False, True, 7, 400, False)
    (False, 'disabled')

    Wrong screen / UI busy:

    >>> _should_run(cfg, "review", False, True, 7, 400, False)
    (False, 'not-deck-browser')
    >>> _should_run(cfg, "deckBrowser", True, True, 7, 400, False)
    (False, 'busy')

    FSRS not enabled on this preset:

    >>> _should_run(cfg, "deckBrowser", False, False, 7, 400, False)
    (False, 'fsrs-disabled')

    Interval / review-count thresholds not yet met:

    >>> _should_run(cfg, "deckBrowser", False, True, 3, 400, False)
    (False, 'interval-not-elapsed')
    >>> _should_run(cfg, "deckBrowser", False, True, 7, 10, False)
    (False, 'insufficient-reviews')

    `force=True` bypasses enabled/interval/review-count...

    >>> _should_run({**cfg, "enabled": False}, "deckBrowser", False, True, 0, 0, True)
    (True, '')

    ...but never state/busy/fsrs-enabled:

    >>> _should_run(cfg, "review", False, True, 0, 0, True)
    (False, 'not-deck-browser')
    >>> _should_run(cfg, "deckBrowser", True, True, 0, 0, True)
    (False, 'busy')
    >>> _should_run(cfg, "deckBrowser", False, False, 0, 0, True)
    (False, 'fsrs-disabled')
    """
    if not force and not config.get("enabled", True):
        return False, "disabled"
    if state != "deckBrowser":
        return False, "not-deck-browser"
    if busy:
        return False, "busy"
    if not fsrs_enabled:
        return False, "fsrs-disabled"
    if not force and days_since < config.get("intervalDays", 7):
        return False, "interval-not-elapsed"
    if not force and review_count < config.get("minReviews", 400):
        return False, "insufficient-reviews"
    return True, ""


def setup(mw: aqt.main.AnkiQt) -> None:
    """Wire up automatic FSRS optimization. Called once from AnkiQt.setupUI()."""
    from aqt import gui_hooks
    from aqt.qt import qconnect

    # Delay the first check by a minute after each profile open, so that
    # auto-sync-on-open has a chance to finish first.
    gui_hooks.profile_did_open.append(
        lambda: mw.progress.timer(
            60_000, lambda: maybe_auto_optimize(mw), repeat=False, parent=mw
        )
    )
    gui_hooks.day_did_change.append(lambda: maybe_auto_optimize(mw))

    action = mw.form.menuTools.addAction("Optimize FSRS Now")
    qconnect(action.triggered, lambda: maybe_auto_optimize(mw, force=True))


def _load_config(mw: aqt.main.AnkiQt) -> dict:
    assert mw.col is not None
    config = dict(_DEFAULT_CONFIG)
    config.update(mw.col.get_config(_CONFIG_KEY, {}))
    return config


def maybe_auto_optimize(mw: aqt.main.AnkiQt, force: bool = False) -> None:
    """Run FSRS optimization for the current deck's preset, if warranted.

    Called speculatively from the post-profile-open timer, the day-change
    hook, and the forced "Optimize FSRS Now" Tools-menu action -- it is a
    no-op unless every gate in `_should_run()` passes.
    """
    if mw.col is None:
        return

    config = _load_config(mw)
    info = mw.col.decks.get_deck_configs_for_update(mw.col.decks.get_current_id())
    review_count = mw.col.db.scalar("select count() from revlog") or 0

    should_run, reason = _should_run(
        config=config,
        state=mw.state,
        busy=bool(mw.progress.busy()),
        fsrs_enabled=info.fsrs,
        days_since=info.days_since_last_fsrs_optimize,
        review_count=review_count,
        force=force,
    )
    if not should_run:
        if reason == "insufficient-reviews":
            _maybe_show_notice(mw, config)
        return

    _run_optimize(mw)


def _maybe_show_notice(mw: aqt.main.AnkiQt, config: dict) -> None:
    "Let the user know once per day (at most) why auto-optimize was skipped."
    from aqt.utils import tooltip

    today = int(time.time()) // 86400
    if config.get("lastNoticeDay") == today:
        return
    config["lastNoticeDay"] = today
    assert mw.col is not None
    mw.col.set_config(_CONFIG_KEY, config)
    tooltip("Not enough reviews yet for automatic FSRS optimization.", parent=mw)


def _run_optimize(mw: aqt.main.AnkiQt) -> None:
    """Recompute FSRS parameters for the current deck's preset.

    Builds the same UpdateDeckConfigsRequest the deck options screen sends
    when the user clicks "Optimize All Presets" there, and runs it as a
    background CollectionOp -- see rslib/src/deckconfig/update.rs for the
    backend logic this mirrors.
    """
    from anki.decks import UpdateDeckConfigs, UpdateDeckConfigsMode
    from aqt.operations.deck import update_deck_configs as update_deck_configs_op
    from aqt.utils import tooltip

    assert mw.col is not None
    deck_id = mw.col.decks.get_current_id()
    info = mw.col.decks.get_deck_configs_for_update(deck_id)

    current_config = next(
        (
            c.config
            for c in info.all_config
            if c.config.id == info.current_deck.config_id
        ),
        None,
    )
    if current_config is None:
        # deck's preset vanished from under us; nothing sane to do
        return

    request = UpdateDeckConfigs(
        target_deck_id=deck_id,
        # compute_all_params() (triggered by the COMPUTE_ALL_PARAMS mode
        # below) back-fills every other preset itself; the *last* entry in
        # `configs` is the one assigned to `target_deck_id`, so it must be
        # the deck's own, currently-selected preset.
        configs=[current_config],
        mode=UpdateDeckConfigsMode.UPDATE_DECK_CONFIGS_MODE_COMPUTE_ALL_PARAMS,
        # passed through unchanged from `info`, as the deck options screen does
        card_state_customizer=info.card_state_customizer,
        new_cards_ignore_review_limit=info.new_cards_ignore_review_limit,
        apply_all_parent_limits=info.apply_all_parent_limits,
        fsrs=True,
        fsrs_health_check=info.fsrs_health_check,
        # written unconditionally by the backend -- omitting these would wipe
        # the deck's own new/review limits and desired-retention override
        limits=info.current_deck.limits,
        fsrs_reschedule=False,
    )

    def on_success(_: object) -> None:
        tooltip("FSRS parameters optimized", parent=mw)
        assert mw.col is not None
        config = _load_config(mw)
        presets = config.setdefault("presets", {})
        presets[str(current_config.id)] = {"lastOptimize": int(time.time())}
        mw.col.set_config(_CONFIG_KEY, config)
        _after_optimize(mw, info)

    update_deck_configs_op(parent=mw, input=request).success(on_success).run_in_background()


def _after_optimize(mw: aqt.main.AnkiQt, info: object) -> None:
    """Phase 4 hook: CMRR chaining"""
