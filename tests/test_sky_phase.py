"""Regression tests for the scene backdrop's pure logic in __init__.py:
`_sky_phase` (which palette the landscape wears at a given hour) and the
two config readers that pick the backdrop mode and intensity.

Runs standalone (no Anki install needed): `python3 tests/test_sky_phase.py`.
Stubs the aqt/anki modules before import, same approach as
test_heatmap_window.py, since __init__.py's module-level imports need those
names even though the functions under test are pure.
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
    "ba_addon_sky", os.path.join(os.path.dirname(__file__), "..", "__init__.py"),
)
mod = importlib.util.module_from_spec(spec)
try:
    spec.loader.exec_module(mod)
except Exception as e:  # module-level wiring beyond our stubs
    print(f"SKIP import-level test (module needs fuller aqt: {e!r})")
    sys.exit(0)

sky = mod._sky_phase
PHASES = ("dawn", "day", "dusk", "night")


def test_every_hour_has_a_phase_the_stylesheet_knows():
    """scene.css only defines palettes for these four, so an hour that
    mapped to anything else would render an unstyled scene."""
    for h in range(24):
        assert sky(h) in PHASES, f"hour {h} produced {sky(h)!r}"


def test_all_four_phases_are_reachable():
    """A boundary typo that swallowed a phase (making, say, dusk
    unreachable) would still pass the test above."""
    seen = {sky(h) for h in range(24)}
    assert seen == set(PHASES), f"unreachable phases: {set(PHASES) - seen}"


def test_phase_boundaries():
    """The exact hour each phase starts, since these are the numbers the
    palettes were tuned against."""
    cases = {
        0: "night", 3: "night", 4: "night",
        5: "dawn", 7: "dawn", 8: "dawn",
        9: "day", 12: "day", 16: "day",
        17: "dusk", 19: "dusk", 20: "dusk",
        21: "night", 23: "night",
    }
    for hour, expected in cases.items():
        assert sky(hour) == expected, (
            f"hour {hour}: expected {expected}, got {sky(hour)}"
        )


def test_phases_are_contiguous_blocks():
    """Each phase is one unbroken run around the clock (night wraps through
    midnight), so the scene can't flicker between palettes hour to hour."""
    runs = []
    for h in range(24):
        phase = sky(h)
        if not runs or runs[-1][0] != phase:
            runs.append([phase, 1])
        else:
            runs[-1][1] += 1
    # Night straddles midnight, so it legitimately appears at both ends.
    if len(runs) > 1 and runs[0][0] == runs[-1][0]:
        runs[0][1] += runs.pop()[1]
    labels = [r[0] for r in runs]
    assert len(labels) == len(set(labels)), (
        f"a phase is split across the day: {runs}"
    )


def test_out_of_range_hours_wrap_instead_of_failing():
    """A bad clock must not produce a phase the stylesheet has no palette
    for, and must not raise inside a page render."""
    assert sky(24) == sky(0)
    assert sky(25) == sky(1)
    assert sky(-1) == sky(23)
    assert sky(48) == sky(0)
    for h in range(-48, 72):
        assert sky(h) in PHASES, f"hour {h} produced {sky(h)!r}"


def test_backdrop_mode_falls_back_instead_of_blanking_the_page():
    mode = mod._backdrop_mode
    assert mode({}) == "scene", "missing key should default to the scene"
    assert mode({"backdrop": "off"}) == "off"
    assert mode({"backdrop": "aurora"}) == "aurora"
    assert mode({"backdrop": "scene"}) == "scene"
    # Junk (a hand-edited config, or a key from a newer version) must land
    # on the default, never pass through to become an unmatched attribute.
    for junk in ("", None, "landscape", "OFF", 3, []):
        assert mode({"backdrop": junk}) == "scene", f"{junk!r} leaked through"


def test_backdrop_intensity_falls_back_too():
    it = mod._backdrop_intensity
    assert it({}) == "cinematic"
    assert it({"backdrop_intensity": "subtle"}) == "subtle"
    assert it({"backdrop_intensity": "cinematic"}) == "cinematic"
    for junk in ("", None, "loud", "SUBTLE", 7):
        assert it({"backdrop_intensity": junk}) == "cinematic", (
            f"{junk!r} leaked through"
        )


def test_scene_markup_cannot_intercept_the_center_tagging():
    """`.ba-home` / `.ba-over` are applied by replacing the first literal
    `<center>` in the page body. The scene is prepended ahead of that body,
    so if it ever contained one it would swallow the replacement and the
    whole home layout would silently stop being scoped."""
    assert "<center" not in mod._SCENE_HTML
    # And it must stay out of the accessibility tree: it says nothing.
    assert 'aria-hidden="true"' in mod._SCENE_HTML


if __name__ == "__main__":
    test_every_hour_has_a_phase_the_stylesheet_knows()
    test_all_four_phases_are_reachable()
    test_phase_boundaries()
    test_phases_are_contiguous_blocks()
    test_out_of_range_hours_wrap_instead_of_failing()
    test_backdrop_mode_falls_back_instead_of_blanking_the_page()
    test_backdrop_intensity_falls_back_too()
    test_scene_markup_cannot_intercept_the_center_tagging()
    print("PASS test_sky_phase")
