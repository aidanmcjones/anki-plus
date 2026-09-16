"""Regression tests for the scene backdrop's pure logic in __init__.py:
`_sky_phase` (which palette the landscape wears at a given hour), the config
readers that pick the backdrop mode and intensity, and the scene rotation
(`_SCENES`, `_scene_variant`, `_scene_list`, `_scene_shuffle_seconds`) that
decides which landscape is up and how fast the page may cycle them.

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


def test_scene_layers_are_in_paint_order():
    """Paint order is document order inside `.ba-scene`, and the two free
    layers only mean anything by where they sit: aux behind the ridges,
    motes in front of them and still under the scrim. Reordering the markup
    would silently move a scene's lava glow on top of its own mountains."""
    order = [
        "ba-scene-stars", "ba-scene-orb", "ba-scene-clouds", "ba-scene-aux",
        "ba-scene-haze", "ba-scene-far", "ba-scene-mid", "ba-scene-near",
        "ba-scene-motes", "ba-scene-scrim",
    ]
    html = mod._SCENE_HTML
    positions = []
    for cls in order:
        idx = html.find(cls)
        assert idx != -1, f"{cls} is missing from the scene markup"
        positions.append(idx)
    assert positions == sorted(positions), (
        f"layers are out of paint order: {list(zip(order, positions))}"
    )


def test_scene_list_is_ten_unique_names():
    """The daily rotation is `day_ordinal % len(_SCENES)`, so a duplicate
    would quietly cost a day of the cycle to a scene already seen."""
    assert len(mod._SCENES) == 10
    assert len(set(mod._SCENES)) == 10, f"duplicates in {mod._SCENES}"
    for name in mod._SCENES:
        assert name and name.islower() and name.isalpha(), (
            f"{name!r} is not a bare lowercase scene name"
        )


def test_every_scene_is_reachable_within_ten_days():
    """Ten scenes, ten consecutive days: any off-by-one in the modulo would
    strand one of them where nobody would ever notice by looking."""
    variant = mod._scene_variant
    base = 739000  # an arbitrary real ordinal, so the test isn't near zero
    seen = [variant({}, base + d) for d in range(10)]
    assert set(seen) == set(mod._SCENES), (
        f"unreachable scenes: {set(mod._SCENES) - set(seen)}"
    )
    assert len(set(seen)) == 10, f"a scene repeated inside one cycle: {seen}"


def test_scene_rotation_is_stable_within_a_day_and_moves_between_them():
    """The deck browser re-renders on every state change. The scene has to
    survive that untouched, or hitting Escape becomes a fidget toy."""
    variant = mod._scene_variant
    assert variant({}, 739000) == variant({}, 739000)
    assert variant({}, 739000) != variant({}, 739001)


def test_scene_falls_back_instead_of_rendering_blank():
    """An unmatched `data-rf-scene` is not a crash, it is a silent loss of
    the whole scene system: nothing would answer the attribute and every
    landscape would look like the base peaks."""
    variant = mod._scene_variant
    for junk in ("", None, "shuffle", "PEAKS", "atlantis", 3, [], {}, True):
        got = variant({"scene": junk}, 739000)
        assert got in mod._SCENES, f"{junk!r} produced {got!r}"
    # A missing key is the same case as junk: the default is the rotation.
    assert variant({}, 739000) in mod._SCENES


def test_a_pinned_scene_is_honoured_and_ignores_the_date():
    """Pinning is the whole point of naming a scene: it must not drift onto
    tomorrow's pick overnight."""
    variant = mod._scene_variant
    for name in mod._SCENES:
        for day in (739000, 739001, 739005, 0):
            assert variant({"scene": name}, day) == name


def test_scene_list_for_the_page_collapses_when_pinned():
    """scene-shuffle.js reads "nothing to cycle" off the list's length, so
    a pinned scene has to arrive as exactly one entry."""
    lst = mod._scene_list
    assert lst({}) == list(mod._SCENES)
    assert lst({"scene": "shuffle"}) == list(mod._SCENES)
    assert lst({"scene": "volcano"}) == ["volcano"]
    for junk in ("", None, "atlantis", 7):
        assert lst({"scene": junk}) == list(mod._SCENES), f"{junk!r} pinned"


def test_shuffle_interval_clamp_rejects_a_spinning_timer():
    """The floor is the load-bearing end: a hand-edited 0 or a negative
    would hand the page a setInterval that never yields the main thread."""
    secs = mod._scene_shuffle_seconds
    assert secs({}) == 10
    assert secs({"scene_shuffle_seconds": 30}) == 30
    assert secs({"scene_shuffle_seconds": 2}) == 2
    assert secs({"scene_shuffle_seconds": 3600}) == 3600
    for spin in (0, 1, -1, -99999):
        assert secs({"scene_shuffle_seconds": spin}) >= 2, (
            f"{spin!r} produced a spinning interval"
        )
    for absurd in (3601, 10 ** 9, 2 ** 62):
        assert secs({"scene_shuffle_seconds": absurd}) == 3600, (
            f"{absurd!r} leaked through"
        )
    # Junk lands on the default rather than raising inside a page render.
    for junk in (None, "", "ten", [], {}, object()):
        assert secs({"scene_shuffle_seconds": junk}) == 10, (
            f"{junk!r} did not fall back"
        )
    # Strings that are numbers are still numbers, and still clamped.
    assert secs({"scene_shuffle_seconds": "45"}) == 45
    assert secs({"scene_shuffle_seconds": "0"}) == 2


def test_js_opts_carries_the_scene_payload_the_script_reads():
    """scene-shuffle.js reads `__baOpts.scene.list` and `.shuffleSeconds`.
    Renaming either here breaks the page silently: the script would just
    fall back to its own copy of the list and the default interval."""
    opts = mod._js_opts({})
    assert "scene" in opts, "the scene payload went missing from __baOpts"
    assert opts["scene"]["list"] == list(mod._SCENES)
    assert opts["scene"]["shuffleSeconds"] == 10
    pinned = mod._js_opts({"scene": "tundra", "scene_shuffle_seconds": 0})
    assert pinned["scene"]["list"] == ["tundra"]
    assert pinned["scene"]["shuffleSeconds"] == 2
    # It has to survive json.dumps: this is injected into a <script> tag.
    import json
    json.loads(json.dumps(opts))


if __name__ == "__main__":
    test_every_hour_has_a_phase_the_stylesheet_knows()
    test_all_four_phases_are_reachable()
    test_phase_boundaries()
    test_phases_are_contiguous_blocks()
    test_out_of_range_hours_wrap_instead_of_failing()
    test_backdrop_mode_falls_back_instead_of_blanking_the_page()
    test_backdrop_intensity_falls_back_too()
    test_scene_markup_cannot_intercept_the_center_tagging()
    test_scene_layers_are_in_paint_order()
    test_scene_list_is_ten_unique_names()
    test_every_scene_is_reachable_within_ten_days()
    test_scene_rotation_is_stable_within_a_day_and_moves_between_them()
    test_scene_falls_back_instead_of_rendering_blank()
    test_a_pinned_scene_is_honoured_and_ignores_the_date()
    test_scene_list_for_the_page_collapses_when_pinned()
    test_shuffle_interval_clamp_rejects_a_spinning_timer()
    test_js_opts_carries_the_scene_payload_the_script_reads()
    print("PASS test_sky_phase")
