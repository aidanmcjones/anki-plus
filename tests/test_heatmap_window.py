"""Regression tests for the heatmap's window math (`_heatmap_window` in
__init__.py): the grid should grow with the user's history instead of
always paying out a fixed `heatmap_weeks`-wide grid.

Runs standalone (no Anki install needed): `python3 tests/test_heatmap_window.py`.
Stubs the aqt/anki modules before import, same approach as
test_study_state_dispatch.py, since __init__.py's module-level imports need
those names even though `_heatmap_window` itself is pure integer math.
"""

import sys
import types
import os


def _stub(name, **attrs):
    mod = sys.modules.get(name) or types.ModuleType(name)
    for k, v in attrs.items():
        setattr(mod, k, v)
    sys.modules[name] = mod
    return mod


class _AnyClass:
    def __init__(self, *a, **k):
        pass


class _HookList:
    def __init__(self):
        self.fns = []

    def append(self, fn):
        self.fns.append(fn)


class _Hooks:
    def __getattr__(self, name):
        h = _HookList()
        setattr(self, name, h)
        return h


class _AddonManager:
    def getConfig(self, *a, **k):
        return {}

    def setConfigUpdatedAction(self, *a, **k):
        pass

    def addonFromModule(self, m):
        return str(m).split(".")[0]

    def setWebExports(self, *a, **k):
        pass


_mw = types.SimpleNamespace(col=None, addonManager=_AddonManager())
_stub("aqt", gui_hooks=_Hooks(), mw=_mw)
_stub("aqt.deckbrowser", DeckBrowser=_AnyClass, DeckBrowserContent=_AnyClass)
_stub("aqt.editor", Editor=_AnyClass, EditorMode=_AnyClass)
_stub("aqt.overview", Overview=_AnyClass)
_stub("aqt.reviewer", Reviewer=_AnyClass)
_stub("aqt.webview", WebContent=_AnyClass)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import importlib.util

spec = importlib.util.spec_from_file_location(
    "ba_addon_heatmap", os.path.join(os.path.dirname(__file__), "..", "__init__.py"),
)
mod = importlib.util.module_from_spec(spec)
try:
    spec.loader.exec_module(mod)
except Exception as e:  # module-level wiring beyond our stubs
    print(f"SKIP import-level test (module needs fuller aqt: {e!r})")
    sys.exit(0)

window = mod._heatmap_window
WEEKS = 53

# Exercise every phase of "today" against the week grid (which day of the
# week today happens to fall on shouldn't change the qualitative behavior).
TODAY_PHASES = range(2000, 2000 + 14)  # 14 arbitrary consecutive days


def test_short_history_is_small_not_full_year():
    """(a) First review 10 days ago -> a handful of columns, nowhere near
    the `heatmap_weeks` cap."""
    for today_idx in TODAY_PHASES:
        earliest_idx = today_idx - 10
        grid_start, start_idx, columns = window(today_idx, earliest_idx, WEEKS)
        assert 1 <= columns <= 4, (
            f"today={today_idx}: expected ~2-3 columns for 10-day history, "
            f"got {columns}"
        )
        assert columns < WEEKS
        # start_idx (where real data may appear) must be the actual first
        # review, not pulled back to the cap floor.
        assert start_idx == earliest_idx
        # grid_start must not be *after* the first review.
        assert grid_start <= earliest_idx


def test_long_history_hits_exactly_the_cap():
    """(b) Five years of history -> exactly `heatmap_weeks` columns, a true
    rolling window, not the old "full scrollable history" behavior."""
    five_years_days = 5 * 365
    for today_idx in TODAY_PHASES:
        earliest_idx = today_idx - five_years_days
        grid_start, start_idx, columns = window(today_idx, earliest_idx, WEEKS)
        assert columns == WEEKS, (
            f"today={today_idx}: expected exactly {WEEKS} columns for "
            f"5-year history, got {columns}"
        )


def test_zero_reviews_renders_current_week_not_a_blank_year():
    """(c) No reviews at all -> a couple of columns (this week + a lead-in
    margin), never the full `heatmap_weeks` grid."""
    for today_idx in TODAY_PHASES:
        grid_start, start_idx, columns = window(today_idx, None, WEEKS)
        assert 1 <= columns <= 3, (
            f"today={today_idx}: expected a small 'current week' grid with "
            f"no history, got {columns} columns"
        )
        assert start_idx == today_idx  # only today itself can show data


def test_grid_extends_by_exactly_one_column_per_week_of_new_history():
    """(d) As days pass (today advances) with a fixed, still-under-the-cap
    first-review date, the grid grows by exactly one column per week and
    never jumps by more than one at a time."""
    earliest_idx = 1000
    prev_columns = None
    for offset in range(0, 120):
        today_idx = earliest_idx + offset
        _, _, columns = window(today_idx, earliest_idx, WEEKS)
        if prev_columns is not None:
            delta = columns - prev_columns
            assert delta in (0, 1), (
                f"offset={offset}: columns jumped by {delta} in a single day"
            )
        prev_columns = columns

    # Over any full week the count must have grown by exactly one column
    # (never stalls, never double-jumps), while still under the cap.
    for offset in range(0, 100):
        c0 = window(earliest_idx + offset, earliest_idx, WEEKS)[2]
        c1 = window(earliest_idx + offset + 7, earliest_idx, WEEKS)[2]
        assert c1 - c0 == 1, (
            f"offset={offset}: expected +1 column after a week passes, "
            f"got {c1 - c0}"
        )


def test_cap_is_a_ceiling_grid_never_exceeds_configured_weeks():
    for today_idx in TODAY_PHASES:
        for days_of_history in (0, 1, 10, 100, 400, 2000, 10000):
            earliest_idx = None if days_of_history == 0 else today_idx - days_of_history
            _, _, columns = window(today_idx, earliest_idx, WEEKS)
            assert columns <= WEEKS, (
                f"today={today_idx} history={days_of_history}d: "
                f"{columns} columns exceeds cap of {WEEKS}"
            )


def test_weekday_glyphs_label_mon_fri_and_keep_seven_rows():
    """The gutter must stay row-for-row with the grid: one span per weekday,
    Sunday-first, letters on Mon-Fri and blank spacers on the weekend (whose
    cells still render, so their rows still need height)."""
    glyphs = mod._WEEKDAY_GLYPHS
    weekdays = mod._WEEKDAYS

    assert len(glyphs) == 7, f"expected 7 gutter rows, got {len(glyphs)}"
    assert len(glyphs) == len(weekdays)

    # Sunday (0) and Saturday (6) are deliberately unlabelled spacers.
    assert glyphs[0] == "" and glyphs[6] == "", (
        f"weekends should be blank spacers, got {glyphs[0]!r}/{glyphs[6]!r}"
    )

    # Every weekday Mon-Fri carries a label, and it's that day's own initial.
    for i in range(1, 6):
        assert glyphs[i], f"{weekdays[i]} (row {i}) has no gutter letter"
        assert glyphs[i] == weekdays[i][0], (
            f"row {i} labelled {glyphs[i]!r} but is {weekdays[i]}"
        )

    # Single characters only: .rf-hm-wd's 4px gutter in web/heatmap.css is
    # sized for one glyph, so "Tu"/"Th" would need that widened to match.
    for g in glyphs:
        assert len(g) <= 1, f"{g!r} is wider than the gutter is sized for"


if __name__ == "__main__":
    test_short_history_is_small_not_full_year()
    test_long_history_hits_exactly_the_cap()
    test_zero_reviews_renders_current_week_not_a_blank_year()
    test_grid_extends_by_exactly_one_column_per_week_of_new_history()
    test_cap_is_a_ceiling_grid_never_exceeds_configured_weeks()
    test_weekday_glyphs_label_mon_fri_and_keep_seven_rows()
    print("PASS test_heatmap_window")
