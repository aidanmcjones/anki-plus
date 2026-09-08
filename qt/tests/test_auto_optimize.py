# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""Tests for the pure gate-checking logic in aqt.auto_optimize.

These exercise `_should_run()` directly, without a running collection or
Qt application. See qt/aqt/auto_optimize.py's module docstring for how to
run the same checks (as doctests) with a bare system python3, without a
built aqt/anki environment.
"""

from __future__ import annotations

from aqt.auto_optimize import (
    _DEFAULT_CONFIG,
    _current_fsrs_params,
    _escape_preset_name_for_search,
    _propose_retention,
    _should_run,
)


def default_config() -> dict:
    return dict(_DEFAULT_CONFIG)


def test_happy_path_runs():
    assert _should_run(default_config(), "deckBrowser", False, True, 7, 400, False) == (
        True,
        "",
    )


def test_disabled_in_config():
    config = {**default_config(), "enabled": False}
    assert _should_run(config, "deckBrowser", False, True, 7, 400, False) == (
        False,
        "disabled",
    )


def test_wrong_screen():
    assert _should_run(default_config(), "review", False, True, 7, 400, False) == (
        False,
        "not-deck-browser",
    )


def test_busy():
    assert _should_run(default_config(), "deckBrowser", True, True, 7, 400, False) == (
        False,
        "busy",
    )


def test_fsrs_disabled():
    assert _should_run(
        default_config(), "deckBrowser", False, False, 7, 400, False
    ) == (
        False,
        "fsrs-disabled",
    )


def test_interval_not_elapsed():
    assert _should_run(default_config(), "deckBrowser", False, True, 3, 400, False) == (
        False,
        "interval-not-elapsed",
    )


def test_insufficient_reviews():
    assert _should_run(default_config(), "deckBrowser", False, True, 7, 10, False) == (
        False,
        "insufficient-reviews",
    )


def test_force_bypasses_enabled_interval_and_review_count():
    config = {**default_config(), "enabled": False}
    assert _should_run(config, "deckBrowser", False, True, 0, 0, True) == (True, "")


def test_force_does_not_bypass_wrong_screen():
    assert _should_run(default_config(), "review", False, True, 0, 0, True) == (
        False,
        "not-deck-browser",
    )


def test_force_does_not_bypass_busy():
    assert _should_run(default_config(), "deckBrowser", True, True, 0, 0, True) == (
        False,
        "busy",
    )


def test_force_does_not_bypass_fsrs_disabled():
    assert _should_run(default_config(), "deckBrowser", False, False, 0, 0, True) == (
        False,
        "fsrs-disabled",
    )


# --- _propose_retention (Phase 4 CMRR damping) -----------------------------


def test_propose_retention_clamps_upward_move():
    assert round(_propose_retention(0.80, 0.90, 0.02), 2) == 0.82


def test_propose_retention_clamps_downward_move():
    assert round(_propose_retention(0.90, 0.70, 0.02), 2) == 0.88


def test_propose_retention_skips_tiny_change():
    assert _propose_retention(0.85, 0.85, 0.03) is None


def test_propose_retention_skips_at_low_clamp_boundary():
    assert _propose_retention(0.70, 0.70, 0.03) is None


def test_propose_retention_skips_at_high_clamp_boundary():
    assert _propose_retention(0.95, 0.95, 0.03) is None


def test_propose_retention_ordinary_downward_move():
    assert round(_propose_retention(0.90, 0.80, 0.03), 2) == 0.87


def test_propose_retention_ordinary_upward_move():
    assert round(_propose_retention(0.90, 0.99, 0.03), 2) == 0.93


# --- small helpers used while building the per-preset simulation request ---


class _FakeConfig:
    def __init__(self, p4=(), p5=(), p6=()):
        self.fsrs_params_4 = list(p4)
        self.fsrs_params_5 = list(p5)
        self.fsrs_params_6 = list(p6)


def test_current_fsrs_params_prefers_6_then_5_then_4():
    assert _current_fsrs_params(_FakeConfig(p4=[1], p5=[2], p6=[3])) == [3]
    assert _current_fsrs_params(_FakeConfig(p4=[1], p5=[2])) == [2]
    assert _current_fsrs_params(_FakeConfig(p4=[1])) == [1]
    assert _current_fsrs_params(_FakeConfig()) == []


def test_escape_preset_name_for_search_matches_ts_regex():
    # mirrors ts/routes/deck-options/lib.ts: name.replace(/([\\"])/g, "\\$1")
    assert (
        _escape_preset_name_for_search('My "Weird" Pre\\set')
        == 'My \\"Weird\\" Pre\\\\set'
    )
