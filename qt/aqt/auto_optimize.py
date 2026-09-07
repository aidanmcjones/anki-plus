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


def _propose_retention(old_dr: float, cmrr: float, max_step: float) -> float | None:
    """Damp a freshly computed CMRR into a bounded desired-retention change.

    `cmrr` is the backend's "compute minimum recommended retention" value for
    one preset (already clamped to [0.7, 0.95] server-side -- see
    rslib/src/scheduler/fsrs/retention.rs). Rather than jumping straight to
    it, the proposed value is only allowed to move at most `max_step` away
    from `old_dr` per run, and is rounded to 2dp to match the precision the
    deck options screen displays/accepts. Returns None if the (rounded)
    change would be smaller than 0.01, i.e. not worth bothering the user (or
    rewriting config) over.

    >>> round(_propose_retention(0.80, 0.90, 0.02), 2)
    0.82
    >>> round(_propose_retention(0.90, 0.70, 0.02), 2)
    0.88

    Below the 0.01 change threshold -- nothing proposed:

    >>> _propose_retention(0.85, 0.85, 0.03) is None
    True

    Already at a clamp boundary, CMRR agrees -- nothing proposed:

    >>> _propose_retention(0.70, 0.70, 0.03) is None
    True
    >>> _propose_retention(0.95, 0.95, 0.03) is None
    True

    Ordinary moves, clamped to +/- max_step:

    >>> round(_propose_retention(0.90, 0.80, 0.03), 2)
    0.87
    >>> round(_propose_retention(0.90, 0.99, 0.03), 2)
    0.93
    """
    new_dr = max(old_dr - max_step, min(old_dr + max_step, cmrr))
    new_dr = round(new_dr, 2)
    if abs(new_dr - old_dr) < 0.01:
        return None
    return new_dr


def _current_fsrs_params(config: object) -> list[float]:
    """Mirror DeckConfig::fsrs_params() (rslib/src/deckconfig/mod.rs):
    prefer fsrs_params_6, falling back to _5, then _4. Returns [] if none of
    the three has ever been populated (i.e. FSRS optimization has never been
    run for this preset)."""
    if config.fsrs_params_6:
        return list(config.fsrs_params_6)
    if config.fsrs_params_5:
        return list(config.fsrs_params_5)
    return list(config.fsrs_params_4)


def _escape_preset_name_for_search(name: str) -> str:
    "Mirror ts/routes/deck-options/lib.ts:getCurrentNameForSearch()."
    return name.replace("\\", "\\\\").replace('"', '\\"')


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

    update_deck_configs_op(parent=mw, input=request).success(
        on_success
    ).run_in_background()


def _eligible_presets(info: object) -> list:
    """Presets worth computing a CMRR for: at least one deck uses them, and
    they have FSRS params to simulate with (mirrors the rust-side
    `fsrs_params()` fallback -- a preset that's never been optimized has
    nothing to simulate)."""
    return [
        c.config
        for c in info.all_config  # DeckConfigsForUpdate.ConfigWithExtra
        if c.use_count > 0 and _current_fsrs_params(c.config.config)
    ]


def _build_simulate_request(config: object, new_cards_ignore_review_limit: bool):
    """Build a SimulateFsrsReviewRequest for one preset, field-for-field the
    same request the deck options screen builds -- see
    ts/routes/deck-options/FsrsOptions.svelte (base request) and
    SimulatorModal.svelte's updateRequest() (daysToSimulate/deckSize/
    suspendAfterLapseCount/easyDaysPercentages overrides)."""
    from anki import deck_config_pb2, scheduler_pb2

    cfg = config.config  # DeckConfig.Config
    request = scheduler_pb2.SimulateFsrsReviewRequest(
        params=_current_fsrs_params(cfg),
        desired_retention=cfg.desired_retention,
        deck_size=0,
        days_to_simulate=365,
        new_limit=cfg.new_per_day,
        review_limit=cfg.reviews_per_day,
        max_interval=cfg.maximum_review_interval,
        search=f'preset:"{_escape_preset_name_for_search(config.name)}" -is:suspended',
        new_cards_ignore_review_limit=new_cards_ignore_review_limit,
        easy_days_percentages=list(cfg.easy_days_percentages),
        review_order=cfg.review_order,
        historical_retention=cfg.historical_retention,
        learning_step_count=len(cfg.learn_steps),
        relearning_step_count=len(cfg.relearn_steps),
    )
    # suspend_after_lapse_count is proto3-optional (a None/unset value is
    # meaningfully different from 0); only set it when leeches suspend.
    if cfg.leech_action == deck_config_pb2.DeckConfig.Config.LeechAction.LEECH_ACTION_SUSPEND:
        request.suspend_after_lapse_count = cfg.leech_threshold
    return request


def _compute_cmrr_proposals(col, presets: list, new_cards_ignore_review_limit: bool):
    """Runs on a background thread (see `_after_optimize` for the QueryOp
    that calls this). `compute_optimal_retention` is a pure, read-only
    simulation -- rslib/src/scheduler/fsrs/retention.rs neither transacts
    nor writes an undo entry, it just crunches revlog data -- but simulating
    365 days for every eligible preset is CPU-bound and can take a
    noticeable amount of time, so it still must not run on the main thread."""
    results = []
    for preset in presets:
        request = _build_simulate_request(preset, new_cards_ignore_review_limit)
        cmrr = col._backend.compute_optimal_retention(request)
        results.append((preset, cmrr))
    return results


def _after_optimize(mw: aqt.main.AnkiQt, info: object) -> None:
    """Phase 4: after optimizing FSRS params, compute a minimum recommended
    retention (CMRR) for every preset with params to simulate, and propose
    (or, if configured, silently apply) a damped desired-retention change.

    Runs the simulations via QueryOp rather than CollectionOp: the compute
    step itself never mutates the collection or needs undo support (see
    `_compute_cmrr_proposals`'s docstring), so QueryOp -- the lighter-weight
    of the two, without CollectionOp's undo bookkeeping and
    operation_did_execute/state_did_reset hooks -- is the right tool. The
    later apply step (`_apply_retention_changes`) *does* mutate deck configs,
    so that one goes through the existing CollectionOp-backed
    `update_deck_configs` op, same as `_run_optimize`.
    """
    from aqt.operations import QueryOp

    assert mw.col is not None
    deck_id = mw.col.decks.get_current_id()
    # info passed in is from before this optimize run; re-fetch so the
    # freshly-optimized params are what gets simulated.
    fresh_info = mw.col.decks.get_deck_configs_for_update(deck_id)
    presets = _eligible_presets(fresh_info)
    if not presets:
        return

    QueryOp(
        parent=mw,
        op=lambda col: _compute_cmrr_proposals(
            col, presets, fresh_info.new_cards_ignore_review_limit
        ),
        success=lambda results: _on_cmrr_computed(mw, fresh_info, results),
    ).with_progress("Computing retention proposals...").run_in_background()


def _on_cmrr_computed(mw: aqt.main.AnkiQt, info: object, results: list) -> None:
    "Runs on the main thread once every preset's CMRR has been computed."
    assert mw.col is not None
    config = _load_config(mw)
    max_step = config.get("maxRetentionStep", _DEFAULT_CONFIG["maxRetentionStep"])
    now = int(time.time())
    presets_bookkeeping = config.setdefault("presets", {})

    proposals = []  # (preset, old_dr, new_dr, cmrr)
    for preset, cmrr in results:
        old_dr = preset.config.desired_retention
        new_dr = _propose_retention(old_dr, cmrr, max_step)
        if new_dr is None:
            # nothing worth changing -- record the CMRR anyway so the user
            # (or a future run) can see it was checked.
            entry = presets_bookkeeping.setdefault(str(preset.id), {})
            entry.update(
                lastCmrr=now, cmrrValue=cmrr, prevRetention=old_dr, lastRetention=old_dr
            )
        else:
            proposals.append((preset, old_dr, new_dr, cmrr))
    mw.col.set_config(_CONFIG_KEY, config)

    if not proposals:
        return

    if config.get("autoApplyRetention", False):
        _apply_retention_changes(mw, info, proposals)
    else:
        mw.taskman.run_on_main(lambda: _show_retention_proposal_dialog(mw, info, proposals))


def _show_retention_proposal_dialog(mw: aqt.main.AnkiQt, info: object, proposals: list) -> None:
    "Non-modal Apply/Skip prompt listing each preset's proposed change."
    from aqt.qt import QMessageBox, Qt, qconnect

    lines = [
        f"{preset.name}: {old_dr:.2f} -> {new_dr:.2f}"
        for preset, old_dr, new_dr, _ in proposals
    ]
    box = QMessageBox(mw)
    box.setIcon(QMessageBox.Icon.Question)
    box.setWindowTitle("FSRS Retention Update")
    box.setText(
        "Based on your review history, the following desired retention "
        "changes are suggested:\n\n" + "\n".join(lines)
    )
    apply_button = box.addButton("Apply", QMessageBox.ButtonRole.AcceptRole)
    box.addButton("Skip", QMessageBox.ButtonRole.RejectRole)
    box.setDefaultButton(apply_button)
    box.setWindowModality(Qt.WindowModality.NonModal)
    box.setModal(False)

    def on_finished(_: int) -> None:
        if box.clickedButton() == apply_button:
            _apply_retention_changes(mw, info, proposals)
        else:
            _record_declined_proposals(mw, proposals)

    qconnect(box.finished, on_finished)
    box.show()


def _record_declined_proposals(mw: aqt.main.AnkiQt, proposals: list) -> None:
    assert mw.col is not None
    config = _load_config(mw)
    presets_bookkeeping = config.setdefault("presets", {})
    now = int(time.time())
    for preset, old_dr, _new_dr, cmrr in proposals:
        entry = presets_bookkeeping.setdefault(str(preset.id), {})
        entry.update(
            lastCmrr=now, cmrrValue=cmrr, prevRetention=old_dr, lastRetention=old_dr
        )
    mw.col.set_config(_CONFIG_KEY, config)


def _apply_retention_changes(mw: aqt.main.AnkiQt, info: object, proposals: list) -> None:
    """Write the (possibly damped) desired_retention proposals back via a
    second UpdateDeckConfigsRequest -- mode NORMAL this time, since we're
    supplying explicit values rather than asking the backend to recompute
    params. As in `_run_optimize`, the *last* entry in `configs` is the one
    the backend assigns to `target_deck_id`, so the current deck's own
    preset (changed or not) must be last."""
    from anki.decks import UpdateDeckConfigs, UpdateDeckConfigsMode
    from aqt.operations.deck import update_deck_configs as update_deck_configs_op
    from aqt.utils import tooltip

    assert mw.col is not None
    current_config_id = info.current_deck.config_id
    changed_by_id = {}
    for preset, _old_dr, new_dr, _cmrr in proposals:
        preset.config.desired_retention = new_dr
        changed_by_id[preset.id] = preset

    configs = [p for pid, p in changed_by_id.items() if pid != current_config_id]
    if current_config_id in changed_by_id:
        configs.append(changed_by_id[current_config_id])
    else:
        # deck's own preset wasn't (re)proposed this round -- pass it through
        # unchanged, but it must still come last.
        current_preset = next(
            c.config for c in info.all_config if c.config.id == current_config_id
        )
        configs.append(current_preset)

    request = UpdateDeckConfigs(
        target_deck_id=mw.col.decks.get_current_id(),
        configs=configs,
        mode=UpdateDeckConfigsMode.UPDATE_DECK_CONFIGS_MODE_NORMAL,
        card_state_customizer=info.card_state_customizer,
        new_cards_ignore_review_limit=info.new_cards_ignore_review_limit,
        apply_all_parent_limits=info.apply_all_parent_limits,
        fsrs=True,
        fsrs_health_check=info.fsrs_health_check,
        # unconditional passthrough, same as _run_optimize -- must NOT carry
        # a deck-level desired_retention override into these limits
        limits=info.current_deck.limits,
        fsrs_reschedule=False,
    )

    def on_success(_: object) -> None:
        tooltip("Desired retention updated", parent=mw)
        assert mw.col is not None
        config = _load_config(mw)
        presets_bookkeeping = config.setdefault("presets", {})
        now = int(time.time())
        for preset, old_dr, new_dr, cmrr in proposals:
            entry = presets_bookkeeping.setdefault(str(preset.id), {})
            entry.update(
                lastCmrr=now, cmrrValue=cmrr, prevRetention=old_dr, lastRetention=new_dr
            )
        mw.col.set_config(_CONFIG_KEY, config)

    update_deck_configs_op(parent=mw, input=request).success(
        on_success
    ).run_in_background()
