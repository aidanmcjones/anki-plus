"""renderer_recovery.Recovery: what happens when mw.web's renderer dies.

Ticket 20260923-153047: the main webview's renderer died (out of memory)
and the window stayed grey until Anki+ was quit. Recovery re-shows the
current screen after an abnormal termination, ignores a normal one, and
stops after repeated deaths (falls back to the deck list once, then gives
up) so a card that kills the renderer every time cannot loop.

Runs standalone: `python3 tests/test_renderer_recovery.py` (no Qt needed;
statuses are plain objects with a `.name`, the timer and clock are fakes).
The real-app proof is tests/live/test_renderer_recovery.py.
"""

import importlib.util
import os
import types

_spec = importlib.util.spec_from_file_location(
    "ba_renderer_recovery",
    os.path.join(os.path.dirname(__file__), "..", "renderer_recovery.py"),
)
rr = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rr)

NORMAL = types.SimpleNamespace(name="NormalTerminationStatus")
KILLED = types.SimpleNamespace(name="KilledTerminationStatus")
CRASHED = types.SimpleNamespace(name="CrashedTerminationStatus")


class _MW:
    def __init__(self, state="review"):
        self.state = state
        self.col = object()
        self.moves = []

    def moveToState(self, state):
        self.moves.append(state)
        self.state = state


def _make(state="review"):
    mw = _MW(state)
    timers = []
    clock = {"t": 1000.0}
    notes = []
    rec = rr.Recovery(mw, schedule=lambda ms, fn: timers.append(fn),
                      clock=lambda: clock["t"], notify=notes.append)

    def fire():
        while timers:
            timers.pop(0)()
    return mw, rec, fire, clock, notes


def test_crash_reshows_the_current_screen():
    mw, rec, fire, _clock, notes = _make("review")
    rec.on_terminated(KILLED, 9)
    assert mw.moves == [], "must wait for the timer, not reload inside the signal"
    fire()
    assert mw.moves == ["review"]
    assert notes, "the user is told the view was reloaded"


def test_normal_termination_is_ignored():
    mw, rec, fire, _clock, _n = _make("deckBrowser")
    rec.on_terminated(NORMAL, 0)
    fire()
    assert mw.moves == []


def test_unknown_state_falls_back_to_the_deck_list():
    mw, rec, fire, _clock, _n = _make("profileManager")
    rec.on_terminated(CRASHED, 11)
    fire()
    assert mw.moves == ["deckBrowser"]


def test_repeated_deaths_fall_back_once_then_stop():
    mw, rec, fire, clock, _n = _make("review")
    for _ in range(rr.MAX_RECOVERIES):
        rec.on_terminated(KILLED, 9)
        fire()
        clock["t"] += 5
    assert mw.moves == ["review"] * rr.MAX_RECOVERIES
    mw.state = "review"
    rec.on_terminated(KILLED, 9)   # one too many: deck list instead
    fire()
    assert mw.moves[-1] == "deckBrowser"
    n = len(mw.moves)
    rec.on_terminated(KILLED, 9)   # and after that, nothing
    fire()
    assert len(mw.moves) == n and rec.gave_up


def test_deaths_spread_out_are_all_recovered():
    mw, rec, fire, clock, _n = _make("review")
    for _ in range(10):
        rec.on_terminated(KILLED, 9)
        fire()
        clock["t"] += rr.WINDOW_S + 1
    assert mw.moves == ["review"] * 10


def test_no_collection_means_no_reshow():
    mw, rec, fire, _clock, _n = _make("review")
    mw.col = None
    rec.on_terminated(KILLED, 9)
    fire()
    assert mw.moves == []


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in tests:
        fn()
    print(f"PASS test_renderer_recovery ({len(tests)} tests)")
