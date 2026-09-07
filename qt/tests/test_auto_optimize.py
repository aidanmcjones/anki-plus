# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""Tests for the pure gate-checking logic in aqt.auto_optimize.

These exercise `_should_run()` directly, without a running collection or
Qt application. See qt/aqt/auto_optimize.py's module docstring for how to
run the same checks (as doctests) with a bare system python3, without a
built aqt/anki environment.
"""

from __future__ import annotations

from aqt.auto_optimize import _DEFAULT_CONFIG, _should_run


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
    assert _should_run(default_config(), "deckBrowser", False, False, 7, 400, False) == (
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
