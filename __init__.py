"""Anki Design — a from-scratch Anki UI redesign.

  * override Anki's design tokens so its own components recolor coherently
  * redesign the deck homepage (card rows, count chips, integrated actions)
  * restyle the top toolbar
  * hide Anki's native bottom strip on the deck list (its actions are moved
    into the page); keep it everywhere else (reviewer answer buttons, etc.)
  * review-activity heatmap on the deck list
  * reviewer progress bar

Everything visible is driven by web/ assets and config so iterating on the
look is just editing CSS / config. In a `make dev` worktree, web/*.css and
web/*.js hot-reload live; Python changes still need an Anki restart.
"""

import datetime
import html
import math
import os
import threading
import time
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote as _quote

from aqt import gui_hooks, mw
from aqt.deckbrowser import DeckBrowser, DeckBrowserContent
from aqt.editor import Editor, EditorMode
from aqt.overview import Overview
from aqt.reviewer import Reviewer
from aqt.webview import WebContent

# Optional bits — guarded so a renamed API can never break the whole add-on.
# NB: the webview's `context` for the bars is the *wrapper* passed to
# stdHtml() — TopToolbar / BottomToolbar — NOT Toolbar / BottomBar. Matching
# the wrong class is why the toolbar was never themed.
try:
    from aqt.toolbar import TopToolbar as _ToolbarCtx
except Exception:
    _ToolbarCtx = None
try:
    from aqt.toolbar import BottomToolbar as _BottomCtx
except Exception:
    _BottomCtx = None
# The reviewer's bottom strip ships as a separate context — `ReviewerBottomBar`,
# NOT the generic `BottomToolbar` used elsewhere (e.g., deck-browser bottom).
# We need both so theme + reviewer-bottom.css both apply.
try:
    from aqt.reviewer import ReviewerBottomBar as _ReviewerBottomCtx
except Exception:
    _ReviewerBottomCtx = None
try:
    from aqt.deckbrowser import DeckBrowserBottomBar as _DeckBrowserBottomCtx
except Exception:
    _DeckBrowserBottomCtx = None
try:
    from aqt.qt import QTimer
except Exception:
    QTimer = None
# The Browse tab's rendered-card pane declares itself as the stdHtml
# context of its webview, so `on_webview_will_set_content` can give it the
# exact same stylesheet stack the reviewer gets.
try:
    from .browse_preview import PreviewPane as _PreviewCtx
except Exception:
    _PreviewCtx = None

ADDON_DIR = os.path.basename(os.path.dirname(__file__))
WEB = f"/_addons/{ADDON_DIR}/web"
# user_files/ is Anki's conventional per-addon data dir: it survives add-on
# updates (unlike the rest of the tree, which gets replaced wholesale), so
# it's where the nature-video library lives — curated separately from this
# checkout. It is kept out of the shipped .ankiaddon by build.py's
# EXCLUDE_DIRS (the actual guard — see build.py), not by anything Anki does
# on its own. See nature.py.
USER_FILES = f"/_addons/{ADDON_DIR}/user_files"

# Let Anki serve our static files to the embedded web views. `.*` in the
# old pattern matched a literal `..` segment too, so
# "user_files/../../<other-addon>/x" fullmatched it — a path-traversal
# hole into every other add-on's user_files. This forbids `..` as a path
# segment while still allowing normal filenames that merely contain dots.
mw.addonManager.setWebExports(
    __name__, r"(?:web|user_files/nature)(?:/(?!\.\.(?:/|$))[^/]+)+"
)


def _config() -> Dict[str, Any]:
    return mw.addonManager.getConfig(__name__) or {}


def _hero_mode(cfg: Optional[Dict[str, Any]] = None) -> bool:
    """True when the home page should show the single-deck hero: the user
    has exactly one top-level deck AND hasn't switched the hero off."""
    cfg = cfg if cfg is not None else _config()
    if not cfg.get("single_deck_hero", True):
        return False
    try:
        return _top_decks_count() == 1
    except Exception:
        return False


def _background_rules(cfg: Dict[str, Any]) -> str:
    """CSS rule blocks that swap the page paper (and the derived panel /
    Anki --canvas tokens) for the user's background overrides. Emits
    nothing while both settings are blank, so the default paper is
    untouched."""
    from . import colors as _colors
    out = ""
    light = _colors.background_override(cfg, dark=False)
    if light:
        paper, panel = light
        decl = (
            f"--rf-paper:{paper}!important;--rf-panel:{panel}!important;"
            f"--rf-bg:{paper}!important;--canvas:{paper}!important;"
            f"--canvas-elevated:{panel}!important;--canvas-inset:{paper}!important;"
        )
        out += (
            f":root[data-rf-theme=\"light\"]{{{decl}}}"
            f"@media (prefers-color-scheme:light)"
            f"{{:root:not([data-rf-theme=\"dark\"]){{{decl}}}}}"
        )
    dark = _colors.background_override(cfg, dark=True)
    if dark:
        paper, panel = dark
        decl = (
            f"--rf-paper:{paper}!important;--rf-panel:{panel}!important;"
            f"--rf-bg:{paper}!important;--canvas:{paper}!important;"
            f"--canvas-elevated:{panel}!important;--canvas-inset:{paper}!important;"
        )
        out += (
            f":root[data-rf-theme=\"dark\"]{{{decl}}}"
            f"@media (prefers-color-scheme:dark)"
            f"{{:root:not([data-rf-theme=\"light\"]){{{decl}}}}}"
        )
    return out


_BACKDROP_CHOICES = ("off", "black", "aurora", "scene", "video")
_BACKDROP_INTENSITIES = ("subtle", "cinematic")


def _backdrop_mode(cfg: Dict[str, Any]) -> str:
    """Which backdrop the home and overview screens get: nothing, the
    abstract aurora wash, or the full layered landscape. Unknown values
    fall back to the default rather than blanking the page."""
    choice = str(cfg.get("backdrop", "scene") or "scene")
    return choice if choice in _BACKDROP_CHOICES else "scene"


def _backdrop_intensity(cfg: Dict[str, Any]) -> str:
    choice = str(cfg.get("backdrop_intensity", "cinematic") or "cinematic")
    return choice if choice in _BACKDROP_INTENSITIES else "cinematic"


_SCENES = (
    "peaks", "dunes", "forest", "canyon", "lake",
    "volcano", "isles", "tundra", "spires", "ruins",
)
# Below 2s the swap reads as a strobe rather than a change of scene; above an
# hour it has stopped being a rotation at all. The floor is the load-bearing
# half: a hand-edited 0 would otherwise hand the page a setInterval that never
# yields.
_SCENE_SECONDS_DEFAULT = 10
_SCENE_SECONDS_MIN = 2
_SCENE_SECONDS_MAX = 3600


def _scene_choice(cfg: Dict[str, Any]) -> str:
    """The raw `scene` setting, reduced to either a known scene name or
    `"shuffle"`. Junk and missing values land on shuffle rather than on a
    name no stylesheet answers to, which would render the default peaks
    silhouettes under a `data-rf-scene` nobody matches."""
    choice = str(cfg.get("scene", "shuffle") or "shuffle")
    return choice if choice in _SCENES else "shuffle"


def _scene_variant(cfg: Dict[str, Any], day_ordinal: int) -> str:
    """Which landscape the server renders first.

    Pinned to the configured scene if there is one. Otherwise the pick is a
    function of the date, not of the render: the deck browser re-renders on
    every state change, and a backdrop that reshuffled each time you hit
    Escape would be a fidget toy. The page script takes over the shuffling
    from here; this only has to be stable and non-blank.
    """
    choice = _scene_choice(cfg)
    if choice != "shuffle":
        return choice
    return _SCENES[int(day_ordinal) % len(_SCENES)]


def _scene_shuffle_seconds(cfg: Dict[str, Any]) -> int:
    try:
        secs = int(cfg.get("scene_shuffle_seconds", _SCENE_SECONDS_DEFAULT))
    except (TypeError, ValueError):
        secs = _SCENE_SECONDS_DEFAULT
    return max(_SCENE_SECONDS_MIN, min(_SCENE_SECONDS_MAX, secs))


def _backdrop_motion_mode(cfg: Dict[str, Any]) -> str:
    """Which "reduce motion" signals the backdrop obeys.

    "always" (the default) obeys the OS-level `prefers-reduced-motion` only,
    which is what every other motion gate in this add-on does. "auto" also
    obeys Anki's own Preferences > Reduce motion.

    Default is "always" rather than "auto" because Anki's own flag defaults
    to ON (`ProfileManager.reduce_motion` returns True when unset), so
    honouring it by default would freeze the backdrop for essentially every
    user who never opened that preference, including the people who install
    this add-on precisely for the backdrop. Opting in keeps the accessible
    behaviour available without silently switching the feature off. The
    OS-level query is obeyed in both modes and is not configurable: that one
    is a deliberate statement by the user, not a default they inherited.
    """
    choice = str(cfg.get("backdrop_motion", "always") or "always")
    return choice if choice in ("always", "auto") else "always"


def _scene_list(cfg: Dict[str, Any]) -> list:
    """What the page is allowed to cycle through. A pinned scene collapses
    this to a single entry, which is how scene-shuffle.js learns to stand
    still: one list to keep in sync instead of a list plus a flag that can
    disagree with it."""
    choice = _scene_choice(cfg)
    return list(_SCENES) if choice == "shuffle" else [choice]


def _sky_phase(hour: int) -> str:
    """Time of day as one of dawn/day/dusk/night, for the scene palette.

    Computed here rather than in the page because the deck browser
    re-renders on every state change, so the attribute is refreshed for
    free — where JS would need a timer, and this add-on has no loop or
    visibility-guard pattern to hang one off. Fixed hours rather than real
    sunrise/sunset: no location data, and the point is a mood, not an
    almanac. Hours outside 0-23 wrap, so a bad clock can't produce a
    phase the stylesheet has no palette for.
    """
    h = int(hour) % 24
    if 5 <= h < 9:
        return "dawn"
    if 9 <= h < 17:
        return "day"
    if 17 <= h < 21:
        return "dusk"
    return "night"


# Layer order is paint order: sky glow and stars behind the ranges, the
# scrim over all of them so deck rows and the heatmap keep their contrast.
# Pure decoration with nothing to announce, so it leaves the a11y tree.
# Contains no `<center>`, which the .ba-home / .ba-over tagging below finds
# by string replacement.
#
# `aux` and `motes` are unpainted and belong to whichever scene wants them:
# aux behind the ridges (a lava glow, a floating mass), motes in front of
# them but under the scrim (snow, embers, fireflies). They are here rather
# than added per scene because every scene shares this one markup string,
# and an empty div costs nothing to the scenes that ignore it. Every scene
# in `web/scene.css` is one stylesheet away from depth without new DOM.
_SCENE_HTML = (
    '<div class="ba-scene" aria-hidden="true">'
    '<div class="ba-scene-stars"></div>'
    '<div class="ba-scene-orb"></div>'
    '<div class="ba-scene-clouds"></div>'
    '<div class="ba-scene-aux"></div>'
    '<div class="ba-scene-haze"></div>'
    '<div class="ba-scene-range ba-scene-far"></div>'
    '<div class="ba-scene-range ba-scene-mid"></div>'
    '<div class="ba-scene-range ba-scene-near"></div>'
    '<div class="ba-scene-motes"></div>'
    '<div class="ba-scene-scrim"></div>'
    '</div>'
)

_VIDEO_EMPTY_WARNED = False


def _video_state(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """Resolves the nature-video library plus the `video_selection` /
    `video_rotate_seconds` config into what both `_js_opts` and the
    injection branch need. Only meaningful (and only ever called) when
    backdrop mode is "video" — callers are responsible for computing this
    once per render and threading the same dict into both `_js_opts` and
    the injection branch, so index.json is read once and the two agree
    exactly on what's playable. Importing `nature` here (rather than at
    module scope) also keeps every other backdrop mode — the common
    case — from paying for this module or a filesystem read at all."""
    from . import nature as _nature
    videos_all = _nature.read_index()
    selection = _nature.normalize_selection(
        cfg.get("video_selection"), videos_all
    )
    filtered = _nature.filtered_for_selection(videos_all, selection)
    return {
        "videos_all": videos_all,
        "selection": selection,
        "filtered": filtered,
        "rotate_seconds": _nature.rotate_seconds(cfg.get("video_rotate_seconds")),
    }


# Layer order mirrors `_SCENE_HTML`: a fixed aria-hidden container behind
# the UI, two stacked <video> elements so video-backdrop.js can crossfade
# between clips (swap the hidden one's source, fade it up, swap which is
# "front") without a black flash, then a scrim mirroring `.ba-scene-scrim`
# so deck text keeps its contrast over whatever the clip is doing.
#
# The first clip's URL is set server-side, same reasoning as
# `_scene_variant`: the very first paint should already show something
# playing rather than wait on the page script to pick one.
def _video_html(initial_url: str) -> str:
    src_attr = (
        f' src="{html.escape(initial_url, quote=True)}"' if initial_url else ""
    )
    return (
        # `ba-video--preinit` is what makes `.ba-video-a` visible before
        # video-backdrop.js has run — see video-backdrop.css. The script's
        # `init()` strips this class the moment it takes over; if it never
        # runs at all, it just stays and the CSS fallback holds.
        '<div class="ba-video ba-video--preinit" aria-hidden="true">'
        f'<video class="ba-video-el ba-video-a" muted autoplay loop '
        f'playsinline preload="auto"{src_attr}></video>'
        '<video class="ba-video-el ba-video-b" muted autoplay loop '
        'playsinline preload="auto"></video>'
        '<div class="ba-video-scrim"></div>'
        '</div>'
    )


def _js_opts(
    cfg: Dict[str, Any], video_state: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    """Feature flags handed to the page scripts as `window.__baOpts`.
    Every key mirrors a Settings toggle; the JS treats a missing key as
    "on" so older payloads keep working.

    This runs on EVERY themed surface — deck browser, overview, reviewer,
    editor, both toolbars, the sidebar webview — so it must stay cheap in
    the common case. `video_state` is never computed here: only a caller
    that already has it (the deck-browser/overview injection branch, the
    one surface that actually renders `.ba-video` markup) can populate the
    "video" payload at all. Every other surface — reviewer, editor,
    toolbars, sidebar — calls this without `video_state` and gets the
    empty payload, even when backdrop mode is "video", because none of
    them show a video backdrop; there is nothing for the ~4.6KB clip list
    to do there but bloat every render. This used to fall back to a fresh
    `_video_state(cfg)` call whenever `video_state` was None, which meant
    those surfaces were reading and re-embedding index.json on every
    single render for no reason — see B5 in the fix history."""
    startup = str(cfg.get("deck_tree_startup", "remember") or "remember")
    if startup not in ("remember", "expanded", "collapsed"):
        startup = "remember"
    if _backdrop_mode(cfg) == "video" and video_state is not None:
        video_payload = [
            {
                "url": f"{USER_FILES}/nature/{_quote(v['file'])}",
                "file": v["file"],
                "biome": v["biome"],
                "title": v["title"],
            }
            for v in video_state["filtered"]
        ]
        rotate_seconds = video_state["rotate_seconds"]
    else:
        video_payload = []
        rotate_seconds = 0
    return {
        "cmdk": bool(cfg.get("cmdk", True)),
        "skipOverview": bool(cfg.get("skip_overview", True)),
        # scene-shuffle.js reads both rather than carrying its own copy, so
        # a scene added to `_SCENES` reaches the page without touching the
        # JS. A pinned scene arrives as a one-entry list, which the script
        # treats as nothing to cycle.
        "scene": {
            "list": _scene_list(cfg),
            "motion": _backdrop_motion_mode(cfg),
            "shuffleSeconds": _scene_shuffle_seconds(cfg),
        },
        # video-backdrop.js reads this the same way scene-shuffle.js reads
        # "scene" above: the filtered, resolved list IS the rotation —
        # Python already applied `video_selection`, so the page never has
        # to know what a biome or a pinned file even is.
        "video": {
            "videos": video_payload,
            "rotateSeconds": rotate_seconds,
            "motion": _backdrop_motion_mode(cfg),
            # Seeds init()'s `occluded` flag so a deck-browser/overview
            # render that happens WHILE an embed overlay is already open
            # (e.g. _refresh_views() firing from a Settings toggle) starts
            # paused instead of decoding invisibly behind the overlay for
            # however long the overlay stays up. `_push_video_occlusion()`
            # covers every occlusion change AFTER the page has loaded; this
            # covers the one it can't — the state as of this render.
            "occluded": _any_embed_open(),
        },
        "deckList": {
            "startup": startup,
            "dragMove": bool(cfg.get("deck_drag_move", True)),
            "singleHero": bool(cfg.get("single_deck_hero", True)),
            "subsections": bool(cfg.get("deck_subsections", True)),
        },
        "reviewer": {
            "clickToReveal": bool(cfg.get("click_to_reveal", True)),
            "pressFeedback": bool(cfg.get("press_feedback", True)),
            "cardStyling": bool(cfg.get("reviewer_card_styling", True)),
            "answerButtons": str(cfg.get("reviewer_answer_buttons", "intervals")),
        },
    }


# Fixed-palette heatmaps. Four shades, low intensity → high.
# Dark-mode set: eyeballed against the near-black canvas (#0b0c0f).
# Light-mode set: eyeballed against the cream canvas (#f6f3ec) — the
# dark sets render as harsh blobs in light mode, so each palette gets
# its own light ramp running from pale-tinted to saturated.
_HEATMAP_DARK: Dict[str, list] = {
    "green":  ["#0e4429", "#006d32", "#26a641", "#39d353"],
    "teal":   ["#0c4747", "#0d7d76", "#14b8a6", "#5eead4"],
    "violet": ["#3b1670", "#6b2da3", "#9656ce", "#c896ec"],
    "rose":   ["#5c1024", "#8c1338", "#cf2553", "#f43f5e"],
    "amber":  ["#5a3a06", "#a87212", "#e09524", "#f7c149"],
}
_HEATMAP_LIGHT: Dict[str, list] = {
    "green":  ["#cdebd4", "#84d18b", "#3aa552", "#1a6c2e"],
    "teal":   ["#cdebe7", "#86d3c8", "#22a89a", "#0e6b62"],
    "violet": ["#e5ddf6", "#bba6e2", "#7c52d6", "#3f1f8a"],
    "rose":   ["#fbd9df", "#f29eaa", "#dc3a59", "#7a1230"],
    "amber":  ["#fde6b6", "#f1c46c", "#cf8418", "#7a4708"],
}


def _shades_from_accent(accent: str, bg: tuple) -> list:
    """Blend the accent toward a background color at four ratios.
    Level 4 is the full accent; lower levels are progressively desaturated
    toward the canvas tint."""
    try:
        r = int(accent[1:3], 16)
        g = int(accent[3:5], 16)
        b = int(accent[5:7], 16)
    except Exception:
        r, g, b = 108, 140, 255  # default accent
    out = []
    for ratio in (0.22, 0.45, 0.72, 1.0):
        nr = int(bg[0] + (r - bg[0]) * ratio)
        ng = int(bg[1] + (g - bg[1]) * ratio)
        nb = int(bg[2] + (b - bg[2]) * ratio)
        out.append(f"#{nr:02x}{ng:02x}{nb:02x}")
    return out


def _heatmap_palette_decl(choice: str, accent: str) -> str:
    """CSS rule block (not a single declaration list) that paints the
    heatmap cells per palette + theme. Emits two rule blocks — one for
    each of dark and light — plus a @media block so the heatmap follows
    the OS appearance when the theme is set to "system"."""
    if choice in _HEATMAP_DARK:
        dark = _HEATMAP_DARK[choice]
        light = _HEATMAP_LIGHT[choice]
    else:
        dark = _shades_from_accent(accent, bg=(12, 14, 22))
        light = _shades_from_accent(accent, bg=(246, 243, 236))
    d1, d2, d3, d4 = dark
    l1, l2, l3, l4 = light
    # The !important wins over theme.css's per-theme defaults. We emit a
    # CSS rule block here (separate from the page's main injection) so the
    # selectors can target dark/light themes independently.
    return (
        f":root,:root[data-rf-theme=\"dark\"]"
        f"{{--rf-hm-l1:{d1}!important;--rf-hm-l2:{d2}!important;"
        f"--rf-hm-l3:{d3}!important;--rf-hm-l4:{d4}!important;}}"
        f":root[data-rf-theme=\"light\"]"
        f"{{--rf-hm-l1:{l1}!important;--rf-hm-l2:{l2}!important;"
        f"--rf-hm-l3:{l3}!important;--rf-hm-l4:{l4}!important;}}"
        f"@media (prefers-color-scheme:light)"
        f"{{:root:not([data-rf-theme=\"dark\"])"
        f"{{--rf-hm-l1:{l1}!important;--rf-hm-l2:{l2}!important;"
        f"--rf-hm-l3:{l3}!important;--rf-hm-l4:{l4}!important;}}}}"
    )


def _is(context: Any, cls: Any) -> bool:
    return bool(cls) and isinstance(context, cls)


# --------------------------------------------------------------------------- #
# Theme + asset injection
# --------------------------------------------------------------------------- #
def on_webview_will_set_content(web_content: WebContent, context: Optional[Any]) -> None:
    is_editor = isinstance(context, Editor)
    themed = (
        isinstance(context, (DeckBrowser, Overview, Reviewer))
        or _is(context, _ToolbarCtx)
        or _is(context, _BottomCtx)
        or _is(context, _ReviewerBottomCtx)
        or _is(context, _DeckBrowserBottomCtx)
        or _is(context, _PreviewCtx)
        or is_editor
    )
    if not themed:
        return

    cfg = _config()
    accent = cfg.get("accent", "#6c8cff")
    theme_pref = cfg.get("theme", "system")  # "system" | "light" | "dark"
    density = cfg.get("density", "comfortable")
    palette_choice = cfg.get("heatmap_palette", "accent")
    card_width_choice = cfg.get("reviewer_card_width", "medium")
    font_size_choice = cfg.get("reviewer_font_size", "medium")
    # User-supplied display fonts are prepended to the existing stacks.
    serif_user = (cfg.get("font_serif") or "").strip()
    sans_user = (cfg.get("font_sans") or "").strip()
    serif_decl = f"--rf-serif:{serif_user}, ui-serif, 'New York', Georgia, serif;" \
        if serif_user else ""
    sans_decl = f"--rf-sans:{sans_user}, ui-sans-serif, -apple-system, system-ui, sans-serif;" \
        if sans_user else ""

    # Reviewer geometry — variables surface in reviewer.css.
    width_map = {"narrow": "640px", "medium": "780px", "wide": "920px",
                 "full": "100%"}
    # Sizes tuned for the reviewer rebuild's reading column.
    fontsize_map = {"small": "22px", "medium": "28px", "large": "34px",
                    "x-large": "40px"}
    # Chrome (header + bottom buttons) scales with the same setting so the
    # whole reviewer surface grows together. Multipliers track the card
    # font-size ratios above (22/28/34/40 → 0.85/1.0/1.25/1.5) so chrome
    # grows enough to stay in proportion with the larger card text.
    chrome_scale_map = {"small": "0.85", "medium": "1", "large": "1.25",
                        "x-large": "1.5"}
    card_width = width_map.get(card_width_choice, "780px")
    card_font_size = fontsize_map.get(font_size_choice, "28px")
    chrome_scale = chrome_scale_map.get(font_size_choice, "1")
    reviewer_decl = (
        f"--rf-card-max-width:{card_width};"
        f"--rf-card-font-size:{card_font_size};"
        f"--rf-chrome-scale:{chrome_scale};"
    )

    # Heatmap palette — emits its own rule block (light + dark variants),
    # so its rules can stand outside the single-rule injection below.
    hm_rules = _heatmap_palette_decl(palette_choice, accent)
    # Background overrides (Settings → Appearance → Background). Blank
    # settings emit nothing, so the theme's own paper stays in charge.
    bg_rules = _background_rules(cfg)

    # tokens.css derives --accent from --rf-accent; inject the latter here.
    # `data-rf-theme` on <html> forces light/dark over the system @media.
    # `data-rf-density` lets theme.css tighten or loosen spacing.
    extras = "<script>(function(){var d=document.documentElement;"
    if theme_pref in ("light", "dark"):
        extras += f"d.dataset.rfTheme='{theme_pref}';"
    extras += f"d.dataset.rfDensity='{density}';"
    # Backdrop palette knobs. `data-rf-sky` is the time of day at render
    # time; scene.css swaps the whole palette off it, so no clock runs in
    # the page. Emitted on every surface (not just the home page) so the
    # attributes are already correct if a screen later grows a backdrop.
    extras += f"d.dataset.rfBackdrop='{_backdrop_mode(cfg)}';"
    extras += f"d.dataset.rfMotion='{_backdrop_motion_mode(cfg)}';"
    extras += f"d.dataset.rfIntensity='{_backdrop_intensity(cfg)}';"
    extras += f"d.dataset.rfSky='{_sky_phase(datetime.datetime.now().hour)}';"
    # The landscape itself, rotated by date unless the user pinned one. Set
    # server-side so the correct scene is up on the first paint; scene-
    # shuffle.js only ever moves it on from here.
    extras += (
        "d.dataset.rfScene='"
        f"{_scene_variant(cfg, datetime.date.today().toordinal())}';"
    )
    if isinstance(context, Reviewer) or _is(context, _PreviewCtx):
        # "native" hands the card's typography/colours back to the note
        # type; reviewer.css gates every content-affecting rule on it.
        # The Browse preview honours it too — the whole point of that pane
        # is that it shows what studying will show.
        if not cfg.get("reviewer_card_styling", True):
            extras += "d.dataset.rfCardstyle='native';"
    if isinstance(context, Reviewer):
        if cfg.get("reviewer_answer_buttons", "intervals") == "native":
            extras += "d.dataset.rfEase='native';"
    extras += "})();</script>"
    # Feature flags read by the page scripts (deck list, reviewer, sidebar).
    # Computed once here (only when it can actually matter — video mode on
    # a surface that gets a backdrop at all) and threaded into both
    # `_js_opts` below and the video injection branch further down, so
    # index.json is read at most once per render rather than twice.
    import json as _json
    video_state: Optional[Dict[str, Any]] = None
    if _backdrop_mode(cfg) == "video" and isinstance(context, (DeckBrowser, Overview)):
        try:
            video_state = _video_state(cfg)
        except Exception:
            video_state = None
    try:
        opts_js = (
            "<script>window.__baOpts="
            + _json.dumps(_js_opts(cfg, video_state)) + ";</script>"
        )
    except Exception:
        opts_js = "<script>window.__baOpts={};</script>"

    web_content.head += (
        f"<style>:root,.night-mode,body{{"
        f"--rf-accent:{accent};"
        f"{serif_decl}{sans_decl}{reviewer_decl}"
        f"}}{hm_rules}{bg_rules}</style>"
        + extras + opts_js
    )
    web_content.css.append(f"{WEB}/tokens.css")

    if isinstance(context, (DeckBrowser, Overview, Reviewer)):
        # theme.css defines the --rf-* design tokens used by reviewer.css
        # (back button, answer divider, progress strip), so the reviewer
        # needs it too — otherwise it falls back to hardcoded dark colors
        # even in light mode. The heavy homepage layout in theme.css is
        # scoped to .ba-home / .ba-over and won't touch the reviewer.
        web_content.css.append(f"{WEB}/theme.css")
        # Cmd-K palette is available on every themed surface (deck browser,
        # overview, reviewer). cmdk.js owns the hotkey and the overlay DOM;
        # the matching Python search backend lives in cmdk.py.
        if cfg.get("cmdk", True):
            web_content.css.append(f"{WEB}/cmdk.css")
            web_content.js.append(f"{WEB}/cmdk.js")
    if isinstance(context, DeckBrowser):
        web_content.css.append(f"{WEB}/heatmap.css")
        web_content.js.append(f"{WEB}/heatmap.js")
        # Shared deck-list component — same code path as the congrats
        # Keep-going list. homedeck.js hides Anki's <table> and renders
        # the list via __adDeckList.render() from window.__baDeckTree.
        web_content.css.append(f"{WEB}/decklist.css")
        web_content.js.append(f"{WEB}/decklist.js")
        web_content.js.append(f"{WEB}/homedeck.js")
        # Embed the full deck tree as JSON so homedeck.js can render the deck
        # list with the EXACT same JS code path as the congrats page (via
        # __adDeckList.render()). Injected regardless of the sidebar setting:
        # the native table is hidden by CSS, so without this payload a
        # sidebar-less home page would show no decks at all.
        try:
            tree_payload = _full_deck_tree_payload()
            web_content.head += (
                "<script>window.__baDeckTree = "
                + _json.dumps(tree_payload) + ";</script>"
            )
        except Exception:
            pass
        # Tag the deck browser's <center> so theme.css can scope the heavy
        # homepage layout to it alone — the Overview shares this stylesheet
        # and must keep its own simple layout (just palette + type).
        # Add `ba-single` when there's only one top-level deck so the hero
        # composition can take over from the (now-hidden) table.
        try:
            klass = "ba-home" + (
                " ba-single" if _hero_mode(cfg) else " ba-multi"
            )
            web_content.body = web_content.body.replace(
                "<center>", f'<center class="{klass}">', 1
            )
        except Exception:
            pass
    if isinstance(context, Overview):
        # Scope Overview's <center> so theme.css can give it its own layout.
        try:
            web_content.body = web_content.body.replace(
                "<center>", '<center class="ba-over">', 1
            )
        except Exception:
            pass
    # Scene backdrop: deck browser and overview only, the same boundary the
    # aurora draws. Motion behind a card you are trying to recall is a
    # distraction, so the reviewer keeps its flat paper. The engine stylesheet
    # is loaded in every mode because it also owns the "off" rules that put
    # the aurora back to a plain static glow.
    if isinstance(context, (DeckBrowser, Overview)):
        web_content.css.append(f"{WEB}/scene.css")
        if _backdrop_mode(cfg) == "scene":
            # The ten scenes are only worth their bytes when one is actually
            # going to be drawn, so they load a step later than the engine.
            web_content.css.append(f"{WEB}/scenes.css")
            # Prepended after the <center> tagging above, so the markup
            # here can never intercept that replacement.
            try:
                web_content.body = _SCENE_HTML + web_content.body
            except Exception:
                pass
            # The only script the backdrop has, and only on these two
            # screens: the reviewer gets no backdrop and no motion at all.
            web_content.js.append(f"{WEB}/scene-shuffle.js")
        elif _backdrop_mode(cfg) == "video":
            # Already computed once above (this is the branch that made
            # that computation happen at all); only re-derive it if that
            # earlier attempt failed, so a transient error here doesn't
            # also take down the opts payload above.
            if video_state is None:
                try:
                    video_state = _video_state(cfg)
                except Exception:
                    video_state = {"filtered": []}
            filtered = video_state["filtered"]
            if filtered:
                web_content.css.append(f"{WEB}/video-backdrop.css")
                try:
                    day_idx = datetime.date.today().toordinal() % len(filtered)
                    initial_url = (
                        f"{USER_FILES}/nature/{_quote(filtered[day_idx]['file'])}"
                    )
                    web_content.body = (
                        _video_html(initial_url) + web_content.body
                    )
                except Exception:
                    pass
                web_content.js.append(f"{WEB}/video-backdrop.js")
            else:
                # No library yet (or index.json is unreadable — already
                # logged once by nature.read_index). Nothing is injected,
                # so the page falls back to whatever scene.css draws for
                # a mode it has no rule for: the plain aurora wash, same
                # as before this feature existed. Logged once, separately
                # from the read-failure case, so choosing "Nature video"
                # with an empty library says so specifically.
                global _VIDEO_EMPTY_WARNED
                if not _VIDEO_EMPTY_WARNED:
                    _VIDEO_EMPTY_WARNED = True
                    try:
                        import sys
                        print(
                            "[anki-design] backdrop is set to \"video\" but "
                            "user_files/nature has no clips; showing the "
                            "default backdrop instead.",
                            file=sys.stderr,
                        )
                    except Exception:
                        pass
    # Sidebar nav — deck browser + overview only. The reviewer gets full
    # focus (no sidebar) so the card area isn't competing with chrome.
    if cfg.get("sidebar_nav", True) and isinstance(
        context, (DeckBrowser, Overview)
    ):
        web_content.css.append(f"{WEB}/logo.css")
        web_content.css.append(f"{WEB}/sidebar.css")
        web_content.css.append(f"{WEB}/deckopts.css")
        web_content.js.append(f"{WEB}/sidebar.js")
        web_content.js.append(f"{WEB}/deckopts.js")
        # Embed the standing data in <head> as a global so sidebar.js reads
        # it synchronously on its first run.
        try:
            import json as _json
            payload = _build_standing_payload()
            web_content.head += (
                "<script>window.__baStandingData = "
                + _json.dumps(payload) + ";</script>"
            )
        except Exception:
            pass
        # Same trick for the rail's active item. The deck browser re-renders
        # behind an open embed's overlay (any collection op will do it), and
        # a freshly built rail defaults to Decks — so without this seed the
        # highlight walks off Browse and onto Decks while Browse is still
        # what's on screen. An eval can't fix it after the fact: it would
        # have to land between the page load and sidebar.js's IIFE.
        try:
            import json as _json
            web_content.head += (
                "<script>window.__baActiveItem = "
                + _json.dumps(_active_embed_cmd() or "decks") + ";</script>"
            )
        except Exception:
            pass
        # Left-rail collapse state (Item 2) — bootstrapped the same way so
        # sidebar.js can stamp the `data-ba-sidebar` attribute on <html>
        # before its first paint instead of flashing expanded-then-collapsed.
        try:
            import json as _json
            web_content.head += (
                "<script>window.__baSidebarCollapsed = "
                + _json.dumps(bool(cfg.get("sidebar_collapsed", False)))
                + ";</script>"
            )
        except Exception:
            pass
        # Per-deck deadline labels, so the gear menu can render
        # "Memorize by Sep 15 — 6d" without a round-trip per deck.
        try:
            import json as _json
            from . import deadlines as _dl
            web_content.head += (
                "<script>window.__baDeadlines = "
                + _json.dumps(_dl.menu_payload()) + ";</script>"
            )
        except Exception:
            pass
    # Floating Settings cog — only when the sidebar is OFF on the homepage.
    no_side = not cfg.get("sidebar_nav", True)
    if no_side and isinstance(context, (DeckBrowser, Overview)):
        web_content.css.append(f"{WEB}/sidebar.css")  # for .ba-cog
        web_content.body = (
            '<button class="ba-cog" onclick="pycmd(\'ba:settings\')" '
            'title="Anki Design settings">⚙</button>' + web_content.body
        )
    # Reviewer header — deck name (with built-in back link) on the left,
    # position counter on the right. Replaces the floating "Decks" button.
    # We also append the ease selector so we can drop the bottom-toolbar
    # webview entirely. Idempotent: if the previous webview content
    # already has our header/ease (e.g., Anki recycles the body for the
    # answer-state render), we skip to avoid stacking duplicates.
    if isinstance(context, Reviewer) and "ba-rv-head" not in web_content.body:
        try:
            head_html = _reviewer_header_html()
        except Exception:
            head_html = ""
        ease_html = ""
        if cfg.get("reviewer_answer_buttons", "intervals") != "native":
            try:
                ease_html = _reviewer_ease_html()
            except Exception:
                ease_html = ""
        hide_answer_html = ""
        if cfg.get("reviewer_hide_answer", True):
            try:
                hide_answer_html = _reviewer_hide_answer_html()
            except Exception:
                hide_answer_html = ""
        prev_html = ""
        if cfg.get("reviewer_prev_card", True):
            try:
                prev_html = _reviewer_prev_html()
            except Exception:
                prev_html = ""
        web_content.body = (
            head_html + web_content.body + ease_html + prev_html + hide_answer_html
        )
    if _is(context, _ToolbarCtx):
        web_content.css.append(f"{WEB}/toolbar.css")
    # Reviewer's bottom bar (Show Answer, Edit, More, answer buttons) lives
    # in `ReviewerBottomBar` — NOT the generic BottomToolbar (that's the
    # deck-browser/overview strip we hide). Style only the reviewer bottom.
    if _is(context, _ReviewerBottomCtx):
        web_content.css.append(f"{WEB}/reviewer-bottom.css")
    # reviewer.css always loads on the reviewer — it owns the back button,
    # answer divider, card chrome, AND the progress strip styling. Gating it
    # on show_progress used to break the back button + card colors when the
    # progress bar was off. show_progress only controls the JS that injects
    # the progress bar element.
    if isinstance(context, Reviewer):
        web_content.css.append(f"{WEB}/reviewer.css")
        if cfg.get("show_progress", True):
            web_content.js.append(f"{WEB}/reviewer.js")
    # Browse tab's rendered-card pane. Same stack as the reviewer — that's
    # the whole promise of the pane: the card looks the way it will look
    # when you study it. browse-preview.css only rescales for the narrower
    # column and adds the Question/Answer switch.
    if _is(context, _PreviewCtx):
        web_content.css.append(f"{WEB}/theme.css")
        web_content.css.append(f"{WEB}/reviewer.css")
        web_content.css.append(f"{WEB}/browse-preview.css")
        web_content.js.append(f"{WEB}/browse-preview.js")
        web_content.head += (
            '<meta name="ba-theme" content="'
            + (theme_pref if theme_pref in ("light", "dark") else "")
            + '">'
        )
    if is_editor:
        # The same Editor class serves Add, Browse and Edit-Current. Add
        # and Browse both get the restyle (they're the two places you
        # actually live in); Edit-Current is left stock so the reviewer's
        # own in-place editing path is untouched.
        #
        # Anki's CSP blocks inline <script> in the editor page, so the mode
        # AND theme are communicated via a meta tag in the head that
        # addcard.js reads (avoiding inline-script CSP). Inline <style> IS
        # allowed and runs synchronously before first paint, so we use it
        # to suppress the open-time FOUC: the body starts at opacity 0 on a
        # paper-colored canvas, then addcard.js fades it back in once the
        # toolbar/field/tags settle. Without this, the user sees Anki's
        # default editor briefly, then ours, then JS reshuffling tags and
        # the gear into place — reads as a stack of flashes.
        em = getattr(context, "editorMode", None)
        editor_mode = ""
        if em == EditorMode.ADD_CARDS and cfg.get("restyle_addcard", True):
            editor_mode = "add"
        elif (
            em == getattr(EditorMode, "BROWSER", None)
            and cfg.get("restyle_browse_editor", True)
        ):
            editor_mode = "browse"
        if editor_mode:
            theme_safe = theme_pref if theme_pref in ("light", "dark") else ""
            # Resolve the actual paper color once, in Python — even when
            # theme_pref is "system" — so the inline <style> can emit a
            # single concrete bg color. If we instead used a CSS media
            # query keyed to prefers-color-scheme, the bg would flip
            # mid-load on system=dark + addon=light (because data-rf-theme
            # isn't set until addcard.js runs), which itself reads as a
            # flash. One concrete color = no flip.
            try:
                from . import addcard as _addcard
                _pal, _ = _addcard._resolve_palette()
                paper_color = _pal["paper"]
            except Exception:
                paper_color = "#f6f3ec"
            web_content.head += (
                "<style>"
                # !important: Anki's editor.css loads after our inline
                # style and sets `body { background-color: var(--bs-body-bg) }`
                # which resolves to a near-white in light mode (the default
                # Bootstrap palette). Without !important Anki wins on source
                # order and the page flashes white before our addcard.css
                # eventually overrides body bg. !important here makes the
                # paper bg stick from first paint.
                f"html,body{{background:{paper_color}!important}}"
                "body{opacity:0;transition:opacity 220ms ease}"
                "html[data-ba-ready] body{opacity:1}"
                "</style>"
                f'<meta name="ba-editor-mode" content="{editor_mode}">'
                f'<meta name="ba-theme" content="{theme_safe}">'
            )
            # addcard.css is the shared editor sheet (its rules key off
            # `html[data-ba-editor]`, i.e. either mode); browse-editor.css
            # carries only the deltas the Browse pane needs.
            web_content.css.append(f"{WEB}/addcard.css")
            if editor_mode == "browse":
                web_content.css.append(f"{WEB}/browse-editor.css")
            web_content.js.append(f"{WEB}/addcard.js")


# --------------------------------------------------------------------------- #
# Integrated action buttons (replace Anki's native bottom strip on the deck
# list). These pycmds are handled by the deck browser's own link handler.
# --------------------------------------------------------------------------- #
# --------------------------------------------------------------------------- #
# Standing computation — used by the sidebar and the single-deck hero.
# --------------------------------------------------------------------------- #


def _standing() -> Dict[str, Any]:
    counts = _counts_by_day()
    shift = _day_shift_seconds()
    today_idx = int((time.time() + shift) // 86400)
    streak = 0
    probe = today_idx if counts.get(today_idx, 0) > 0 else today_idx - 1
    while counts.get(probe, 0) > 0:
        streak += 1
        probe -= 1
    out: Dict[str, Any] = {
        "today": counts.get(today_idx, 0),
        "total": sum(counts.values()),
        "streak": streak,
        "new": None,
        "learn": None,
        "due": None,
    }
    try:
        tree = mw.col.sched.deck_due_tree()
        n = lr = rv = 0
        for c in getattr(tree, "children", []):
            n += int(getattr(c, "new_count", 0) or 0)
            lr += int(getattr(c, "learn_count", 0) or 0)
            rv += int(getattr(c, "review_count", 0) or 0)
        out["new"], out["learn"], out["due"] = n, lr, rv
    except Exception:
        pass
    return out




# --------------------------------------------------------------------------- #
# Heatmap
# --------------------------------------------------------------------------- #
def _rollover_hour() -> int:
    try:
        return int(mw.col.get_preferences().scheduling.rollover)
    except Exception:
        try:
            return int(mw.col.conf.get("rollover", 4))
        except Exception:
            return 4


def _day_shift_seconds() -> int:
    """Offset so a `revlog.id` (ms, UTC) divided by a day lands on the
    correct local day, accounting for the user's day rollover hour."""
    if time.localtime().tm_isdst and time.daylight:
        offset = -time.altzone
    else:
        offset = -time.timezone
    return offset - _rollover_hour() * 3600


def _counts_by_day() -> Dict[int, int]:
    shift = _day_shift_seconds()
    rows = mw.col.db.all(
        "select cast((id/1000 + ?) / 86400 as int) as d, count() "
        "from revlog group by d",
        shift,
    )
    return {int(d): int(n) for d, n in rows}


_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
           "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
_WEEKDAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]

# Gutter glyphs for the heatmap's seven rows, Sunday-first like _WEEKDAYS.
# Spelled out instead of sliced off _WEEKDAYS because "Tue"[0] and "Thu"[0]
# are both "T": running the whole Mon-Fri block makes that pair readable by
# position, but only as long as the collision is a decision rather than a
# side effect of slicing. Weekends stay blank so their rows keep full height
# and weekend study still shows up in the grid.
_WEEKDAY_GLYPHS = ["", "M", "T", "W", "T", "F", ""]


def _heatmap_level_fn(counts: Dict[int, int]):
    """Pick a bucketing algorithm based on the shape of the data, so the
    four shades stay visually varied across very different histories
    (uniform habit, gentle ramp, occasional cram days, long-tail outliers).

    The chosen strategy depends on:
      • # of distinct nonzero values (tiny histories get a direct mapping)
      • peak / median ratio (skewness)
      • peak / p95 ratio (outlier severity)
    """
    nonzero = sorted(c for c in counts.values() if c > 0)
    if not nonzero:
        return lambda n: 0

    uniq = sorted(set(nonzero))
    # Few distinct values → map each one directly to a shade. Avoids the
    # awkwardness of bucketing 4 values into 4 buckets via arithmetic.
    if len(uniq) <= 4:
        direct = {v: i + 1 for i, v in enumerate(uniq)}
        return lambda n: direct.get(n, 0) if n > 0 else 0

    def pct(p: float) -> int:
        # Nearest-rank percentile; safe for short lists.
        i = max(0, min(len(nonzero) - 1, int(round(p * (len(nonzero) - 1)))))
        return nonzero[i]

    peak = nonzero[-1]
    median = pct(0.5) or 1
    p95 = pct(0.95) or 1
    skew = peak / median
    outlier = peak / p95

    # Outlier-dominated (a few cram days dwarf the rest): quantile buckets
    # so the outliers can't squash everyone else into L1.
    if outlier > 3 and len(nonzero) >= 8:
        q1, q2, q3 = pct(0.25), pct(0.5), pct(0.75)
        def level_q(n: int) -> int:
            if n <= 0: return 0
            if n <= q1: return 1
            if n <= q2: return 2
            if n <= q3: return 3
            return 4
        return level_q

    # Heavy skew without runaway outliers: log compresses the long tail.
    if skew > 10:
        log_peak = math.log1p(peak) or 1.0
        return lambda n: 0 if n <= 0 else min(4, 1 + int(math.log1p(n) * 4 / log_peak))

    # Moderate skew: sqrt is gentler than log, keeps mid-range readable.
    if skew > 3:
        sqrt_peak = math.sqrt(peak) or 1.0
        return lambda n: 0 if n <= 0 else min(4, 1 + int(math.sqrt(n) * 4 / sqrt_peak))

    # Tight distribution: plain linear is enough.
    return lambda n: 0 if n <= 0 else min(4, 1 + int(n * 4 / (peak + 0.0001)))


# Empty columns of lead-in room to the left of a young history, so the grid
# never looks flush-clipped against the edge while it's still small. Kept to
# a single week — any more and a brand-new user's 1-day-old grid would be
# mostly dead space again, the exact thing this whole feature exists to fix.
_HEATMAP_LEAD_IN_WEEKS = 1


def _heatmap_dow(idx: int) -> int:  # 0 = Sunday .. 6 = Saturday
    return (idx + 4) % 7


def _heatmap_window(
    today_idx: int,
    earliest_idx: Optional[int],
    weeks: int,
    lead_in_weeks: int = _HEATMAP_LEAD_IN_WEEKS,
) -> Tuple[int, int, int]:
    """Pick the Sunday-aligned window to render: (grid_start, start_idx,
    columns).

    The grid grows with the user's history instead of always paying out a
    fixed `weeks`-wide window:
      * `grid_start` is anchored to the user's first-ever review (Sunday of
        that week), pulled back by `lead_in_weeks` so it doesn't look
        clipped, then clamped so the grid never exceeds `weeks` columns —
        past that cap it becomes a rolling window of the most recent
        `weeks` weeks, same as a long-time user sees today.
      * `start_idx` is the first day that may show real data; days before it
        (but still inside `grid_start..today_idx`, i.e. the lead-in) render
        as blank placeholder cells rather than data.
      * `columns` is exact: at the cap it works out to precisely `weeks`
        (verified in tests/test_heatmap_window.py) because both boundaries
        are anchored to `today`'s own Sunday, not to each other.

    No reviews yet (`earliest_idx is None`) renders "this week" — a couple
    of columns, not an empty year.
    """
    today_sunday = today_idx - _heatmap_dow(today_idx)
    cap_start = today_sunday - (weeks - 1) * 7  # oldest Sunday the cap allows

    if earliest_idx is None:
        earliest_idx = today_idx  # no history: show just the current week

    earliest_sunday = earliest_idx - _heatmap_dow(earliest_idx)
    wanted_start = earliest_sunday - lead_in_weeks * 7
    grid_start = max(wanted_start, cap_start)  # never wider than the cap
    start_idx = max(earliest_idx, cap_start)

    columns = (today_idx - grid_start) // 7 + 1
    return grid_start, start_idx, columns


def build_heatmap_html(weeks: int = 53) -> str:
    col = mw.col
    if not col:
        return ""

    counts = _counts_by_day()
    shift = _day_shift_seconds()
    today_idx = int((time.time() + shift) // 86400)
    today_date = datetime.date.today()

    def date_for(idx: int) -> datetime.date:
        return today_date - datetime.timedelta(days=today_idx - idx)

    dow = _heatmap_dow

    earliest = min(counts) if counts else None
    grid_start, start_idx, columns = _heatmap_window(today_idx, earliest, weeks)

    nonzero = [c for c in counts.values() if c > 0]
    peak = max(nonzero) if nonzero else 1
    level = _heatmap_level_fn(counts)

    cells = []
    month_spans = []  # [label, span_in_columns]
    prev_month = None
    for w in range(columns):
        cd = date_for(grid_start + w * 7)  # the column's Sunday
        if cd.month != prev_month:
            month_spans.append([_MONTHS[cd.month - 1], 1])
            prev_month = cd.month
        else:
            month_spans[-1][1] += 1

        col_cells = []
        for r in range(7):
            idx = grid_start + w * 7 + r
            if idx < start_idx or idx > today_idx:
                col_cells.append('<div class="rf-hm-cell rf-hm-empty"></div>')
                continue
            n = counts.get(idx, 0)
            d = date_for(idx)
            human = f"{_WEEKDAYS[dow(idx)]}, {d.day} {_MONTHS[d.month - 1]} {d.year}"
            if idx == today_idx:
                rel = "Today"
            elif idx == today_idx - 1:
                rel = "Yesterday"
            else:
                rel = ""
            is_peak = "1" if (n == peak and peak >= 8) else "0"
            col_cells.append(
                f'<div class="rf-hm-cell rf-hm-l{level(n)}" '
                f'data-count="{n}" data-human="{human}" '
                f'data-rel="{rel}" data-peak="{is_peak}"></div>'
            )
        cells.append('<div class="rf-hm-col">' + "".join(col_cells) + "</div>")

    # Only label months wide enough to fit the text without colliding —
    # EXCEPT the last (rightmost) span, which is always labelled even when
    # narrow. That span is always the current, in-progress month (it's the
    # one containing `today_idx`), and it starts out just 1-3 columns wide
    # for most of every month — hiding it left the previous month's label
    # as the last visible one, right next to this month's (unlabelled)
    # cells, which read as if that activity belonged to last month.
    last_span_idx = len(month_spans) - 1
    months_html = "".join(
        f'<span class="rf-hm-mon" style="width:{span * 14}px">'
        f'{label if (span >= 4 or i == last_span_idx) else ""}</span>'
        for i, (label, span) in enumerate(month_spans)
    )
    # Every weekday gets its letter, not just the old Mon/Wed/Fri cadence.
    # All seven spans are still emitted: the blank weekend ones are the
    # spacers that keep letter rows lined up with cell rows.
    weekdays_html = "".join(
        f'<span class="rf-hm-wd">{g}</span>' for g in _WEEKDAY_GLYPHS
    )

    total = sum(counts.values())
    streak = 0
    probe = today_idx if counts.get(today_idx, 0) > 0 else today_idx - 1
    while counts.get(probe, 0) > 0:
        streak += 1
        probe -= 1
    today_n = counts.get(today_idx, 0)

    return f"""
    <div class="rf-heatmap">
      <div class="rf-hm-head">
        <span class="rf-hm-title">Review activity</span>
        <span class="rf-hm-stats">
          <b>{today_n}</b> today &nbsp;·&nbsp;
          <b>{streak}</b> day streak &nbsp;·&nbsp;
          <b>{total}</b> total
        </span>
      </div>
      <div class="rf-hm-body">
        <div class="rf-hm-wds">
          <span class="rf-hm-mon-spacer"></span>
          {weekdays_html}
        </div>
        <div class="rf-hm-scroll">
          <div class="rf-hm-inner">
            <div class="rf-hm-months">{months_html}</div>
            <div class="rf-hm-grid">{''.join(cells)}</div>
          </div>
        </div>
      </div>
    </div>
    """


# Optionally force Anki's own deck-browser tree to render with every node
# expanded (Settings → Deck list → "Sub-decks on startup: Expanded"). We patch
# `_render_deck_node` (not the persisted collapsed state) so the user's saved
# collapse flags stay intact in the backend; we just ignore them in this view.
# The default ("Remember") leaves Anki's persisted state alone.
def _patch_deck_tree_always_expanded() -> None:
    try:
        if getattr(DeckBrowser, "_ad_expand_patched", False):
            return
        _orig = DeckBrowser._render_deck_node

        def _patched(self, node, ctx):
            if _config().get("deck_tree_startup", "remember") != "expanded":
                return _orig(self, node, ctx)
            try:
                # Walk and flatten any persisted collapse so all rows render.
                # Per-call: only mutates the in-memory tree node Anki built
                # for this render. The next render rebuilds from the backend.
                stack = [node]
                while stack:
                    n = stack.pop()
                    try:
                        n.collapsed = False
                    except Exception:
                        pass
                    stack.extend(getattr(n, "children", []) or [])
            except Exception:
                pass
            return _orig(self, node, ctx)

        DeckBrowser._render_deck_node = _patched  # type: ignore[assignment]
        DeckBrowser._ad_expand_patched = True  # type: ignore[attr-defined]
    except Exception:
        pass


def on_deck_browser_will_render_content(
    deck_browser: DeckBrowser, content: DeckBrowserContent
) -> None:
    cfg = _config()
    heatmap = ""
    if cfg.get("show_heatmap", True):
        try:
            heatmap = build_heatmap_html(int(cfg.get("heatmap_weeks", 53)))
        except Exception as e:
            heatmap = f"<!-- anki-design heatmap error: {e} -->"
    # Single-deck hero replaces the top-level row (CSS hides the table in
    # this mode). Sub-decks of that one deck still render as a list under
    # the hero — homedeck.js takes care of that.
    hero = ""
    if _hero_mode(cfg):
        try:
            hero = _single_deck_hero()
        except Exception:
            pass
    # Pulse strip (today panel + streak) leads the practice section. Same
    # shape in single-deck and multi-deck because it sits inside
    # .ba-practice (which is the same in both modes). Each half has its
    # own toggle in Settings.
    pulse = _pulse_html(
        show_today=bool(cfg.get("show_today", True)),
        show_streak=bool(cfg.get("show_streak", True)),
    )
    practice_inner = pulse + heatmap
    practice = (
        f'<section class="ba-practice">{practice_inner}</section>'
        if practice_inner else ""
    )
    content.stats = hero + content.stats + practice


# --------------------------------------------------------------------------- #
# Native bottom strip: hide on the deck list (actions moved into the page),
# keep it everywhere else so the reviewer answer buttons / overview "Study"
# button are untouched.
# --------------------------------------------------------------------------- #
def _set_bottom_visible(visible: bool) -> None:
    bw = getattr(mw, "bottomWeb", None)
    if bw is None:
        return
    try:
        bw.setVisible(visible)
    except Exception:
        pass


def _update_title() -> None:
    # Anki sets "<profile> - Anki" late in profile load; collapse it to a
    # clean "Anki+" (we re-assert it after render so ours wins that race).
    try:
        mw.setWindowTitle("Anki+")
    except Exception:
        pass


def _mark_toolbar_state(state: Optional[str] = None) -> None:
    """Tag the toolbar <body> with the current screen so toolbar.css can
    highlight the active section (e.g. Decks on the deck list). The toolbar
    DOM survives state changes — only the content webview swaps — so a single
    eval sticks. Best-effort and fully guarded."""
    tw = getattr(mw, "toolbarWeb", None)
    if tw is None:
        return
    raw = state if state is not None else getattr(mw, "state", "")
    safe = "".join(ch for ch in str(raw) if ch.isalnum())
    try:
        tw.eval(
            "document.body && document.body.setAttribute("
            "'data-rf-state','%s');" % safe
        )
    except Exception:
        pass


def on_state_did_change(new_state: str, old_state: str) -> None:
    cfg = _config()
    if new_state != "review":
        # Backstop for any route out of a queue that doesn't tear the
        # reviewer down (`reviewer_will_end` handles the common one, and
        # earlier — before the new state paints). Normally there is nothing
        # left to release, so the refresh below never runs.
        if release_study_queues():
            try:
                if new_state == "deckBrowser":
                    mw.deckBrowser.refresh()
                elif new_state == "overview":
                    mw.overview.refresh()
            except Exception:
                pass
    hide_decks = cfg.get("hide_bottom_on_decks", True)
    hide_over = cfg.get("hide_bottom_on_overview", True)
    if new_state == "deckBrowser":
        _set_bottom_visible(not hide_decks)
    elif new_state == "overview":
        _set_bottom_visible(not hide_over)
    elif new_state == "review":
        # We render our own ease selector inside the reviewer webview, so
        # the native bottom toolbar is redundant — unless the user opted
        # back into Anki's own answer buttons.
        native_ease = cfg.get("reviewer_answer_buttons", "intervals") == "native"
        _set_bottom_visible(native_ease)
    else:
        _set_bottom_visible(True)
    _update_title()
    _mark_toolbar_state(new_state)
    _apply_chrome()
    _mark_sidebar_active(new_state)
    _push_sidebar_standing()
    # Re-check pending sync on every navigation so the sidebar dot reflects
    # current state after reviewing/adding/etc., not just on deck-browser
    # render.
    _refresh_sync_status()


def _post_render_fixups() -> None:
    if _config().get("hide_bottom_on_decks", True):
        _set_bottom_visible(False)
    _update_title()
    _mark_toolbar_state()
    _apply_chrome()
    _push_sidebar_standing()
    _refresh_sync_status()


# --------------------------------------------------------------------------- #
# Sidebar nav — hide Anki's top toolbar webview and route `ba:*` pycmds.
# --------------------------------------------------------------------------- #
def _sidebar_on() -> bool:
    return bool(_config().get("sidebar_nav", True))


def _set_top_toolbar_visible(visible: bool) -> None:
    tw = getattr(mw, "toolbarWeb", None)
    if tw is None:
        return
    try:
        tw.setVisible(visible)
    except Exception:
        pass


def _apply_chrome() -> None:
    """Hide Anki's top toolbar webview when the sidebar is on. Anki re-shows
    it on state transitions, so we re-hide after each render."""
    _set_top_toolbar_visible(not _sidebar_on())


# Which rail item each inline embed owns while its overlay is up.
#
# addcard_embed is deliberately absent: there is no "add" rail item any
# more (Add lives inside Browse — see _open_add below). Add now normally
# renders *inside* Browse's own CARD panel (browse_embed._open_add_in_panel),
# which never touches addcard_embed._state at all — it reparents just the
# AddCards dialog's central widget into browse_embed's own QStackedWidget.
# The old stacked-overlay path (browse_embed._open_add_in_browse, used as a
# fallback and by the dev screenshot harness) does open addcard_embed's own
# overlay on top of browse_embed's. Either way, if addcard_embed were listed
# here it would wrongly try to highlight a rail item that no longer exists
# instead of falling through to "browse" below, which is what should be lit
# while Add is showing (panel or fallback overlay alike).
#
# Among the embeds listed here, only one is ever open at a time, so the
# order doesn't matter.
_EMBED_RAIL_CMDS = (
    ("browse_embed", "browse"),
    ("stats_embed", "stats"),
    ("settings_embed", "settings"),
)


def _active_embed_cmd() -> str:
    """The rail item an open embed owns, or "" when none is open."""
    from importlib import import_module

    for mod_name, cmd in _EMBED_RAIL_CMDS:
        try:
            mod = import_module("." + mod_name, __name__)
            if mod._state.get("overlay") is not None:
                return cmd
        except Exception:
            continue
    return ""


def _mark_sidebar_active(state: Optional[str] = None) -> None:
    """Tell every themed webview's sidebar which item is current. Cheap and
    safe — no-ops if the sidebar JS hasn't initialised yet.

    An open embed outranks `mw.state`, which is a poor witness while one is
    up: an embed is an overlay *over* the deck browser, so mw.state reads
    "deckBrowser" the entire time Browse is on screen. And the deck browser
    re-renders behind that overlay for all sorts of reasons — expanding a
    deck in Browse's own sidebar tree writes the collapse flag, which is a
    collection op, which refreshes the deck browser — so every one of those
    renders used to walk the highlight back to Decks underneath the user.
    """
    raw = state if state is not None else getattr(mw, "state", "")
    cmd = _active_embed_cmd() or {
        "deckBrowser": "decks", "overview": "decks", "review": "decks",
    }.get(str(raw), "")
    if not cmd:
        return
    js = "window.__baSetActive && window.__baSetActive('%s');" % cmd
    for attr in ("web",):
        w = getattr(mw, attr, None)
        if w is not None:
            try:
                w.eval(js)
            except Exception:
                pass


def _open_settings() -> None:
    """Open the Anki Design settings — embedded inline if possible,
    standalone Preferences dialog otherwise."""
    # Tear down any other embed that might be up; only one inline view
    # at a time.
    for mod in ("addcard_embed", "browse_embed", "stats_embed"):
        try:
            from importlib import import_module
            import_module("." + mod, __name__).close_inline()
        except Exception:
            pass
    if _embed_enabled("embed_settings"):
        try:
            from . import settings_embed
            settings_embed.open_inline(mw)
            return
        except Exception:
            pass
        finally:
            _push_video_occlusion()
    # Standalone Preferences dialog with the Anki Design tab selected — the
    # fallback path, and the normal path when inline settings are off.
    try:
        from .settings import open_settings
        open_settings(mw)
    except Exception as e:
        try:
            from aqt.utils import showWarning
            showWarning(f"Anki Design settings: {e}")
        except Exception:
            pass
    _push_video_occlusion()


def _on_js_message(handled, message, context):
    """Dispatch `ba:<cmd>` pycmds from our sidebar/settings. Filter hook:
    return (True, None) when we handle it."""
    if not isinstance(message, str):
        return handled
    # Anki's deck browser emits `open:<did>` when a deck is clicked, which
    # normally lands on the intermediate Overview page. Skip that and go
    # straight into studying — same target as the single-deck hero.
    if message.startswith("open:") and isinstance(context, DeckBrowser):
        if not _config().get("skip_overview", True):
            return handled  # let Anki open its overview page as usual
        tail = message.split(":", 1)[1]
        if tail.isdigit():
            try:
                _start_studying(int(tail))
            except Exception:
                return handled
            return (True, None)
        return handled
    if not message.startswith("ba:"):
        return handled
    cmd = message[3:]
    try:
        if cmd.startswith("cmdk-search:"):
            try:
                from . import cmdk as _cmdk
                _cmdk.handle_search(cmd[len("cmdk-search:"):])
            except Exception:
                pass
            return (True, None)
        if cmd.startswith("cmdk-do:"):
            try:
                from . import cmdk as _cmdk
                _cmdk.handle_do(cmd[len("cmdk-do:"):])
            except Exception:
                pass
            return (True, None)
        if cmd == "cmdk-open":
            # open_from_outside picks the right host: reviewer.web during
            # review, the cmdk_overlay when an embed is active (so the
            # palette floats above it), mw.web otherwise.
            if not _config().get("cmdk", True):
                return (True, None)
            try:
                from . import cmdk as _cmdk
                _cmdk.open_from_outside("")
            except Exception:
                pass
            return (True, None)
        if cmd == "cmdk-closed":
            # The palette JS fires this on every close (Esc, click outside,
            # item commit). Hide the cmdk_overlay frame if it was the host;
            # a no-op when the palette was hosted in mw.web/reviewer.web.
            try:
                from . import cmdk_overlay
                cmdk_overlay.close()
            except Exception:
                pass
            return (True, None)
        if cmd == "embed-ready":
            # addcard.js fires this from reveal() once the editor body is
            # ready to fade in. We drop the anti-flash curtain that
            # open_inline put up, so the user transitions straight from
            # paper to faded-in editor with no intermediate state.
            try:
                from . import addcard_embed
                addcard_embed.drop_curtain()
            except Exception:
                pass
            return (True, None)
        if cmd.startswith("edit-save:"):
            payload = cmd[len("edit-save:"):]
            try:
                from . import editreviewer
                editreviewer.handle_edit_save(payload)
            except Exception:
                pass
            return (True, None)
        if cmd.startswith("edit-full"):
            # Either "edit-full" (no payload) or "edit-full:<json>".
            payload = ""
            if cmd.startswith("edit-full:"):
                payload = cmd[len("edit-full:"):]
            try:
                from . import editreviewer
                editreviewer.handle_edit_full(payload)
            except Exception:
                pass
            return (True, None)
        if cmd.startswith("edit-state:"):
            # JS enter/exit signal — toggles whether the reviewer's Qt
            # shortcuts are active. While editing they're disabled so the
            # user can type normally (including letters like e/m, plus
            # ⌘+Backspace which Anki normally maps to delete-note).
            on = cmd[len("edit-state:"):] == "on"
            try:
                from . import editreviewer
                editreviewer.set_edit_active(on)
            except Exception:
                pass
            return (True, None)
        if cmd == "decks":
            # If we're in any embedded view (Add, Browse, Stats,
            # Settings), close it first so the deck browser becomes
            # visible again. The embeds are overlays painted *on top
            # of* the deck browser — mw.state stays "deckBrowser" the
            # whole time. Calling moveToState while already in
            # deckBrowser state triggers a full re-render of the deck
            # list HTML (deckBrowser.show()), which reads as a
            # disorienting flash when switching tabs. Only move state
            # when we're actually somewhere else (reviewer, overview,
            # etc.).
            for mod in (
                "addcard_embed", "browse_embed", "stats_embed", "settings_embed",
            ):
                try:
                    from importlib import import_module
                    import_module("." + mod, __name__).close_inline()
                except Exception:
                    pass
            _push_video_occlusion()
            if getattr(mw, "state", None) != "deckBrowser":
                mw.moveToState("deckBrowser")
        elif cmd == "browse":
            for mod in ("addcard_embed", "stats_embed", "settings_embed"):
                try:
                    from importlib import import_module
                    import_module("." + mod, __name__).close_inline()
                except Exception:
                    pass
            _open_browse()
        elif cmd == "stats":
            for mod in ("addcard_embed", "browse_embed", "settings_embed"):
                try:
                    from importlib import import_module
                    import_module("." + mod, __name__).close_inline()
                except Exception:
                    pass
            _open_stats()
        elif cmd == "sync":
            mw.on_sync_button_clicked()
        elif cmd == "settings":
            _open_settings()
        elif cmd.startswith("sidebar-collapse:"):
            # Left-rail collapse toggle (Item 2). Persist to the add-on's
            # own config (same key/reader _config() uses) and, if any
            # *_embed.py overlay is currently open, reposition it to the
            # new sidebar width immediately rather than waiting for the
            # next window resize to correct the gap it sits in.
            val = cmd[len("sidebar-collapse:"):]
            collapsed = val not in ("0", "false", "")
            try:
                cfg2 = mw.addonManager.getConfig(__name__) or {}
                cfg2["sidebar_collapsed"] = collapsed
                mw.addonManager.writeConfig(__name__, cfg2)
            except Exception:
                pass
            for mod in (
                "addcard_embed", "browse_embed", "settings_embed", "stats_embed",
            ):
                try:
                    from importlib import import_module
                    _m = import_module("." + mod, __name__)
                    _reflow = getattr(_m, "reflow", None)
                    if _reflow is not None:
                        _reflow()
                except Exception:
                    pass
        elif cmd == "website":
            try:
                from aqt.utils import openLink
                openLink("https://anki.design")
            except Exception:
                return handled
        elif cmd == "undo":
            try:
                mw.undo()
            except Exception:
                return handled
        elif cmd == "hide-answer":
            # Bottom-left "Hide Answer" button / H key: back the same card
            # out to its question side without grading it — no scheduling
            # call is made, so this can't cost the user a review. A no-op
            # unless we're actually on the answer side (covers a stray
            # click/keystroke racing a card change).
            try:
                if not _config().get("reviewer_hide_answer", True):
                    return handled
                rv = getattr(mw, "reviewer", None)
                if rv is None or getattr(rv, "card", None) is None:
                    return handled
                if getattr(rv, "state", "") != "answer":
                    return handled
                rv._showQuestion()
            except Exception:
                return handled
        elif cmd == "prev-card":
            # Bottom-left "‹ Previous" button / P key: step back to the card
            # you just answered so you can grade it again. This is Anki's own
            # review undo — mw.undo() pops the AnswerCard entry off the undo
            # queue, which restores that card's pre-answer scheduling in full
            # (ivl, due, factor, reps, lapses, and its revlog row) and puts it
            # back at the head of the queue, where the reviewer re-shows it in
            # its *question* state for a fresh grade. Guarded by _can_prev()
            # so we never fire when the pending undo is something other than
            # an answer (adding a note, switching decks, an empty stack) —
            # undoing those from here would be a nasty surprise.
            try:
                if not _can_prev():
                    return handled
                mw.undo()
            except Exception:
                return handled
        elif cmd == "flag-cycle":
            # Cycle the current card's flag 0 → 1 → 2 → 3 → 4 → 0.
            try:
                rv = getattr(mw, "reviewer", None)
                if rv is None or getattr(rv, "card", None) is None:
                    return handled
                card = rv.card
                cur = int(card.user_flag())
                nxt = (cur + 1) % 5  # 0..4
                card.set_user_flag(nxt)
                # The card needs to be saved so the flag persists; the
                # reviewer's _showQuestion will re-pull on next render.
                try:
                    mw.col.update_card(card)
                except Exception:
                    pass
                _push_progress()
            except Exception:
                return handled
        elif cmd == "create":
            _focus_inline_new_deck()
        elif cmd == "create-fallback":
            _open_new_deck_dialog()
        elif cmd.startswith("create:"):
            _create_deck_inline(cmd.split(":", 1)[1])
        elif cmd == "import":
            mw.onImport()
        elif cmd.startswith("study:"):
            tail = cmd.split(":", 1)[1]
            if tail.isdigit():
                _start_studying(int(tail))
            else:
                return handled
        elif cmd.startswith("study-state:"):
            # Sidebar totals (Due/New/Learning) as cross-deck launch buttons.
            _study_state(cmd.split(":", 1)[1])
        elif cmd == "prefs":
            # Same destination as ba:settings — route through the embed.
            try:
                _open_settings()
            except Exception:
                return handled
        elif cmd.startswith("deck:"):
            # Items from our custom deck-options menu (web/deckopts.js).
            # Format: deck:<action>:<did>[:<arg>]
            parts = cmd.split(":", 3)
            if len(parts) < 3 or not parts[2].isdigit():
                return handled
            action = parts[1]
            did = int(parts[2])
            extra = parts[3] if len(parts) >= 4 else None
            db = getattr(mw, "deckBrowser", None)
            try:
                if action == "rename" and db is not None:
                    db._rename(did)  # type: ignore[attr-defined]
                elif action == "rename-to" and extra is not None:
                    _rename_deck_inline(did, extra)
                elif action == "collapse":
                    _set_deck_collapsed(did, extra)
                elif action == "reparent" and extra is not None:
                    _reparent_deck(did, extra)
                elif action == "options":
                    try:
                        from aqt.deckoptions import display_options_for_deck_id
                        from anki.decks import DeckId
                        display_options_for_deck_id(DeckId(did))
                    except Exception:
                        if db is not None:
                            db._options(did)  # type: ignore[attr-defined]
                elif action == "export":
                    # Anki's deck-browser exporter dispatches on did.
                    try:
                        from aqt.import_export.exporting import ExportDialog
                        ExportDialog(mw, did=did)
                    except Exception:
                        if db is not None:
                            db._export(did)  # type: ignore[attr-defined]
                elif action == "rebuild" and db is not None:
                    db._rebuild(did)  # type: ignore[attr-defined]
                elif action == "empty" and db is not None:
                    db._empty(did)  # type: ignore[attr-defined]
                elif action == "restudy":
                    try:
                        from aqt import restudy
                    except ImportError:
                        restudy = None
                    if restudy is not None:
                        restudy.RestudyDialog.fetch_and_show(mw, did)
                    else:
                        # Stock Anki — the fork's dialog isn't installed.
                        # Fall back to the native Options dialog (same path
                        # as the "options" action) rather than doing nothing.
                        try:
                            from aqt.deckoptions import display_options_for_deck_id
                            from anki.decks import DeckId
                            display_options_for_deck_id(DeckId(did))
                        except Exception:
                            if db is not None:
                                db._options(did)  # type: ignore[attr-defined]
                            else:
                                from aqt.utils import tooltip
                                tooltip("Restudy isn't available in this version of Anki.")
                elif action == "randomize":
                    # In-place shuffle of this deck's new-card positions —
                    # the same thing the reviewer's More menu does, aimed
                    # at the deck whose gear was clicked rather than the
                    # one being studied.
                    from . import reviewer_menu as _rm
                    _rm.randomize_deck(did)
                elif action == "subsection":
                    from . import subsections as _subs
                    if _subs.enabled():
                        _subs.show_dialog(did)
                elif action == "deadline":
                    from . import deadlines as _dl
                    if _dl.enabled():
                        _dl.show_dialog(did)
                elif action == "delete" and db is not None:
                    db._delete(did)  # type: ignore[attr-defined]
                else:
                    return handled
            except Exception:
                return handled
        else:
            return handled
    except Exception:
        return handled
    return (True, None)


def _focus_inline_new_deck() -> None:
    """Focus the inline create input in the sidebar. Falls back to Anki's
    native dialog if the sidebar isn't reachable (other page, JS not loaded)."""
    web = getattr(mw, "web", None)
    state = getattr(mw, "state", "")
    if web is not None and state in ("deckBrowser", "overview"):
        try:
            web.eval(
                "if (window.__baFocusNewDeck) window.__baFocusNewDeck();"
                "else pycmd('ba:create-fallback');"
            )
            return
        except Exception:
            pass
    _open_new_deck_dialog()


def _open_new_deck_dialog() -> None:
    """Last-resort fallback: open Anki's native New Deck dialog."""
    try:
        from aqt.operations.deck import add_deck_dialog
        add_deck_dialog(parent=mw)
    except Exception:
        try:
            mw.deckBrowser._on_create()  # type: ignore[attr-defined]
        except Exception:
            pass


def _create_deck_inline(name: str) -> None:
    """Create a deck by name from the inline sidebar input. `decks.id(name,
    create=True)` is idempotent — a duplicate name returns the existing id,
    so no pre-check is needed."""
    name = (name or "").strip()
    if not name:
        return
    try:
        mw.col.decks.id(name, create=True)
    except Exception:
        return
    try:
        mw.reset()
    except Exception:
        pass


def _set_deck_collapsed(did: int, state: Any) -> None:
    """Write a deck's persisted (and synced) collapse flag — exactly what
    Anki's native "+/-" toggle stores — WITHOUT re-rendering the page. The
    JS list has already hidden/shown the rows and tells us the resulting
    state ("1" collapsed / "0" open), so rapid toggles can't drift: each
    write is absolute, never a read-modify-write. The next render reads the
    flag back from the backend, so the state survives restarts and syncs."""
    try:
        from anki.decks import DeckId
        deck = mw.col.decks.get(DeckId(did))
        if not deck:
            return
        s = str(state if state is not None else "").strip()
        if s in ("1", "0"):
            collapsed = s == "1"
        else:  # no explicit state: plain toggle
            collapsed = not bool(deck.get("collapsed", False))
        try:
            from anki.decks import DeckCollapseScope
            from aqt.operations.deck import set_deck_collapsed
            set_deck_collapsed(
                parent=mw, deck_id=DeckId(did), collapsed=collapsed,
                scope=DeckCollapseScope.REVIEWER,
            ).run_in_background(initiator=getattr(mw, "deckBrowser", None))
        except Exception:
            mw.col.decks.set_collapsed(
                DeckId(did), collapsed, 1  # 1 == DeckCollapseScope.REVIEWER
            )
    except Exception:
        pass


def _reparent_deck(did: int, target_raw: Any) -> None:
    """Move a deck under another deck — or to the top level with target 0.
    Drives Anki's own reparent op (the one its native drag-and-drop uses),
    so naming, merging and undo behave exactly as they do in stock Anki."""
    try:
        target = int(str(target_raw).strip() or "0")
    except Exception:
        return
    if target == did:
        return
    try:
        from anki.decks import DeckId
        from aqt.operations.deck import reparent_decks

        def _after(_out: Any) -> None:
            try:
                if getattr(mw, "state", "") == "deckBrowser":
                    mw.deckBrowser.refresh()
            except Exception:
                pass

        reparent_decks(
            parent=mw, deck_ids=[DeckId(did)], new_parent=DeckId(target),
        ).success(_after).run_in_background()
    except Exception:
        pass


def _rename_deck_inline(did: int, encoded_leaf: str) -> None:
    """Apply an inline rename from the home page row. The user edits only
    the leaf segment, so we preserve any parent prefix and replace the
    last `::`-separated component with what they typed.

    If they typed `::` themselves, that path becomes the new tail — so
    `A::B::C` edited to `X::Y` becomes `A::B::X::Y`. Empty/unchanged
    inputs are no-ops."""
    try:
        from urllib.parse import unquote
        new_leaf = unquote(encoded_leaf or "").strip()
    except Exception:
        return
    if not new_leaf:
        return
    try:
        current = mw.col.decks.name(did)
    except Exception:
        return
    if not current:
        return
    parent_parts = current.split("::")[:-1]
    new_full = "::".join(parent_parts + [new_leaf]) if parent_parts else new_leaf
    if new_full == current:
        return
    try:
        from aqt.operations.deck import rename_deck
        from anki.decks import DeckId
        rename_deck(
            parent=mw, deck_id=DeckId(did), new_name=new_full,
        ).run_in_background()
    except Exception:
        pass


_STUDY_STATE_SPECS = {
    # kind -> (deck name, cross-deck search, order attr on SearchTerm.Order)
    # Mirrors custom study's cram kinds (rslib custom_study.rs): due/new/
    # learning study through a rescheduling filtered deck, so answers count
    # exactly as normal reviews would.
    "due": ("Study: Due", "is:due -is:suspended", "DUE"),
    "new": ("Study: New", "is:new -is:suspended", "ADDED"),
    "learn": ("Study: Learning", "is:learn -is:suspended", "DUE"),
}


def _queue_totals() -> Dict[str, int]:
    """The three numbers the sidebar shows — new / learn / due for today.

    Same summation the sidebar's standing uses: the top level of
    `deck_due_tree()`, whose counts the v3 scheduler has *already* clipped
    to each deck's daily limits. That clipping is the point. `is:new`
    matches every unseen card in the collection (233 in the demo); the
    sidebar says 65, because 65 is what today's limits will actually let
    you see. Clicking "65" and being handed 233 is the button lying about
    what it does, so the count doubles as the queue's limit.
    """
    out = {"new": 0, "learn": 0, "due": 0}
    try:
        tree = mw.col.sched.deck_due_tree()
        for c in getattr(tree, "children", []):
            out["new"] += int(getattr(c, "new_count", 0) or 0)
            out["learn"] += int(getattr(c, "learn_count", 0) or 0)
            out["due"] += int(getattr(c, "review_count", 0) or 0)
    except Exception:
        pass
    return out


def _paused_exclusions(col: Any) -> str:
    """`-deck:"…"` for every deck a passed deadline has paused.

    See `deadlines.paused_deck_ids`: a pause is a per-day limit of 0, and
    filtered decks don't read per-day limits, so without this the sidebar's
    Due total would happily drag a paused deck's backlog back in front of
    the user.
    """
    try:
        from anki.collection import SearchNode

        from . import deadlines as _deadlines
    except Exception:
        return ""
    terms: List[str] = []
    for did in _deadlines.paused_deck_ids():
        try:
            name = col.decks.name(did)
        except Exception:
            continue
        if not name:
            continue
        try:
            terms.append("-" + col.build_search_string(SearchNode(deck=name)))
        except Exception:
            continue
    return (" " + " ".join(terms)) if terms else ""


def _study_state(kind: str) -> None:
    """Cross-deck one-click study for a sidebar total (Due/New/Learning).

    Rebuilt from scratch on every click, so what you get is the collection
    as it stands right now — not whatever the last click left behind.
    """
    spec = _STUDY_STATE_SPECS.get(kind)
    if spec is None:
        return
    name, search, order_name = spec
    col = getattr(mw, "col", None)
    if col is not None:
        search += _paused_exclusions(col)
    build_study_deck(
        name,
        search,
        order_name,
        log_tag=f"study-state {kind}",
        limit=_queue_totals().get(kind, 0),
    )


STUDY_DECK_PREFIX = "Study: "

# True while a queue is mid-build. `build_study_deck` finishes by calling
# `mw.moveToState("review")`, and moving to review *from* review runs the old
# reviewer's cleanup — which fires `reviewer_will_end`, the very hook that
# empties our queues. Without this flag a Randomize/Cue launched from inside
# the reviewer would hand back the cards it had just gathered.
_study_building = {"v": False}


def _study_decks_in_use(col: Any) -> List[Tuple[int, str]]:
    """`(id, name)` of every transient queue we own that still holds cards.

    The card-count probe matters: `empty_filtered_deck` is a transaction and
    an undo step, and this runs on every exit from the reviewer. Emptying an
    already-empty queue would push "Empty" onto the undo stack after every
    single session.
    """
    out: List[Tuple[int, str]] = []
    try:
        entries = list(col.decks.all_names_and_ids())
    except Exception:
        return out
    for entry in entries:
        if not entry.name.startswith(STUDY_DECK_PREFIX):
            continue
        try:
            deck = col.decks.get(entry.id)
            if not deck or not deck.get("dyn"):
                continue
            if not col.db.scalar("select 1 from cards where did = ? limit 1", entry.id):
                continue
        except Exception:
            continue
        out.append((int(entry.id), entry.name))
    return out


def release_study_queues() -> int:
    """Send every `Study: …` queue's cards home. The at-rest invariant.

    These queues are *transient*: a click on a sidebar total (or Randomize /
    Cue) gathers a fresh one and drops you into it, and the moment you leave
    that session the cards go back to the decks they actually live in. That
    is the whole point — a filtered deck can only hold a card once, so a
    queue left standing quietly steals cards from every other view of the
    collection: the deck list under-counts, the Browser shows the cards
    filed under "Study: Due", and the next queue you build can't even see
    them (rslib appends `-deck:filtered` to every gather).

    Emptying is Anki's own "Empty" button — cards return home with their
    scheduling untouched — so this is free to run as often as we like.
    """
    if _study_building["v"]:
        return 0
    col = getattr(mw, "col", None)
    if col is None:
        return 0
    released = 0
    for did, name in _study_decks_in_use(col):
        try:
            col.sched.empty_filtered_deck(did)
            released += 1
        except Exception as exc:
            _dev_cmd_log(f"release {name}: {exc!r}")
    if released:
        _dev_cmd_log(f"released {released} study queue(s)")
    return released


def _patch_browser_sidebar_hides_spent_queues() -> None:
    """Keep spent `Study: …` queues out of the Browser's deck tree.

    Same reasoning as the deck list (see homedeck.js): between sessions
    these queues hold nothing, so a row for one is a deck that isn't
    anywhere. Worse here than on the deck list, because in the Browser a
    deck row is a *search*, and clicking it would return nothing at all.

    Patched on the class rather than the instance: the sidebar is built
    inside `Browser.__init__`, before `browse_embed` gets its hands on the
    object, and a standalone Browser deserves the same treatment.
    """
    try:
        from aqt.browser.sidebar.item import SidebarItemType
        from aqt.browser.sidebar.tree import SidebarTreeView
    except Exception:
        return
    if getattr(SidebarTreeView, "_ad_hides_spent_queues", False):
        return
    orig = SidebarTreeView._deck_tree

    def _deck_tree(self, root) -> None:  # type: ignore[no-untyped-def]
        orig(self, root)
        try:
            col = self.col
            live = {did for did, _ in _study_decks_in_use(col)}

            def keep(item) -> bool:
                if item.item_type is not SidebarItemType.DECK:
                    return True
                if not item.full_name.startswith(STUDY_DECK_PREFIX):
                    return True
                if int(item.id) in live:
                    return True
                deck = col.decks.get(int(item.id))
                # A *normal* deck someone named "Study: …" is theirs, not
                # ours. Only the filtered ones are queues.
                return not (deck and deck.get("dyn"))

            for section in root.children:
                if section.item_type is SidebarItemType.DECK_ROOT:
                    section.children = [c for c in section.children if keep(c)]
        except Exception:
            pass

    SidebarTreeView._deck_tree = _deck_tree  # type: ignore[assignment]
    SidebarTreeView._ad_hides_spent_queues = True  # type: ignore[attr-defined]


_patch_browser_sidebar_hides_spent_queues()


def _release_study_decks(col: Any, keep: int = 0) -> int:
    """Send cards home from the filtered decks that would otherwise hide
    them from the queue we're about to build.

    rslib appends `-deck:filtered` to every filtered-deck search
    (`move_cards_matching_term`), so a card already sitting in ANY
    filtered deck is invisible to a new one. Get this wrong and the build
    doesn't half-work, it fails outright with `FilteredDeckError` and the
    user is told there is nothing to study — while still looking at the
    deck they were already in, which reads exactly like "the button did
    nothing".

    Two kinds of deck are released, both non-destructively (this is
    Anki's own "Empty" button: cards go home with their scheduling
    intact):

      * our own transient queues (`Study: …`). At rest there shouldn't be
        any left to release — `release_study_queues` empties them when you
        leave the reviewer — but a crash mid-session, or a queue built by
        an older version of this add-on, can leave one standing;
      * the filtered deck currently being studied, whoever built it. If
        you're inside a cram deck and press "Randomize Set", re-gathering
        what you're looking at is unambiguously what you asked for.

    Filtered decks you are *not* studying are left alone — silently
    emptying someone's hand-built cram deck would be rude. If cards are
    stuck in one of those, `_blocking_filtered_decks` names it in the
    failure message instead.
    """
    released = 0
    targets: List[Tuple[int, str]] = []
    try:
        current = int(col.decks.get_current_id())
    except Exception:
        current = 0
    try:
        for entry in col.decks.all_names_and_ids():
            if entry.id == keep:
                continue
            ours = entry.name.startswith(STUDY_DECK_PREFIX)
            if ours or entry.id == current:
                targets.append((entry.id, entry.name))
    except Exception as exc:
        _dev_cmd_log(f"release study decks: {exc!r}")
        return 0
    for did, name in targets:
        try:
            deck = col.decks.get(did)
            if not deck or not deck.get("dyn"):
                continue
            col.sched.empty_filtered_deck(did)
            released += 1
        except Exception as exc:
            _dev_cmd_log(f"release {name}: {exc!r}")
    return released


def _blocking_filtered_decks(col: Any, search: str) -> List[str]:
    """Filtered decks still holding cards our search wanted.

    Only called when a build came back empty, so the per-card lookup is
    affordable and buys a message that says what to do rather than
    "nothing to study".
    """
    names = set()
    try:
        for cid in col.find_cards(search):
            card = col.get_card(cid)
            if getattr(card, "odid", 0):
                names.add(col.decks.name(card.did))
    except Exception:
        return []
    return sorted(names)


def build_study_deck(
    name: str,
    search: str,
    order_name: str = "DUE",
    log_tag: str = "study-deck",
    empty_message: str = "Nothing to study there right now",
    limit: int = 0,
) -> None:
    """Build (or rebuild) a rescheduling filtered deck and enter review.

    The one place in the add-on that creates study queues: the sidebar's
    Due/New/Learning totals, and the reviewer's Randomize / Cue menu items
    (see reviewer_menu.py). `reschedule = True` throughout, mirroring
    custom study's cram kinds in rslib's custom_study.rs, so answers count
    exactly as normal reviews would rather than being thrown away.

    Reuses the deck of the same name if it already exists, so repeated use
    refreshes one queue instead of littering the deck list. An empty
    result surfaces as a tooltip, not a traceback.

    Any *other* queue we own is emptied first — see `_release_study_decks` —
    and the queue this builds is emptied again the moment you leave the
    reviewer (`release_study_queues`). Between clicks, nothing lives here.

    `limit` caps the gather. 0 means "everything the search matches", which
    is right for Cue (drill this tag, all of it) and wrong for the sidebar
    totals, which have a number printed on them — see `_queue_totals`.
    """
    try:
        from anki.decks import DeckId, FilteredDeckConfig
        from aqt.operations import QueryOp
        from aqt.operations.scheduling import add_or_update_filtered_deck
        from aqt.utils import tooltip
    except Exception:
        return
    col = getattr(mw, "col", None)
    if col is None:
        return
    existing = col.decks.id_for_name(name)
    if existing:
        deck = col.decks.get(existing)
        if deck and not deck["dyn"]:
            try:
                tooltip(f'A normal deck already uses the name "{name}".', parent=mw)
            except Exception:
                pass
            return
    target = DeckId(existing or 0)
    order = getattr(FilteredDeckConfig.SearchTerm.Order, order_name)
    _release_study_decks(col, keep=existing or 0)

    def on_fetched(update) -> None:
        update.name = name
        config = update.config
        config.reschedule = True
        del config.search_terms[:]
        config.search_terms.append(
            FilteredDeckConfig.SearchTerm(
                search=search, limit=int(limit) or 99_999, order=order
            )
        )
        update.allow_empty = False

        def on_success(_changes) -> None:
            # Moving to review *from* review tears the old reviewer down,
            # which fires `reviewer_will_end` — our at-rest release. Hold it
            # off for the length of the transition or we'd immediately give
            # back the cards we just gathered.
            _study_building["v"] = True
            try:
                mw.moveToState("review")
            finally:
                _study_building["v"] = False

        def on_failure(exc: Exception) -> None:
            # Typically FilteredDeckError: the search gathered no cards.
            _dev_cmd_log(f"{log_tag}: {exc!r}")
            msg = empty_message
            blocked = _blocking_filtered_decks(col, search)
            if blocked:
                # The cards exist — they're just locked inside a filtered
                # deck we chose not to empty on the user's behalf. Say so.
                msg = (
                    "Those cards are in the filtered deck "
                    + ", ".join(f'"{n}"' for n in blocked[:3])
                    + " — empty it first."
                )
            try:
                tooltip(msg, parent=mw, period=5000)
            except Exception:
                pass

        add_or_update_filtered_deck(parent=mw, deck=update).success(
            on_success
        ).failure(on_failure).run_in_background()

    QueryOp(
        parent=mw,
        op=lambda col: col.sched.get_or_create_filtered_deck(deck_id=target),
        success=on_fetched,
    ).run_in_background()


def _start_studying(did: int) -> None:
    """Select a deck and go straight into the reviewer."""
    try:
        _dev_cmd_log(f"start_studying: did={did}")
        mw.col.decks.select(did)
        try:
            mw.col.startTimebox()
        except Exception:
            pass
        cur_state = getattr(mw, "state", "")
        rv = getattr(mw, "reviewer", None)
        if cur_state == "review" and rv is not None:
            try:
                rv._showQuestion()
            except Exception:
                pass
        else:
            mw.moveToState("review")
        # Log final state for debugging.
        try:
            after = getattr(mw, "state", "")
            _dev_cmd_log(f"start_studying done (state now={after})")
        except Exception:
            _dev_cmd_log("start_studying done")
    except Exception as e:
        _dev_cmd_log(f"start_studying err: {e!r}")
        try:
            mw.moveToState("overview")
        except Exception:
            pass


def _single_deck_hero() -> str:
    """When the user has just one top-level deck, present it as a hero with
    the deck name + actionable stats + a primary Study button instead of a
    one-row table that would feel silly."""
    try:
        tree = mw.col.sched.deck_due_tree()
        kids = getattr(tree, "children", [])
        if len(kids) != 1:
            return ""
        d = kids[0]
        name = html.escape(getattr(d, "name", ""))
        new_n = int(getattr(d, "new_count", 0) or 0)
        learn_n = int(getattr(d, "learn_count", 0) or 0)
        rev_n = int(getattr(d, "review_count", 0) or 0)
        did = int(getattr(d, "deck_id", 0))
        total = new_n + learn_n + rev_n
        # The whole card IS the action — no button. Click anywhere on it to
        # start studying. Big numbers carry the visual weight; deck title is
        # small because it's incidental once the user knows which deck.
        click = "" if not total else f"pycmd('ba:study:{did}')"
        tabindex = "-1" if not total else "0"
        disabled = "ba-hero--done" if not total else ""
        # Deck name lives ABOVE the card now (a quiet header). The card is
        # focused on the numbers + the click-anywhere action. The small gear
        # next to the name opens this deck's options dialog (otherwise hard
        # to reach in single-deck mode since the deck row is hidden).
        return f"""
        <header class="ba-deck-head ba-rise">
          <h1 class="ba-deck-name" data-did="{did}">{name}</h1>
          <button class="ba-deck-opts"
                  onclick="window.__adDeckOpts({did}, event)"
                  title="Deck options" aria-label="Deck options">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor"
                 stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round">
              <circle cx="12" cy="12" r="3"/>
              <path d="M19.4 15a1.6 1.6 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.6 1.6 0 0 0-1.8-.3 1.6 1.6 0 0 0-1 1.5V21a2 2 0 0 1-4 0v-.1a1.6 1.6 0 0 0-1-1.5h0a1.6 1.6 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.6 1.6 0 0 0 .3-1.8 1.6 1.6 0 0 0-1.5-1H3a2 2 0 0 1 0-4h.1a1.6 1.6 0 0 0 1.5-1 1.6 1.6 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.6 1.6 0 0 0 1.8.3h0a1.6 1.6 0 0 0 1-1.5V3a2 2 0 0 1 4 0v.1a1.6 1.6 0 0 0 1 1.5h0a1.6 1.6 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.6 1.6 0 0 0-.3 1.8v0a1.6 1.6 0 0 0 1.5 1H21a2 2 0 0 1 0 4h-.1a1.6 1.6 0 0 0-1.5 1z"/>
            </svg>
          </button>
        </header>
        <button class="ba-hero ba-rise {disabled}" tabindex="{tabindex}"
                onclick="{click}" aria-label="Study {name}">
          <div class="ba-hero-stats">
            <div class="ba-hero-stat ba-due">
              <span class="ba-hero-n">{rev_n}</span>
              <span class="ba-hero-l">Due</span>
            </div>
            <div class="ba-hero-stat ba-new">
              <span class="ba-hero-n">{new_n}</span>
              <span class="ba-hero-l">New</span>
            </div>
            <div class="ba-hero-stat ba-learn">
              <span class="ba-hero-n">{learn_n}</span>
              <span class="ba-hero-l">Learn</span>
            </div>
          </div>
        </button>
        <script>
          (function() {{
            var card = document.querySelector('.ba-hero');
            if (!card || card.classList.contains('ba-hero--done')) return;
            document.addEventListener('keydown', function(e) {{
              if ((e.key === 'Enter' || e.key === ' ')
                  && !e.target.closest('input, textarea, [contenteditable]')) {{
                e.preventDefault();
                card.click();
              }}
            }});
          }})();
        </script>
        """
    except Exception:
        return ""


def _pulse_html(show_today: bool = True, show_streak: bool = True) -> str:
    """Today panel (totals + hourly session-bars) and a separated streak
    element. Renders for both single-deck and multi-deck modes; sits
    inside .ba-practice so it inherits the section's width and entry
    animation.

    Today panel: a soft-bg card holding two things — an editorial header
    of "N cards · M min" in the .ba-cg-result mixed-type vocabulary, and
    below it a histogram of cards-per-hour from the user's first studied
    hour through the current hour. Empty hours mid-session render as a
    short dim stub so breaks read as gaps, not as missing data.

    Streak: a separate flat element (no panel chrome) below the today
    panel — thematically grouped with the heatmap below, since both
    speak to long-term consistency rather than immediate effort."""
    if not show_today and not show_streak:
        return ""
    try:
        s = _standing()
    except Exception:
        return ""
    today_total = int(s.get("today", 0) or 0)
    streak = int(s.get("streak", 0) or 0)
    try:
        mins = int(_minutes_today() or 0)
    except Exception:
        mins = 0
    by_hour = _today_by_hour()
    rollover = _rollover_hour()

    # Build the streak chip once — always present (a streak persists across
    # days of inactivity until it actually breaks).
    zs = " is-zero" if streak == 0 else ""
    # Heroicons "fire" (solid) — a recognizable two-arc flame with an
    # inner ember. Reads as fire at any size without the cartoon-emoji
    # vibe; matches the addon's filled-icon family weight.
    fire_svg = (
        '<svg class="ba-streak-fire" viewBox="0 0 24 24" '
        'fill="currentColor" aria-hidden="true">'
        '<path fill-rule="evenodd" clip-rule="evenodd" '
        'd="M12.963 2.286a.75.75 0 0 0-1.071-.136 9.742 9.742 0 0 0-3.539 '
        '6.177A7.547 7.547 0 0 1 6.648 6.61a.75.75 0 0 0-1.152-.082A9 9 0 '
        '1 0 15.68 4.534a7.46 7.46 0 0 1-2.717-2.248zM15.75 14.25a3.75 '
        '3.75 0 1 1-7.313-1.172c.628.465 1.35.81 2.133 1A5.99 5.99 0 0 1 '
        '12.366 10.4a3.75 3.75 0 0 1 3.384 3.85z"/>'
        '</svg>'
    )
    streak_html = (
        f'<div class="ba-streak{zs}" role="group" '
        f'aria-label="{streak} day streak">'
        f'{fire_svg}'
        f'<span class="ba-streak-n">{streak:,}</span>'
        f'</div>'
    ) if show_streak else ""

    # If there are no reviews today, omit the today panel entirely —
    # an empty card just to say "nothing yet" reads as filler, and the
    # streak + heatmap below already tell the relevant story for an
    # un-studied day. Same when the panel is switched off in Settings.
    if not by_hour or not show_today:
        return f'<div class="ba-pulse">{streak_html}</div>' if streak_html else ""

    # Build the today panel body. by_hour is guaranteed non-empty here
    # because we returned early above when it was empty.
    first_h = min(by_hour.keys())
    # "Current hour" — where we should end the chart. If the user has a row
    # in an hour past `now` (e.g. dev-fixture timestamp drift), respect that
    # as the right edge so the data doesn't get clipped.
    shift = _day_shift_seconds()
    now_h_within = int(((time.time() + shift) % 86400) / 3600)
    last_h = max(now_h_within, max(by_hour.keys()))
    hours = list(range(first_h, last_h + 1))
    max_count = max((n for n, _ in by_hour.values()), default=1) or 1
    # Render bars. Each bar is a column wrapper holding a count label that
    # rides above the bar, the bar itself, and a custom hover tooltip with
    # hour + cards + minutes. The tooltip is plain DOM (not the browser's
    # title attr) so we can style it to match the page.
    bar_html = []
    for h in hours:
        n, ms = by_hour.get(h, (0, 0))
        # Cap heights at 88% so the count label above always has room
        # without the bar visually colliding with it; empty hours get a
        # tiny stub so breaks remain visible as ticks.
        if n > 0:
            pct = max(8.0, (n / max_count) * 88.0)
        else:
            pct = 4.0
        cls = "ba-today-bar" + ("" if n > 0 else " is-empty")
        count_str = str(n) if n > 0 else ""
        hour_label = _format_hour_of_day((rollover + h) % 24)
        # Minutes for the tooltip — sub-30s rounds to "<1", otherwise
        # rounded minutes. We don't show a tooltip on empty stubs.
        if n > 0:
            if ms < 30_000:
                mins_str = "<1 min"
            else:
                mins_str = f"{round(ms / 60_000)} min"
            tip_html = (
                f'<span class="ba-today-bar-tip" aria-hidden="true">'
                f'<span class="ba-today-bar-tip-h">{hour_label}</span>'
                f'<span class="ba-today-bar-tip-d">'
                f'<b>{n:,}</b> cards · <b>{mins_str}</b>'
                f'</span>'
                f'</span>'
            )
        else:
            tip_html = ""
        bar_html.append(
            f'<span class="ba-today-bar-col">'
            f'{tip_html}'
            f'<span class="ba-today-bar-count">{count_str}</span>'
            f'<span class="{cls}" style="height:{pct:.1f}%"></span>'
            f'</span>'
        )
    bars = "".join(bar_html)
    # Labels: collapse to a single centered "NOW" when the session sits in
    # one hour (the user-just-started case). Otherwise the start hour
    # anchors the left and "NOW" the right.
    if first_h == last_h:
        labels = (
            f'<div class="ba-today-labels ba-today-labels--solo">'
            f'<span class="ba-today-now">NOW</span>'
            f'</div>'
        )
    else:
        first_label = _format_hour_of_day((rollover + first_h) % 24)
        labels = (
            f'<div class="ba-today-labels">'
            f'<span>{first_label}</span>'
            f'<span class="ba-today-now">NOW</span>'
            f'</div>'
        )

    # Header row: editorial mixed-type totals. Uses the .ba-cg-result
    # vocabulary (serif numbers + sans uppercase units + mid-dot).
    head = (
        f'<div class="ba-today-head">'
        f'<span class="ba-today-n">{today_total:,}</span>'
        f'<span class="ba-today-u">cards</span>'
        f'<span class="ba-today-sep">·</span>'
        f'<span class="ba-today-n">{mins:,}</span>'
        f'<span class="ba-today-u">min</span>'
        f'</div>'
    )

    return f"""
    <div class="ba-pulse">
      <section class="ba-today" aria-label="Your study today">
        {head}
        <div class="ba-today-bars">{bars}</div>
        {labels}
      </section>
      {streak_html}
    </div>
    """


def _today_by_hour() -> Dict[int, tuple]:
    """Cards reviewed in each hour-within-today, keyed by hour-offset from
    the day rollover. Returns {hour: (card_count, total_time_ms)} so the
    today-panel hover tooltips can show per-hour minutes alongside counts
    without a second SQL round-trip. Empty dict on any error."""
    try:
        shift = _day_shift_seconds()
        today_idx = int((time.time() + shift) // 86400)
        rows = mw.col.db.all(
            "select cast(((id/1000 + ?) - ? * 86400) / 3600 as int) as hr, "
            "       count(), sum(time) "
            "from revlog "
            "where cast((id/1000 + ?) / 86400 as int) = ? "
            "group by hr",
            shift, today_idx, shift, today_idx,
        )
        return {int(h): (int(n), int(t or 0)) for h, n, t in rows}
    except Exception:
        return {}


def _format_hour_of_day(h: int) -> str:
    """12-hour time label with AM/PM suffix only at the boundary. `h` is
    a 0..23 hour-of-day in user local time."""
    h = h % 24
    if h == 0:
        return "12 AM"
    if h == 12:
        return "12 PM"
    if h < 12:
        return f"{h} AM"
    return f"{h - 12} PM"


def _minutes_today() -> int:
    """Total minutes reviewed today, from the revlog. 0 on any error."""
    try:
        shift = _day_shift_seconds()
        today_idx = int((time.time() + shift) // 86400)
        row = mw.col.db.first(
            "select sum(time) from revlog where "
            "cast((id/1000 + ?) / 86400 as int) = ?",
            shift, today_idx,
        )
        if row and row[0]:
            return int(float(row[0]) / 60000.0)
    except Exception:
        pass
    return 0


def _top_decks_count() -> int:
    try:
        tree = mw.col.sched.deck_due_tree()
        return len(getattr(tree, "children", []))
    except Exception:
        return 0


def _last_7_days_active() -> list:
    """A 7-bool list, oldest → today, for the sidebar mini-grid."""
    try:
        counts = _counts_by_day()
        shift = _day_shift_seconds()
        today_idx = int((time.time() + shift) // 86400)
        return [bool(counts.get(today_idx - i, 0) > 0) for i in range(6, -1, -1)]
    except Exception:
        return [False] * 7


def _build_standing_payload() -> Dict[str, Any]:
    s = _standing()
    return {
        "streak": s.get("streak", 0),
        "due": s.get("due"),
        "new": s.get("new"),
        "learn": s.get("learn"),
        "today": s.get("today", 0),
        "todayMin": _minutes_today(),
        "total": s.get("total", 0),
        "singleDeck": _hero_mode(),
        "last7": _last_7_days_active(),
    }


def _push_sidebar_sync(state: str) -> None:
    """Update the sidebar's Sync indicator state. `state` is one of:
    "" (clean), "pending", "full", "active"."""
    safe = "".join(ch for ch in state if ch.isalnum())
    js = "window.__baSetSync && window.__baSetSync('%s');" % safe
    for w in (getattr(mw, "web", None),):
        if w is not None:
            try:
                w.eval(js)
            except Exception:
                pass


def _refresh_sync_status() -> None:
    """Ask Anki for the current sync status and push it to the sidebar."""
    try:
        from aqt.sync import get_sync_status
        from anki.sync_pb2 import SyncStatusResponse

        def on_status(status):
            req = getattr(status, "required", 0)
            if req == SyncStatusResponse.NORMAL_SYNC:
                _push_sidebar_sync("pending")
            elif req == SyncStatusResponse.FULL_SYNC:
                _push_sidebar_sync("full")
            else:
                _push_sidebar_sync("")
        get_sync_status(mw, on_status)
    except Exception:
        # Older Anki / API change: fall back to silently clearing.
        _push_sidebar_sync("")


# --------------------------------------------------------------------------- #
# Sync — silent, sidebar-driven UX.
#
# Anki's default flow opens a modal QProgressDialog (titled "Checking…" then
# "Uploading…" etc.) while sync runs, and pops a `tooltip("Sync complete")`
# after it closes. Both yank focus and break the page's visual flow. We
# replace `mw._sync_collection_and_media` with a version that runs the same
# sync via `taskman.run_in_background` (no modal, no tooltip) and feeds the
# lifecycle to the sidebar instead — the existing "active" pulse on the
# Sync row carries the load while a 150 ms timer mirrors normal_sync stage
# strings to JS, and a final result push triggers the "Synced" reveal.
#
# Confirmation paths that legitimately need a dialog (login when no auth,
# full-upload/full-download choice) still surface via Anki's own UI — we
# only suppress the *progress* dialog and the *post-sync* tooltip.
# --------------------------------------------------------------------------- #
def _push_sidebar_sync_progress(stage: str, added: str, removed: str) -> None:
    """Mirror normal_sync progress strings to the sidebar's Sync row."""
    import json as _json
    payload = _json.dumps({"stage": stage or "", "added": added or "",
                           "removed": removed or ""})
    js = "window.__baSetSyncProgress && window.__baSetSyncProgress(%s);" % payload
    w = getattr(mw, "web", None)
    if w is not None:
        try:
            w.eval(js)
        except Exception:
            pass


def _push_sidebar_sync_result(kind: str) -> None:
    """Trigger the sidebar's end-of-sync reveal. kind ∈ {"ok","noop","error"}."""
    safe = "".join(ch for ch in kind if ch.isalnum())
    js = "window.__baSetSyncResult && window.__baSetSyncResult('%s');" % safe
    w = getattr(mw, "web", None)
    if w is not None:
        try:
            w.eval(js)
        except Exception:
            pass


def _ad_sync_collection_silent(mw_arg: Any, on_done) -> None:
    """Drop-in for `aqt.sync.sync_collection` — no modal, no tooltip.

    Calls `mw.col.sync_collection(auth, media_enabled)` directly through
    `taskman.run_in_background`, polls `latest_progress().normal_sync`
    every 150 ms, and routes the result to the sidebar. Full-sync paths
    still delegate to `aqt.sync.full_sync` (which owns its own dialog)."""
    from aqt.sync import full_sync as _full_sync, handle_sync_error
    from aqt.qt import QTimer, qconnect
    from aqt.utils import showText

    auth = mw_arg.pm.sync_auth()
    if not auth:
        # Should never happen: callers gate on auth. Defensive no-op.
        return on_done()

    seen = {"added": "", "removed": "", "stage": ""}
    timer = QTimer(mw_arg)

    def on_timer() -> None:
        try:
            progress = mw_arg.col.latest_progress()
            if progress.HasField("normal_sync"):
                p = progress.normal_sync
                if p.added:   seen["added"]   = p.added
                if p.removed: seen["removed"] = p.removed
                if p.stage:   seen["stage"]   = p.stage
                _push_sidebar_sync_progress(p.stage, p.added, p.removed)
        except Exception:
            pass
    qconnect(timer.timeout, on_timer)
    timer.start(150)

    def on_future_done(fut) -> None:
        try:
            mw_arg.col._load_scheduler()
        except Exception:
            pass
        timer.stop()
        try:
            out = fut.result()
        except Exception as err:
            _push_sidebar_sync_result("error")
            handle_sync_error(mw_arg, err)
            return on_done()

        try:
            mw_arg.pm.set_host_number(out.host_number)
            if out.new_endpoint:
                mw_arg.pm.set_current_sync_url(out.new_endpoint)
            if out.server_message:
                showText(out.server_message, parent=mw_arg)
        except Exception:
            pass

        if out.required == out.NO_CHANGES:
            # Distinguish "real changes were synced" vs "heartbeat, nothing
            # happened" so the reveal can be louder vs quieter accordingly.
            had_changes = bool(seen["added"] or seen["removed"])
            _push_sidebar_sync_result("ok" if had_changes else "noop")
            try:
                mw_arg.media_syncer.start_monitoring()
            except Exception:
                pass
            return on_done()
        else:
            # Full upload/download paths legitimately need a confirmation
            # dialog — fall back to Anki's native flow. The sidebar's
            # "active" pulse is cleared by on_done()'s gui_hooks.sync_did_finish.
            _push_sidebar_sync_result("ok")
            _full_sync(mw_arg, out, on_done)

    mw_arg.taskman.run_in_background(
        lambda: mw_arg.col.sync_collection(auth, mw_arg.pm.media_syncing_enabled()),
        on_future_done,
    )


def _install_silent_sync() -> None:
    """Patch mw._sync_collection_and_media to use our silent flow.

    Both manual sync (Sync button / Y) and automatic sync on profile
    open/close route through this method, so a single patch covers all
    entry points."""
    import types

    orig = getattr(mw, "_sync_collection_and_media", None)

    def _silent(self, after_sync) -> None:
        if not _config().get("silent_sync", True) and orig is not None:
            # User prefers Anki's own progress dialog + "Sync complete".
            return orig(after_sync)

        def on_collection_sync_finished() -> None:
            try:
                self.col.models._clear_cache()
            except Exception:
                pass
            gui_hooks.sync_did_finish()
            try:
                self.reset()
            except Exception:
                pass
            after_sync()
        gui_hooks.sync_will_start()
        _ad_sync_collection_silent(self, on_done=on_collection_sync_finished)

    try:
        mw._sync_collection_and_media = types.MethodType(_silent, mw)
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# Congrats page (Overview's empty state) — redesigned.
# Anki's "Congratulations! You have finished this deck for now." is a Svelte
# page loaded via `web.load_sveltekit_page("congrats")`. That bypasses the
# `webview_will_set_content` filter, so we hook `webview_did_inject_style_into_page`
# instead — it fires after Anki's standard CSS injection for *every* page,
# including Svelte ones. We detect the congrats URL and inject our redesign.
# --------------------------------------------------------------------------- #
def _deck_and_descendant_ids(did: int) -> list:
    """Return [did, *all_descendant_dids] for a given deck. Best-effort: we
    fall back to just [did] if Anki's deck API surface differs."""
    try:
        ids = [int(did)]
        try:
            children = mw.col.decks.children(did)  # [(name, id), ...]
            ids.extend(int(cid) for _name, cid in children)
        except Exception:
            pass
        return ids
    except Exception:
        return [int(did)]


def _finished_deck_stats(did: int) -> Dict[str, Any]:
    """Today's revlog stats for the deck the user just finished (deck + its
    descendants). Returns counts, total time, and ease breakdown — used as the
    big-number panel on the redesigned congrats page."""
    out: Dict[str, Any] = {
        "thisDeck": 0,
        "timeSec": 0,
        "breakdown": {"again": 0, "hard": 0, "good": 0, "easy": 0},
    }
    try:
        shift = _day_shift_seconds()
        today_idx = int((time.time() + shift) // 86400)
        ids = _deck_and_descendant_ids(int(did))
        if not ids:
            return out
        placeholders = ",".join("?" * len(ids))
        row = mw.col.db.first(
            f"select count(*), coalesce(sum(time),0), "
            f"sum(case when ease=1 then 1 else 0 end), "
            f"sum(case when ease=2 then 1 else 0 end), "
            f"sum(case when ease=3 then 1 else 0 end), "
            f"sum(case when ease=4 then 1 else 0 end) "
            f"from revlog where cid in "
            f"(select id from cards where did in ({placeholders})) "
            f"and cast((id/1000 + ?) / 86400 as int) = ?",
            *ids, shift, today_idx,
        )
        if row:
            cnt, ms, ag, hd, gd, ez = row
            out["thisDeck"] = int(cnt or 0)
            out["timeSec"] = int(float(ms or 0) / 1000.0)
            out["breakdown"] = {
                "again": int(ag or 0),
                "hard":  int(hd or 0),
                "good":  int(gd or 0),
                "easy":  int(ez or 0),
            }
    except Exception:
        pass
    return out


def _full_deck_tree_payload() -> list:
    """Flat list of EVERY deck (top + descendants), tagged with depth and
    counts. Used to render the home-page deck list with the same JS render
    path as the congrats "Keep going" list — both pages feed an identical
    structure into __adDeckList.render()."""
    out: list = []
    try:
        tree = mw.col.sched.deck_due_tree()
        try:
            current_did = int(mw.col.decks.get_current_id())
        except Exception:
            try:
                current_did = int(mw.col.decks.current()["id"])
            except Exception:
                current_did = 0

        def is_filtered(did: int) -> bool:
            try:
                d = mw.col.decks.get(did)
                return bool(d and d.get("dyn"))
            except Exception:
                return False

        def visit(node, depth):
            did = int(getattr(node, "deck_id", 0) or 0)
            n = int(getattr(node, "new_count", 0) or 0)
            l = int(getattr(node, "learn_count", 0) or 0)
            r = int(getattr(node, "review_count", 0) or 0)
            kids = list(getattr(node, "children", []) or [])
            kid_rows: list = []
            for c in kids:
                kid_rows.extend(visit(c, depth + 1))
            if did == 0:
                return kid_rows
            full = str(getattr(node, "name", "") or "")
            leaf = full.split("::")[-1]
            try:
                path = str(mw.col.decks.name(did) or full)
            except Exception:
                path = full
            row = {
                "did": did,
                "name": leaf,
                "path": path,
                "depth": depth,
                "new": n,
                "learn": l,
                "review": r,
                "current": did == current_did,
                "filtered": is_filtered(did),
                # One of our transient `Study: …` queues. At rest these are
                # always empty (see `release_study_queues`), and an empty
                # queue is a row about nothing — the deck list drops it.
                "transient": bool(
                    path.startswith(STUDY_DECK_PREFIX) and is_filtered(did)
                ),
                # Anki's persisted collapse flag (the one its own deck list
                # honours and syncs). Only meaningful on parents.
                "collapsed": bool(getattr(node, "collapsed", False)) and bool(kids),
            }
            return [row] + kid_rows

        out = visit(tree, -1)
    except Exception:
        pass
    return out


def _filtered_deck_tree(exclude_did: int) -> list:
    """Flat list of decks with any work (new/learn/review > 0), excluding the
    finished deck *and its descendants*. Preserves Anki's natural deck order
    (a depth-first walk of `deck_due_tree()`), tagging each entry with its
    visual depth so the JS can indent."""
    out: list = []
    try:
        exclude = set(int(x) for x in _deck_and_descendant_ids(int(exclude_did)))
        tree = mw.col.sched.deck_due_tree()

        def visit(node, depth):
            did = int(getattr(node, "deck_id", 0) or 0)
            n = int(getattr(node, "new_count", 0) or 0)
            l = int(getattr(node, "learn_count", 0) or 0)
            r = int(getattr(node, "review_count", 0) or 0)
            # Sum descendant work so a parent with empty self but stocked kids
            # still surfaces as a row (we still show kids individually).
            sub_total = n + l + r
            kids = list(getattr(node, "children", []) or [])
            kid_rows: list = []
            for c in kids:
                kid_rows.extend(visit(c, depth + 1))
                cn = int(getattr(c, "new_count", 0) or 0)
                cl = int(getattr(c, "learn_count", 0) or 0)
                cr = int(getattr(c, "review_count", 0) or 0)
                sub_total += cn + cl + cr
            if did in exclude:
                return []
            if did == 0:
                # Root — emit kids only.
                return kid_rows
            if sub_total == 0:
                return []
            # Leaf name (after the last "::" — Anki uses "^_" internally but
            # the `name` field on the tree node is the display path; we want
            # just the leaf for the row, the depth carries the hierarchy).
            full = str(getattr(node, "name", "") or "")
            leaf = full.split("::")[-1]
            row = {
                "did": did,
                "name": leaf,
                "depth": depth,
                "new": n,
                "learn": l,
                "review": r,
                "collapsed": bool(getattr(node, "collapsed", False)) and bool(kid_rows),
            }
            return [row] + kid_rows

        out = visit(tree, -1)  # root depth -1 so top-level decks are 0
    except Exception:
        pass
    return out


def _build_congrats_payload() -> Dict[str, Any]:
    """Bundle everything congrats.js needs for the redesigned page."""
    did = 0
    name = ""
    try:
        did = int(mw.col.decks.get_current_id())
    except Exception:
        try:
            did = int(mw.col.decks.current()["id"])
        except Exception:
            pass
    try:
        if did:
            name = str(mw.col.decks.name(did) or "")
            # Anki uses "::" as the display separator; the leaf reads best.
            name = name.split("::")[-1] or name
    except Exception:
        pass
    stats = _finished_deck_stats(did) if did else {}
    # Dev preview: when designing the congrats page against a cached demo
    # collection, "today" rarely has any reviews. Fall back to the most
    # recent day with reviews on this deck so the page renders with real
    # numbers. Only active when .devmode is present (never ships).
    if _dev_active() and did and (stats.get("thisDeck") or 0) == 0:
        try:
            ids = _deck_and_descendant_ids(int(did))
            if ids:
                placeholders = ",".join("?" * len(ids))
                shift = _day_shift_seconds()
                row = mw.col.db.first(
                    f"select cast((id/1000 + ?) / 86400 as int) as d "
                    f"from revlog where cid in "
                    f"(select id from cards where did in ({placeholders})) "
                    f"order by id desc limit 1",
                    shift, *ids,
                )
                if row and row[0] is not None:
                    d = int(row[0])
                    row2 = mw.col.db.first(
                        f"select count(*), coalesce(sum(time),0), "
                        f"sum(case when ease=1 then 1 else 0 end), "
                        f"sum(case when ease=2 then 1 else 0 end), "
                        f"sum(case when ease=3 then 1 else 0 end), "
                        f"sum(case when ease=4 then 1 else 0 end) "
                        f"from revlog where cid in "
                        f"(select id from cards where did in ({placeholders})) "
                        f"and cast((id/1000 + ?) / 86400 as int) = ?",
                        *ids, shift, d,
                    )
                    if row2:
                        cnt, ms, ag, hd, gd, ez = row2
                        stats = {
                            "thisDeck": int(cnt or 0),
                            "timeSec": int(float(ms or 0) / 1000.0),
                            "breakdown": {
                                "again": int(ag or 0),
                                "hard":  int(hd or 0),
                                "good":  int(gd or 0),
                                "easy":  int(ez or 0),
                            },
                        }
        except Exception:
            pass
    today_total = 0
    try:
        s = _standing()
        today_total = int(s.get("today", 0) or 0)
    except Exception:
        pass
    return {
        "deckId": did,
        "deckName": name or "this deck",
        "thisDeck": stats.get("thisDeck", 0),
        "todayTotal": today_total,
        "timeSec": stats.get("timeSec", 0),
        "breakdown": stats.get("breakdown", {"again": 0, "hard": 0, "good": 0, "easy": 0}),
        "otherDecks": _filtered_deck_tree(did),
    }


def _is_congrats_url(url: str) -> bool:
    if not url:
        return False
    # Anki serves the Svelte congrats page at <serverURL>/congrats (with an
    # optional `#night` fragment). Match flexibly so a future path tweak
    # (e.g. /pages/congrats) doesn't break detection.
    low = url.lower()
    if low.endswith("/congrats") or low.endswith("/congrats/"):
        return True
    if "/congrats#" in low or "/congrats?" in low:
        return True
    if low.rstrip("/").endswith("/pages/congrats"):
        return True
    return False


def _is_graphs_url(url: str) -> bool:
    if not url:
        return False
    # Anki serves the SvelteKit stats page at <serverURL>/graphs (with an
    # optional `#night` fragment). The page is loaded by NewDeckStats via
    # `web.load_sveltekit_page("graphs")`.
    low = url.lower()
    if low.endswith("/graphs") or low.endswith("/graphs/"):
        return True
    if "/graphs#" in low or "/graphs?" in low:
        return True
    return False


def _inject_graphs_overrides(webview) -> None:
    """Style the SvelteKit graphs page with our palette + typography.

    The page uses Anki's `--canvas` / `--fg` / etc tokens; web/stats.css
    re-maps those to our `--rf-*` tokens. tokens.css must load first so
    the `--rf-*` variables are defined when stats.css consumes them."""
    cfg = _config()
    accent = cfg.get("accent", "#6c8cff")
    theme_pref = cfg.get("theme", "system")
    theme_attr = ""
    if theme_pref in ("light", "dark"):
        import json as _json
        theme_attr = (
            f"document.documentElement.dataset.rfTheme="
            f"{_json.dumps(theme_pref)};"
        )
    accent_style = (
        f"var st=document.createElement('style');"
        f"st.textContent=':root,body{{--rf-accent:{accent};}}';"
        f"document.head.appendChild(st);"
    )
    css_files = ["tokens.css", "theme.css", "stats.css"]
    css_inject = ""
    for f in css_files:
        css_inject += (
            f"(function(){{var l=document.createElement('link');"
            f"l.rel='stylesheet';l.href='{WEB}/{f}';"
            f"document.head.appendChild(l);}})();"
        )
    try:
        webview.eval(theme_attr + accent_style + css_inject)
    except Exception:
        pass


def on_webview_did_inject_style_into_page(webview) -> None:
    """Detect Anki's congrats / graphs Svelte pages after their dynamic
    styling finishes, then graft our redesign on top. Idempotent (the
    style/script appends bail if they already ran), so re-firing on theme
    changes is harmless."""
    try:
        url = webview.page().url().toString()
    except Exception:
        return
    if _is_graphs_url(url):
        _inject_graphs_overrides(webview)
        return
    if not _is_congrats_url(url):
        return
    cfg = _config()
    if not cfg.get("congrats_redesign", True):
        return  # stock Anki congrats page
    try:
        import json as _json
        standing = _build_standing_payload()
        # Dev override: if a `?did=N` query string is present on /congrats,
        # build the payload around that deck instead of the current one.
        # Lets the screenshot loop preview any deck without finishing it.
        override_did = 0
        try:
            from urllib.parse import urlparse, parse_qs
            qs = parse_qs(urlparse(url).query)
            override_did = int((qs.get("did") or ["0"])[0])
        except Exception:
            override_did = 0
        if override_did:
            try:
                mw.col.decks.select(override_did)
            except Exception:
                pass
        congrats = _build_congrats_payload()
        # Build a single eval that:
        #   1. seeds globals BEFORE the scripts run,
        #   2. injects our stylesheets (tokens, theme, logo, sidebar, congrats),
        #   3. injects sidebar.js + congrats.js as scripts.
        # Asset URLs are absolute relative paths served by Anki's media server
        # (`/_addons/<dir>/web/<file>`), which works for Svelte pages too.
        accent = cfg.get("accent", "#6c8cff")
        theme_pref = cfg.get("theme", "system")
        # Mirror webview_will_set_content's :root accent declaration so colors
        # land consistently here too.
        theme_attr = ""
        if theme_pref in ("light", "dark"):
            theme_attr = (
                f"document.documentElement.dataset.rfTheme={_json.dumps(theme_pref)};"
            )
        accent_style = (
            f"var st=document.createElement('style');"
            f"st.textContent=':root,body{{--rf-accent:{accent};}}';"
            f"document.head.appendChild(st);"
        )
        css_files = [
            "tokens.css", "theme.css", "logo.css", "sidebar.css",
            "deckopts.css", "decklist.css", "congrats.css",
        ]
        js_files = ["sidebar.js", "deckopts.js", "decklist.js", "congrats.js"]
        css_inject = ""
        for f in css_files:
            css_inject += (
                f"(function(){{var l=document.createElement('link');"
                f"l.rel='stylesheet';l.href='{WEB}/{f}';"
                f"document.head.appendChild(l);}})();"
            )
        js_inject = ""
        for f in js_files:
            # `async=false` on a dynamically-inserted script preserves the
            # insertion order — otherwise decklist.js (which sets
            # window.__adDeckList) may load AFTER congrats.js, and the
            # Keep-going list silently fails to render.
            js_inject += (
                f"(function(){{var s=document.createElement('script');"
                f"s.src='{WEB}/{f}';s.async=false;"
                f"document.head.appendChild(s);}})();"
            )
        try:
            all_decks = _full_deck_tree_payload()
        except Exception:
            all_decks = []
        seed = (
            f"window.__baStandingData={_json.dumps(standing)};"
            f"window.__baCongratsData={_json.dumps(congrats)};"
            # Full tree for the deck-options "Move to…" picker + flags.
            f"window.__baDeckTree={_json.dumps(all_decks)};"
            f"window.__baOpts={_json.dumps(_js_opts(cfg))};"
            f"window.__baSidebarCollapsed="
            f"{_json.dumps(bool(cfg.get('sidebar_collapsed', False)))};"
        )
        webview.eval(theme_attr + seed + accent_style + css_inject + js_inject)
    except Exception:
        pass


def _push_sidebar_standing() -> None:
    """Push the day's standing into every webview's sidebar for live updates
    (state changes, post-render). The initial render is bootstrapped via a
    <head> global; this is for changes after that."""
    try:
        payload = _build_standing_payload()
    except Exception:
        return
    import json as _json
    js = f"window.__baSetStanding && window.__baSetStanding({_json.dumps(payload)});"
    for attr in ("web",):
        w = getattr(mw, attr, None)
        if w is not None:
            try:
                w.eval(js)
            except Exception:
                pass
    # Reviewer has its own webview.
    rv = getattr(mw, "reviewer", None)
    if rv is not None and getattr(rv, "web", None) is not None:
        try:
            rv.web.eval(js)
        except Exception:
            pass


def _any_embed_open() -> bool:
    """True while any inline embed overlay (Add, Browse, Stats, Settings)
    is painted over mw.web. Unlike `_active_embed_cmd()`, this checks all
    four, including addcard_embed — that function deliberately omits it
    for rail-highlight purposes only (an open Add overlay should still
    highlight "browse", per its own docstring), but the video backdrop
    underneath mw.web is occluded by ANY of the four, including Add's own
    fallback stacked overlay."""
    from importlib import import_module

    for mod_name in (
        "addcard_embed", "browse_embed", "stats_embed", "settings_embed",
    ):
        try:
            mod = import_module("." + mod_name, __name__)
            if mod._state.get("overlay") is not None:
                return True
        except Exception:
            continue
    return False


def _push_video_occlusion() -> None:
    """Pause or resume the deck-browser/overview video backdrop
    (video-backdrop.js) depending on whether an inline embed currently
    occludes mw.web.

    These embeds are Qt widgets painted *on top of* mw.web without ever
    hiding, minimizing, or blurring it — so `document.hidden` never
    fires for them, and without this a video backdrop underneath keeps
    decoding, invisibly, behind an opaque panel for as long as the
    overlay is up (see the long comment in video-backdrop.js's
    __baVideoPause/__baVideoResume block). Call this after anything that
    opens or closes an embed; it's cheap (one property read on each of
    four already-imported modules, then a single `.eval()`) and a no-op
    on every non-video-mode render, since the JS hooks it targets are
    only ever defined when video-backdrop.js actually initialised."""
    w = getattr(mw, "web", None)
    if w is None:
        return
    hook = "__baVideoPause" if _any_embed_open() else "__baVideoResume"
    try:
        w.eval(f"window.{hook} && window.{hook}();")
    except Exception:
        pass


def on_deck_browser_did_render(deck_browser: DeckBrowser) -> None:
    # The deck browser re-shows the bottom strip and Anki (re)sets the window
    # title around render; re-assert our state on the next event-loop tick so
    # our in-page actions stand alone and the title/active-section stick.
    if QTimer is not None:
        QTimer.singleShot(0, _post_render_fixups)
    else:
        _post_render_fixups()


# --------------------------------------------------------------------------- #
# Reviewer progress bar
# --------------------------------------------------------------------------- #
_session = {"total": 0}


def _remaining() -> int:
    try:
        return int(sum(mw.col.sched.counts()))
    except Exception:
        return 0


def _current_deck_name() -> str:
    try:
        did = mw.col.decks.get_current_id()
        return mw.col.decks.name(did) or ""
    except Exception:
        try:
            return mw.col.decks.current()["name"]
        except Exception:
            return ""


def _reviewer_ease_html() -> str:
    """Four interval chips that appear under the answer. Text only —
    no numbers, no labels, no bar. They're hidden by CSS until the
    answer is revealed. We render four slots even for shorter button
    counts (Anki may show 2/3/4 buttons); JS hides extras."""
    return (
        '<div class="ba-rv-ease" aria-label="Rate this card" hidden>'
        '<button class="ba-rv-ease-key ba-rv-ease-1" type="button"'
        '        onclick="pycmd(\'ease1\')" data-ease="1">'
        '<span class="ba-rv-ease-int" data-ease-slot="1"></span></button>'
        '<button class="ba-rv-ease-key ba-rv-ease-2" type="button"'
        '        onclick="pycmd(\'ease2\')" data-ease="2">'
        '<span class="ba-rv-ease-int" data-ease-slot="2"></span></button>'
        '<button class="ba-rv-ease-key ba-rv-ease-3" type="button"'
        '        onclick="pycmd(\'ease3\')" data-ease="3">'
        '<span class="ba-rv-ease-int" data-ease-slot="3"></span></button>'
        '<button class="ba-rv-ease-key ba-rv-ease-4" type="button"'
        '        onclick="pycmd(\'ease4\')" data-ease="4">'
        '<span class="ba-rv-ease-int" data-ease-slot="4"></span></button>'
        '</div>'
    )


def _reviewer_prev_html() -> str:
    """Bottom-LEFT "‹ Previous" control — step back to the card you just
    answered and grade it again. Unlike Hide Answer this is present on
    *both* sides of the card, so it never starts `hidden`; what changes is
    whether it's enabled, which __baSetPrev (web/reviewer.js) drives from
    _push_prev_state's read of the undo queue. Renders disabled: the first
    reviewer_did_show_question right after this body is injected pushes the
    real state a beat later."""
    return (
        '<button class="ba-rv-prev" type="button" disabled aria-disabled="true"'
        ' onclick="pycmd(\'ba:prev-card\')"'
        ' title="Back to the previous card, to grade it again (P)"'
        ' aria-label="Previous card">'
        '<span class="ba-rv-prev-chev" aria-hidden="true">‹</span>'
        'Previous</button>'
    )


def _reviewer_hide_answer_html() -> str:
    """Bottom-RIGHT "Hide Answer" control — pinned in the corner the ease
    selector's centered layout leaves empty. (It sat bottom-left until
    "‹ Previous" claimed that corner; the two are otherwise the same quiet
    flat idiom.) Only meaningful once the answer is showing ("let me try
    that card again"), so it starts `hidden` like the ease selector and is
    flipped by the same __baSetEase call (see web/reviewer.js), driven by
    the same Python-side isAnswer signal from _push_progress."""
    return (
        '<button class="ba-rv-hide-answer" type="button" hidden'
        ' onclick="pycmd(\'ba:hide-answer\')"'
        ' title="Hide answer, try again (H)"'
        ' aria-label="Hide answer">Hide Answer</button>'
    )


def _current_card_type() -> str:
    """A short label for the current card's note type (e.g., "Basic",
    "Cloze"). Empty when no card. Trimmed to fit the header."""
    try:
        c = mw.reviewer.card
        if c is None:
            return ""
        nt = c.note_type() or c.note().model()
        name = (nt.get("name") or "") if isinstance(nt, dict) else getattr(nt, "name", "")
        return str(name)
    except Exception:
        return ""


def _reviewer_header_html() -> str:
    """Header above the card: back chevron + deck name on the left, the
    count breakdown on the right, and Edit + More icon buttons next to
    them (Anki's More menu already covers flag/mark/undo)."""
    name = html.escape(_current_deck_name() or "Studying")
    new_n = learn_n = rev_n = 0
    try:
        c = mw.col.sched.counts()
        new_n, learn_n, rev_n = int(c[0]), int(c[1]), int(c[2])
    except Exception:
        pass
    edit_svg = (
        '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" '
        'stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" '
        'xmlns="http://www.w3.org/2000/svg" aria-hidden="true">'
        '<path d="M12 20h9"/>'
        '<path d="M16.5 3.5a2.121 2.121 0 1 1 3 3L7 19l-4 1 1-4Z"/></svg>'
    )
    more_svg = (
        '<svg viewBox="0 0 24 24" fill="currentColor" '
        'xmlns="http://www.w3.org/2000/svg" aria-hidden="true">'
        '<circle cx="5" cy="12" r="1.6"/>'
        '<circle cx="12" cy="12" r="1.6"/>'
        '<circle cx="19" cy="12" r="1.6"/></svg>'
    )
    return f"""
    <header class="ba-rv-head">
      <div class="ba-rv-head-left">
        <button class="ba-rv-back" type="button"
                onclick="pycmd('ba:decks')"
                title="Back to decks (Esc)"
                aria-label="Back to decks">‹</button>
        <span class="ba-rv-deck" title="{name}">{name}</span>
      </div>
      <div class="ba-rv-head-right">
        <span class="ba-rv-counts">
          <span class="ba-rv-count ba-rv-c-new"
                title="New cards still to study"><b id="ba-rv-c-new">{new_n}</b><i>new</i></span>
          <span class="ba-rv-count ba-rv-c-learn"
                title="Cards in learning"><b id="ba-rv-c-learn">{learn_n}</b><i>learn</i></span>
          <span class="ba-rv-count ba-rv-c-due"
                title="Review cards due today"><b id="ba-rv-c-due">{rev_n}</b><i>due</i></span>
        </span>
        <button class="ba-rv-icon-btn" type="button"
                onclick="pycmd('edit')" title="Edit card (E)"
                aria-label="Edit card">{edit_svg}</button>
        <button class="ba-rv-icon-btn" type="button"
                onclick="pycmd('more')" title="More options (M)"
                aria-label="More options">{more_svg}</button>
      </div>
    </header>
    """


def _ease_intervals() -> Optional[Dict[int, str]]:
    """Return {ease: interval_str} for the current card's next-state
    intervals (e.g., {1: "<1m", 2: "<10m", 3: "6d", 4: "13d"}). Returns
    None if anything goes wrong — caller hides the chips in that case."""
    try:
        rv = mw.reviewer
        if rv is None or rv.card is None:
            return None
        labels = mw.col.sched.describe_next_states(rv._v3.states)
        # `labels` order matches Anki's _answerButtonList: Again, [Hard,]
        # Good, [Easy]. Map back to ease number via the button list.
        buttons = rv._answerButtonList()
        out: Dict[int, str] = {}
        for (ease, _name), interval in zip(buttons, labels):
            out[int(ease)] = str(interval)
        return out
    except Exception:
        return None


def _push_progress() -> None:
    if not _config().get("show_progress", True):
        return
    rem = _remaining()
    if rem > _session["total"]:
        _session["total"] = rem
    total = _session["total"] or 1
    done = max(0, total - rem)
    pct = min(100, int(done * 100 / total))
    new_n = learn_n = rev_n = 0
    try:
        c = mw.col.sched.counts()
        new_n, learn_n, rev_n = int(c[0]), int(c[1]), int(c[2])
    except Exception:
        pass
    intervals = _ease_intervals() or {}
    default_ease = 3
    try:
        default_ease = int(mw.reviewer._defaultEase())
    except Exception:
        pass
    # Reviewer state is authoritative — `mw.reviewer.state` is "question" or
    # "answer". Pass it explicitly so JS can drive ease-selector visibility
    # without relying on `<hr id=answer>` (which Image Occlusion and any
    # back template that doesn't start with `{{FrontSide}}` don't emit).
    is_answer = False
    try:
        is_answer = getattr(mw.reviewer, "state", "question") == "answer"
    except Exception:
        pass
    import json as _json
    intervals_js = _json.dumps(intervals)
    is_answer_js = "true" if is_answer else "false"
    try:
        mw.reviewer.web.eval(
            f"window.__reforgeProgress && window.__reforgeProgress({pct},{done},{rem});"
            f"var $=function(id){{return document.getElementById(id);}};"
            f"var cn=$('ba-rv-c-new'),cl=$('ba-rv-c-learn'),cd=$('ba-rv-c-due');"
            f"if(cn)cn.textContent={new_n};if(cl)cl.textContent={learn_n};"
            f"if(cd)cd.textContent={rev_n};"
            f"window.__baSetEase && window.__baSetEase({intervals_js}, {default_ease}, {is_answer_js});"
        )
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# "Hide Answer" — H key
#
# Same action as the bottom-left button: back the current card out to its
# question side, ungraded. Registered as an *additional* review-state
# shortcut (not a rewrite, unlike editreviewer's "e") — checked against
# aqt/reviewer.py's Reviewer._shortcutKeys(): "h" isn't one of Anki's
# defaults (e/space/return/m/r/F5/Ctrl+<flag>/*/=/-/!/@/the answer keys/
# u/5/6/7/Shift+A), so this adds rather than collides. Left *enabled* only
# while the answer is on screen — toggled from on_show_question/
# on_show_answer below, mirroring editreviewer.set_edit_active's use of
# `mw.stateShortcuts`.
# --------------------------------------------------------------------------- #
def _hide_answer_shortcut() -> None:
    if not _config().get("reviewer_hide_answer", True):
        return
    rv = getattr(mw, "reviewer", None)
    if rv is None or getattr(rv, "card", None) is None:
        return
    if getattr(rv, "state", "") != "answer":
        return
    try:
        rv._showQuestion()
    except Exception:
        pass


def _shortcut_key_taken(shortcuts: list, key: str) -> bool:
    for item in shortcuts:
        if not isinstance(item, tuple) or len(item) != 2:
            continue
        existing, _fn = item
        if isinstance(existing, str) and existing.lower() == key:
            return True
    return False


def _on_reviewer_state_shortcuts_will_change(state: str, shortcuts: list) -> None:
    """Add our review-state keys to Anki's own table rather than rewriting
    it. Both are checked against Reviewer._shortcutKeys() (aqt/reviewer.py)
    and against each other's neighbours here, and skipped entirely if
    something else already claims the letter."""
    if state != "review":
        return
    try:
        if _config().get("reviewer_hide_answer", True) and not _shortcut_key_taken(
            shortcuts, "h"
        ):
            shortcuts.append(("h", _hide_answer_shortcut))
        if _config().get("reviewer_prev_card", True) and not _shortcut_key_taken(
            shortcuts, "p"
        ):
            shortcuts.append(("p", _prev_card_shortcut))
    except Exception:
        pass


def _hide_answer_qshortcut():
    """The QShortcut Anki built for our "H" tuple above, if it exists yet
    (it's created once per entry into the review state)."""
    try:
        from aqt.qt import QKeySequence
        for sc in getattr(mw, "stateShortcuts", []) or []:
            if sc.key() == QKeySequence("h"):
                return sc
    except Exception:
        pass
    return None


def _set_hide_answer_shortcut_enabled(on: bool) -> None:
    sc = _hide_answer_qshortcut()
    if sc is not None:
        try:
            sc.setEnabled(bool(on))
        except Exception:
            pass


# --------------------------------------------------------------------------- #
# "‹ Previous" — P key
#
# Step back to the card you just answered so you can change the grade. This
# is deliberately *not* a bespoke re-queue: it's Anki's own review undo
# (mw.undo() → aqt.operations.collection.undo → col.undo()), which pops the
# AnswerCard entry and restores that card's pre-answer row wholesale — ivl,
# due, factor, reps, lapses, queue/type, and the revlog entry it wrote — then
# puts it back at the head of the queue. The reviewer's op_executed sees
# study_queues change and calls nextCard(), so the card comes back in its
# *question* state and the user re-grades it forward from there. Successive
# presses walk back through consecutive AnswerCard entries.
#
# The whole feature therefore hinges on one question: is the top of the undo
# stack an answer? col.undo_status().undo is the localized description of
# that pending op (rslib/src/ops.rs: Op::AnswerCard → tr.actions_answer_card),
# so we compare it against the same translation rather than a hardcoded
# English string. If the head is anything else — a note the user just added,
# a deck switch, an empty stack — the button is dimmed and inert and the key
# does nothing, because undoing *that* from a "Previous card" control would
# silently destroy unrelated work.
#
# "p" is free: it's not in Reviewer._shortcutKeys() (aqt/reviewer.py), and
# the add-on's own bare-key bindings are "," (settings), "e" (inline edit)
# and "h" (hide answer) — cmdk only takes Ctrl/Cmd+P. "," was the first
# choice but is already ours, so "p" it is. Registered per-state alongside
# "h"; unlike "h" it stays enabled and gates on _can_prev() at press time,
# since it's live on both sides of the card.
# --------------------------------------------------------------------------- #
def _answer_card_op_name() -> str:
    """Anki's own label for the AnswerCard undo entry, in the user's
    language. Falls back to the English string only if the translation
    lookup itself fails."""
    col = getattr(mw, "col", None)
    if col is not None:
        try:
            return str(col.tr.actions_answer_card()).strip()
        except Exception:
            pass
    try:
        from aqt.utils import tr as _tr
        return str(_tr.actions_answer_card()).strip()
    except Exception:
        return "Answer Card"


def _can_prev() -> bool:
    """True when the pending undo operation is an answered card, i.e. when
    "Previous" has somewhere to go."""
    try:
        if not _config().get("reviewer_prev_card", True):
            return False
        col = getattr(mw, "col", None)
        if col is None:
            return False
        rv = getattr(mw, "reviewer", None)
        if rv is None or getattr(rv, "card", None) is None:
            return False
        pending = (getattr(col.undo_status(), "undo", "") or "").strip()
        if not pending:
            return False  # bottom of the stack
        return pending == _answer_card_op_name()
    except Exception:
        return False


def _push_prev_state() -> None:
    """Sync the button's enabled state to the undo queue. Cheap enough to
    call on every card render and after every collection op — undo_status()
    is a single backend read.

    Nothing in here may raise. One caller is Anki's operation_did_execute,
    which runs inside CollectionOp's completion path (aqt/operations/
    __init__.py: on_op_finished), and an exception escaping there would
    strand whatever op just finished — so the config read is inside the
    guard too, not just the backend call."""
    try:
        if not _config().get("reviewer_prev_card", True):
            return
        if getattr(mw, "state", "") != "review":
            return
        on = "true" if _can_prev() else "false"
        mw.reviewer.web.eval(f"window.__baSetPrev && window.__baSetPrev({on});")
    except Exception:
        pass


def _prev_card_shortcut() -> None:
    if not _can_prev():
        return
    try:
        mw.undo()
    except Exception:
        pass


def _on_operation_did_execute(changes, handler) -> None:
    """Anything that touches the collection can change what's at the head of
    the undo queue — adding a note mid-review, setting a due date, burying.
    reviewer_did_show_question doesn't fire for those (the card on screen
    doesn't change), so the button would keep a stale enabled state without
    this. Deliberately total: this runs inside CollectionOp's completion
    path, so it must never raise."""
    try:
        _push_prev_state()
    except Exception:
        pass


def on_show_question(card) -> None:
    _push_progress()
    _push_prev_state()
    _set_hide_answer_shortcut_enabled(False)


def on_show_answer(card) -> None:
    _push_progress()
    _push_prev_state()
    _set_hide_answer_shortcut_enabled(True)


def on_reviewer_will_end() -> None:
    _session["total"] = 0
    # Leaving the reviewer ends the transient queue with it — see
    # `release_study_queues`. Synchronously, on purpose: this hook runs
    # during `moveToState`, *before* the incoming state renders, so the deck
    # list we're about to draw counts the cards in the decks they've just
    # gone home to. Deferring by a tick would paint the stale zeroes first.
    release_study_queues()


def on_reviewer_will_answer_card(proceed_ease, reviewer, card):
    """Kick off the press-feedback animation in the reviewer webview.
    Fires on both keyboard (1–4) and click paths since both flow through
    ``Reviewer._answerCard``. The eval is queued and runs before Anki's
    next-card ``_updateQA`` (which Anki also queues, after our handler
    returns), so the ghost + bloom appear immediately and keep playing
    while the new card slides in beneath. Pure side effect — we return
    ``proceed_ease`` unchanged."""
    try:
        proceed, ease = proceed_ease
        if proceed and reviewer is not None and getattr(reviewer, "web", None):
            reviewer.web.eval(
                f"window.__baEaseFx && window.__baEaseFx({int(ease)});"
            )
    except Exception:
        pass
    return proceed_ease


# --------------------------------------------------------------------------- #
# Register hooks
# --------------------------------------------------------------------------- #
gui_hooks.webview_will_set_content.append(on_webview_will_set_content)
gui_hooks.deck_browser_will_render_content.append(on_deck_browser_will_render_content)
gui_hooks.deck_browser_did_render.append(on_deck_browser_did_render)
# Expand-all patch applied at import time; idempotent.
_patch_deck_tree_always_expanded()
# Svelte pages (congrats) bypass webview_will_set_content; this hook fires
# after every page's dynamic styling finishes, so we use it to detect the
# congrats URL and inject our redesign.
try:
    gui_hooks.webview_did_inject_style_into_page.append(
        on_webview_did_inject_style_into_page
    )
except Exception:
    pass
gui_hooks.state_did_change.append(on_state_did_change)
gui_hooks.reviewer_did_show_question.append(on_show_question)
gui_hooks.reviewer_did_show_answer.append(on_show_answer)
gui_hooks.reviewer_will_end.append(on_reviewer_will_end)
# A queue can only outlive its session if Anki didn't get to shut down
# cleanly. Sweep on open, before the deck list is drawn, so a crash mid-review
# doesn't leave a chunk of the collection filed under "Study: Due" forever.
gui_hooks.profile_did_open.append(release_study_queues)
try:
    gui_hooks.reviewer_will_answer_card.append(on_reviewer_will_answer_card)
except Exception:
    pass
try:
    gui_hooks.state_shortcuts_will_change.append(
        _on_reviewer_state_shortcuts_will_change
    )
except Exception:
    pass
# Keeps "‹ Previous" honest about what's actually at the head of the undo
# queue after ops that don't swap the card (add note, set due date, bury).
try:
    gui_hooks.operation_did_execute.append(_on_operation_did_execute)
except Exception:
    pass

# Browser ("Browse" tab) — Qt-side chrome restyle, trimmed sidebar, and the
# rendered-card preview pane. Every piece is config-gated inside
# browse_style / browse_preview, so a failure here can only ever cost us
# Anki's stock browser look, never the browser itself.
try:
    from . import browse_style as _browse_style

    gui_hooks.browser_will_show.append(_browse_style.apply)
    gui_hooks.browser_will_build_tree.append(
        _browse_style.on_browser_will_build_tree
    )
    gui_hooks.theme_did_change.append(_browse_style.on_theme_did_change)
except Exception:
    pass

# "New subsection…" on a deck's right-click menu in the Browse sidebar —
# the same action the deck list's gear menu carries.
try:
    from . import subsections as _subsections

    gui_hooks.browser_sidebar_will_show_context_menu.append(
        _subsections.on_browser_sidebar_context_menu
    )
except Exception:
    pass

# Browse sidebar: multi-select without a mode. Installed per Browser (the
# tree is built in `Browser.setupSidebar`), plus "Move to…" on the
# right-click menu for whatever is selected.
try:
    from . import sidebar_select as _sidebar_select

    gui_hooks.browser_will_show.append(_sidebar_select.install)
    gui_hooks.browser_sidebar_will_show_context_menu.append(
        _sidebar_select.on_sidebar_context_menu
    )
except Exception:
    pass

# Reviewer "More" menu: Randomize Set + the Cue… tag submenu.
try:
    from . import reviewer_menu as _reviewer_menu

    gui_hooks.reviewer_will_show_context_menu.append(
        _reviewer_menu.on_will_show_context_menu
    )
    # Right-click on a selection inside a card → look it up.
    gui_hooks.webview_will_show_context_menu.append(
        _reviewer_menu.on_webview_will_show_context_menu
    )
except Exception:
    pass

# Per-deck memorization deadlines. Two refresh paths, both idempotent:
#
#   * `refresh_all` recomputes every deadlined deck's interval ceiling once
#     a day (it no-ops once it's already run for Anki's current day) —
#     profile open covers the normal case (Anki restarted overnight); the
#     reviewer entry covers the app being left running across a rollover.
#   * `check_due_transitions` is the event-driven counterpart that makes a
#     deadline's cap actually come off within moments of the deadline
#     *moment* (not just the calendar date) — wired to every state change
#     and every deck-list render, plus a single-shot QTimer (armed by the
#     deadlines module itself for the next upcoming deadline) so it still
#     fires even if the user never triggers either of those in between.
#     It's cheap when nothing has crossed: a couple of dict lookups per
#     deadlined deck, no config writes, no polling loop anywhere in it.
try:
    from . import deadlines as _deadlines

    def _deadlines_on_profile_open() -> None:
        # One-time repair of three decks' `deck_deadlines` bookkeeping that
        # was already corrupted before today's fix (see the function's own
        # docstring in deadlines.py) — must run before the two calls below,
        # since it's what makes their "orig_*" restore targets correct.
        _deadlines.repair_legacy_corruption()
        # Un-park "— deadline" clone presets an older build left at 0/day
        # (one-time, guarded by its own flag). Before the mode migration:
        # that migration pauses decks by their *own* limits, and a preset
        # still stuck at 0 underneath would make a later resume a no-op.
        _deadlines.repair_zeroed_clone_presets()
        # One-time: decks whose deadline already passed under the old
        # implicit "maintain" default now follow `deadline_passed_mode`
        # ("pause"). Guarded by its own dated flag, and it never touches a
        # deck that explicitly chose maintain after this shipped — see the
        # eligibility notes on `migrate_passed_mode_default`.
        _deadlines.migrate_passed_mode_default()
        _deadlines.refresh_all()
        _deadlines.check_due_transitions()

    gui_hooks.profile_did_open.append(_deadlines_on_profile_open)

    def _deadlines_on_state(new_state: str, old_state: str) -> None:
        if new_state == "review":
            _deadlines.refresh_all()
        _deadlines.check_due_transitions()

    gui_hooks.state_did_change.append(_deadlines_on_state)

    gui_hooks.deck_browser_will_render_content.append(
        lambda _deck_browser, _content: _deadlines.check_due_transitions()
    )
except Exception:
    pass

# Sidebar nav: route `ba:*` pycmds to the right mw methods + settings dialog.
gui_hooks.webview_did_receive_js_message.append(_on_js_message)


def _embed_enabled(key: str) -> bool:
    """Inline windows (Add / Browse / Stats / Settings) live to the right
    of the sidebar rail, so they only make sense while the sidebar is on;
    each also has its own Settings toggle."""
    cfg = _config()
    if not cfg.get("sidebar_nav", True):
        return False
    return bool(cfg.get(key, True))


def _open_add(*args: Any, **kwargs: Any) -> None:
    """Add Cards — reached through Browse now, not a rail item of its own.

    The left-nav no longer has a standalone "Add" entry; the "+ Add"
    button inside Browse is the visible entry point. This function still
    exists because it's what the "A" shortcut calls (see
    `_setup_sidebar_shortcuts`) and what `mw.onAddCard` is patched to —
    both are muscle-memory paths that should keep working, they just now
    land the user in the same place the "+ Add" button does.

    When both the Browse and Add embeds are enabled (the default), open
    the embedded Browse and turn its CARD panel into the add-a-note form
    via `browse_embed._open_add_in_panel` — the exact same call the
    "+ Add" button makes, no parallel implementation. That function falls
    back to the old stacked-overlay Add (`_open_add_in_browse`) on its
    own if the panel isn't available for some reason, so nothing extra is
    needed here for that case. If Browse's embed is off (but Add's
    isn't), there's nothing to open a panel inside, so fall back to the
    old full-pane inline Add. If neither embed is enabled, fall back to
    Anki's own Add Cards window."""
    try:
        if _embed_enabled("embed_add") and _config().get("restyle_addcard", True):
            try:
                from . import addcard_embed
                if _embed_enabled("embed_browse"):
                    from . import browse_embed
                    browse_embed.open_inline(mw)
                    if browse_embed._state.get("overlay") is not None:
                        browse_embed._open_add_in_panel(mw)
                        return
                    # Browse's embed didn't come up for some reason — fall
                    # through to a plain full-pane Add rather than doing
                    # nothing.
                addcard_embed.open_inline(mw)
                return
            except Exception:
                pass
        orig = getattr(mw, "_ba_orig_on_add_card", None)
        if orig is not None:
            orig(*args, **kwargs)
            return
        try:
            from aqt import dialogs
            dialogs.open("AddCards", mw)
        except Exception:
            pass
    finally:
        # Belt-and-braces for the embed paths above (open_inline/
        # _open_add_in_panel push their own occlusion state on their own
        # exit too, via _open_browse/_open_settings-style tails elsewhere)
        # — cheap, idempotent, and covers the orig()/dialogs.open()
        # fallbacks, which are real top-level windows this function
        # itself never needs to occlude-push for but which may be
        # closing an embed that was open a moment ago.
        _push_video_occlusion()


def _open_browse(*args: Any, **kwargs: Any) -> None:
    """Browse — inline embed when enabled, else Anki's own window."""
    try:
        if _embed_enabled("embed_browse"):
            try:
                from . import browse_embed
                browse_embed.open_inline(mw)
                return
            except Exception:
                pass
        orig = getattr(mw, "_ba_orig_on_browse", None)
        if orig is not None:
            orig(*args, **kwargs)
            return
        try:
            from aqt import dialogs
            dialogs.open("Browser", mw)
        except Exception:
            pass
    finally:
        _push_video_occlusion()


def _open_stats(*args: Any, **kwargs: Any) -> None:
    """Stats — inline embed when enabled, else Anki's own window. Shift+T
    (legacy DeckStats) always falls through to Anki."""
    orig = getattr(mw, "_ba_orig_on_stats", None)
    try:
        try:
            from aqt.utils import KeyboardModifiersPressed
            if KeyboardModifiersPressed().shift and orig is not None:
                orig(*args, **kwargs)
                return
        except Exception:
            pass
        if _embed_enabled("embed_stats"):
            try:
                from . import stats_embed
                stats_embed.open_inline(mw)
                return
            except Exception:
                pass
        if orig is not None:
            orig(*args, **kwargs)
            return
        try:
            from aqt import dialogs
            dialogs.open("NewDeckStats", mw)
        except Exception:
            pass
    finally:
        _push_video_occlusion()


def _open_prefs(*args: Any, **kwargs: Any) -> None:
    """Preferences — always lands on the Anki Design tab; inline when the
    settings embed is enabled."""
    try:
        _open_settings()
    except Exception:
        orig = getattr(mw, "_ba_orig_on_prefs", None)
        if orig is not None:
            orig(*args, **kwargs)


# Sidebar shortcuts — make the keys hinted in the sidebar (A, D, comma) do
# what the user expects.
#
# Anki binds `a`/`d`/`b`/`t`/`y`/`s` as global shortcuts in aqt/main.py.
# We can't unbind those (they're created as QShortcut objects on `mw` at
# startup), but we CAN intercept the functions they call. So:
#   - A / B / T → patch onAddCard / onBrowse / onStats to open our inline
#     embeds (each checks its Settings toggle at call time and falls back
#     to Anki's standalone window).
#   - D → moveToState("deckBrowser"): patch to also tear down the embed
#     if it's currently open, so the deck-browser UI actually comes back.
#   - Plain `,` → settings: Anki has no binding for `,` at all, so add a
#     QShortcut for it.
def _setup_sidebar_shortcuts() -> None:
    # Patch the mw entry points so the Tools menu, toolbar, and any code
    # path that goes through them opens the inline embed instead of the
    # standalone window. Originals are stashed on mw so the openers can
    # fall back to them when a toggle is off.
    for attr, stash, fn in (
        ("onAddCard", "_ba_orig_on_add_card", _open_add),
        ("onBrowse", "_ba_orig_on_browse", _open_browse),
        ("onStats", "_ba_orig_on_stats", _open_stats),
        ("onPrefs", "_ba_orig_on_prefs", _open_prefs),
    ):
        try:
            if getattr(mw, stash, None) is None:
                setattr(mw, stash, getattr(mw, attr))
            setattr(mw, attr, fn)
        except Exception:
            pass

    # The global shortcuts are bound by aqt/main.py:setupKeys via
    # `("a", self.onAddCard)` — a bound-method reference captured BEFORE
    # we monkey-patch. The QShortcut holds the original bound method, so
    # the patch above doesn't catch it. Find the QShortcut children whose
    # keys match, disconnect the existing activation, and reconnect to
    # our openers.
    try:
        from aqt.qt import QShortcut, QKeySequence
        targets = {
            QKeySequence("a").toString(): _open_add,
            QKeySequence("b").toString(): _open_browse,
            QKeySequence("t").toString(): _open_stats,
        }
        for sc in mw.findChildren(QShortcut):
            try:
                fn = targets.get(sc.key().toString())
                if fn is None:
                    continue
                try:
                    sc.activated.disconnect()
                except Exception:
                    pass
                sc.activated.connect(fn)
            except Exception:
                continue
    except Exception:
        pass

    # Patch moveToState so leaving the deck-area context (e.g., D from
    # inside an inline embed) closes the embed before the navigation.
    try:
        _orig_move_to_state = mw.moveToState

        def _patched_move_to_state(state, *args, **kwargs):
            for mod in (
                "addcard_embed", "browse_embed", "stats_embed", "settings_embed",
            ):
                try:
                    from importlib import import_module
                    import_module("." + mod, __name__).close_inline()
                except Exception:
                    pass
            _push_video_occlusion()
            return _orig_move_to_state(state, *args, **kwargs)

        mw.moveToState = _patched_move_to_state  # type: ignore[assignment]
    except Exception:
        pass

    # Plain comma → settings. Anki uses Ctrl/Cmd+, for preferences (already
    # wired by _add_tools_menu_action); this adds the bare-key version that
    # the sidebar hints at.
    try:
        from aqt.qt import QShortcut, QKeySequence
        sc = QShortcut(QKeySequence(","), mw)
        sc.setAutoRepeat(False)
        sc.activated.connect(_open_settings)
    except Exception:
        pass

    # Cmd-K / Ctrl-K — Command palette. cmdk.js already binds the hotkey
    # inside every themed webview (deck browser, overview, reviewer), but
    # when focus is on a Qt-native widget (Add Cards embed editor field,
    # Browser table, a dialog) the webview never sees the keydown. Bind a
    # Qt-level shortcut on mw so the palette is reachable from anywhere.
    def _open_cmdk_palette() -> None:
        if not _config().get("cmdk", True):
            return
        try:
            from . import cmdk as _cmdk
            _cmdk.open_from_outside("")
        except Exception:
            pass
    try:
        from aqt.qt import QShortcut, QKeySequence, Qt
        for seq in ("Ctrl+K", "Meta+K", "Ctrl+Shift+P", "Meta+Shift+P"):
            try:
                sc = QShortcut(QKeySequence(seq), mw)
                sc.setAutoRepeat(False)
                sc.setContext(Qt.ShortcutContext.ApplicationShortcut)
                sc.activated.connect(_open_cmdk_palette)
            except Exception:
                continue
    except Exception:
        pass


gui_hooks.main_window_did_init.append(_setup_sidebar_shortcuts)

# Sync status indicator — show pending/full when there are changes to push,
# and a soft pulse while a sync is in progress.
try:
    gui_hooks.sync_will_start.append(lambda *a: _push_sidebar_sync("active"))
    gui_hooks.sync_did_finish.append(lambda *a: _refresh_sync_status())
except Exception:
    pass

# Silent sync (no modal progress dialog, no post-sync tooltip). Patches
# mw._sync_collection_and_media, so it needs mw to exist — install on
# main_window_did_init.
try:
    gui_hooks.main_window_did_init.append(lambda *a: _install_silent_sync())
except Exception:
    pass

# Hide Anki's top toolbar webview as soon as the main window / profile is up.
gui_hooks.main_window_did_init.append(_apply_chrome)
gui_hooks.profile_did_open.append(_apply_chrome)

# Re-tag the toolbar after Anki rebuilds it (e.g. sync-status redraw), so the
# active-section highlight isn't lost. Optional — guarded per add-on policy.
try:
    gui_hooks.top_toolbar_did_redraw.append(lambda tb: _mark_toolbar_state())
except Exception:
    pass

# Inject an "Anki Design" tab into Anki's native Preferences dialog so every
# entry point — including the Tools-menu "Preferences…" / app-menu shortcut
# the user already knows — surfaces our settings alongside Anki's own.
try:
    from .settings import install_into_preferences
    install_into_preferences()
except Exception:
    pass

# Anki add-on dialog "Config" → open Preferences on the Anki Design tab
# instead of dumping raw JSON in front of the user.
try:
    mw.addonManager.setConfigAction(__name__, _open_settings)
except Exception:
    pass


def _add_tools_menu_action() -> None:
    try:
        from aqt.qt import QAction, QKeySequence, QShortcut, Qt
        act = QAction("Anki Design Settings…", mw)
        # Cmd+, on macOS / Ctrl+, elsewhere — the canonical "preferences" key.
        act.setShortcut(QKeySequence("Ctrl+,"))
        # Use the enum (NOT a literal int — the values differ between Qt5/6).
        act.setShortcutContext(Qt.ShortcutContext.ApplicationShortcut)
        act.triggered.connect(_open_settings)
        mw.form.menuTools.addAction(act)
        # Belt-and-braces: also register a global QShortcut on the main
        # window so the key fires regardless of focus.
        sc = QShortcut(QKeySequence("Ctrl+,"), mw)
        sc.setContext(Qt.ShortcutContext.ApplicationShortcut)
        sc.activated.connect(_open_settings)
        # And a macOS Cmd+, equivalent (some Qt builds need it explicitly).
        sc2 = QShortcut(QKeySequence("Meta+,"), mw)
        sc2.setContext(Qt.ShortcutContext.ApplicationShortcut)
        sc2.activated.connect(_open_settings)
    except Exception as e:
        try:
            from aqt.utils import showWarning
            showWarning(f"Anki Design: failed to register settings shortcut: {e}")
        except Exception:
            pass


gui_hooks.main_window_did_init.append(_add_tools_menu_action)


# Add Card window redesign — separate module so the file stays focused.
try:
    from . import addcard as _addcard
    _addcard.register()
except Exception as _e:
    try:
        print(f"[anki-design] addcard register failed: {_e}", flush=True)
    except Exception:
        pass


# Reversible cards — Browse toggle to present a card's back as the
# question and its front as the answer. Registered BEFORE editreviewer
# below so its card_will_show handler runs first in the chain: it swaps
# in the other side's raw render, then editreviewer's field-wrap runs on
# that (already-swapped) text, so inline editing still works on reversed
# cards.
try:
    from . import card_reverse as _card_reverse
    _card_reverse.register()
except Exception as _e:
    try:
        print(f"[anki-design] card_reverse register failed: {_e}", flush=True)
    except Exception:
        pass


# Inline reviewer editing — replaces the EditCurrent dialog.
try:
    from . import editreviewer as _editreviewer
    _editreviewer.register()
except Exception as _e:
    try:
        print(f"[anki-design] editreviewer register failed: {_e}", flush=True)
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# Dev hot-reload — web/ assets only, enabled by `make dev` (a .devmode file).
#
# Hard rule: the watcher must NEVER touch Anki during shutdown. Refreshing a
# view there fires Anki's own signals after the collection is gone, and those
# errors surface OUTSIDE our try/except (in Anki's slots) -> the "may be
# caused by an add-on" dialog that blocks quitting. So the thread is tied to
# the profile lifecycle: it starts only once the profile is open and is
# stopped on profile_will_close, before any teardown.
#
# Never ships: build.py and .gitignore exclude .devmode, so end users (who
# install the zip, no .devmode) never start the watcher thread.
#
# .devmode is necessary but NOT sufficient — see _dev_profile_allowed below.
# --------------------------------------------------------------------------- #
ADDON_SRC = os.path.dirname(os.path.abspath(__file__))

_dev_stop = threading.Event()
_dev_thread: Optional[threading.Thread] = None


# --------------------------------------------------------------------------- #
# Which profiles may arm the dev channel
# --------------------------------------------------------------------------- #
# A `.devmode` file is a property of the *source tree*, and a source tree can
# be loaded by more than one Anki at once: this repo is commonly symlinked
# into a real profile's addons21 as well as into the dev/demo bases. When it
# is, `make demo` touching .devmode armed the command channel inside the
# user's LIVE Anki too — same file, same watcher, same `.context/cmd` — and
# scripted probes meant for a throwaway demo executed against their real
# collection. That happened repeatedly, so the flag alone no longer decides.
#
# The base directory does. Every dev and demo instance is launched with
# `-b .../Anki2-dev/<name>`; a normal install lives in `.../Anki2`. So the
# channel arms only for a base under Anki2-dev (or one named like a demo
# profile), or when someone deliberately sets ANKI_DESIGN_DEV=1. Anything
# else — most importantly a real profile — refuses, .devmode or not.
_DEV_BASE_PARENT = "Anki2-dev"
_DEV_DEMO_PREFIX = "anki-design-demo"
_DEV_ENV_FLAG = "ANKI_DESIGN_DEV"

_dev_profile_ok: Dict[str, Optional[bool]] = {"v": None}


def _dev_profile_allowed() -> bool:
    """True if THIS Anki's profile is one the dev channel may drive."""
    cached = _dev_profile_ok["v"]
    if cached is not None:
        return cached
    if os.environ.get(_DEV_ENV_FLAG) == "1":
        _dev_profile_ok["v"] = True
        return True
    try:
        base = os.path.abspath(mw.pm.base)
    except Exception:
        # Profile not up yet (add-ons import before it opens). Refuse
        # rather than guess, and don't cache — the answer isn't knowable
        # yet, and every caller re-checks.
        return False
    if not base:
        return False
    ok = (
        os.path.basename(os.path.dirname(base)) == _DEV_BASE_PARENT
        or os.path.basename(base).startswith(_DEV_DEMO_PREFIX)
    )
    _dev_profile_ok["v"] = ok
    if not ok and os.path.exists(os.path.join(ADDON_SRC, ".devmode")):
        # Worth saying out loud: someone armed the tree and is running a
        # profile that won't honour it. Silence here reads as a broken
        # dev channel and sends you hunting in the wrong place.
        print(
            f"[anki-design] .devmode ignored: profile base {base!r} is not a "
            f"dev/demo base. Set {_DEV_ENV_FLAG}=1 to override.",
            flush=True,
        )
    return ok


def _dev_active() -> bool:
    return os.path.exists(
        os.path.join(ADDON_SRC, ".devmode")
    ) and _dev_profile_allowed()


def _dev_reload_views() -> None:
    """Runs on the Qt main thread. Bails unless a collection is open and we
    are not shutting down. Cache-busts our stylesheets in every webview
    (instant, no flicker), then re-renders the current screen."""
    if _dev_stop.is_set() or not _dev_active():
        return
    if mw is None or getattr(mw, "col", None) is None:
        return  # no profile / mid-shutdown — never refresh here
    state = getattr(mw, "state", None)
    if state not in ("deckBrowser", "overview", "review"):
        return

    bust = (
        "(function(){var v=Date.now();"
        "var ls=document.getElementsByTagName('link');"
        "for(var i=0;i<ls.length;i++){var l=ls[i];"
        "if(l.rel==='stylesheet'&&l.href.indexOf('/_addons/%s/web/')!==-1)"
        "{l.href=l.href.split('?')[0]+'?v='+v;}}})();" % ADDON_DIR
    )
    views = []
    for attr in ("web", "bottomWeb"):
        w = getattr(mw, attr, None)
        if w is not None:
            views.append(w)
    rv = getattr(mw, "reviewer", None)
    if rv is not None:
        if getattr(rv, "web", None) is not None:
            views.append(rv.web)
        bottom = getattr(rv, "bottom", None)
        if bottom is not None and getattr(bottom, "web", None) is not None:
            views.append(bottom.web)
    # Bust the AddCards editor webview too (covers both the standalone
    # AddCards window and our inline embed — both hold a live editor.web
    # that loads addcard.css/addcard.js from /_addons).
    try:
        from aqt import dialogs
        ac = dialogs._dialogs.get("AddCards", [None, None])[1]
        if ac is None:
            try:
                from . import addcard_embed
                ac = addcard_embed._state.get("addcards")
            except Exception:
                ac = None
        if ac is not None and getattr(ac, "editor", None):
            w = getattr(ac.editor, "web", None)
            if w is not None:
                views.append(w)
    except Exception:
        pass
    for w in views:
        try:
            w.eval(bust)
        except Exception:
            pass

    try:
        if state == "deckBrowser":
            mw.deckBrowser.refresh()
        elif state == "overview":
            mw.overview.refresh()
        elif state == "review":
            try:
                with open(os.path.join(ADDON_SRC, "web", "reviewer.js")) as fh:
                    mw.reviewer.web.eval(fh.read())
            except Exception:
                pass
            _push_progress()
    except Exception:
        pass


def _dev_screenshot(request_path: str) -> None:
    """Runs on the Qt main thread. Reads the JSON request file, finds the
    target Qt widget (by window-title substring or "main"), grabs it as a
    PNG and writes to the requested output path. Always removes the request
    file. Used by the iterative-design screenshot loop."""
    import json
    try:
        with open(request_path) as fh:
            req = json.load(fh)
    except Exception:
        try:
            os.remove(request_path)
        except Exception:
            pass
        return
    try:
        os.remove(request_path)
    except Exception:
        pass
    out = req.get("out")
    target_title = (req.get("title") or "").lower()
    open_addcards = bool(req.get("open_addcards"))
    if not out:
        return
    try:
        from aqt.qt import QApplication, QTimer
        embed_add = bool(req.get("embed_add"))
        if open_addcards:
            try:
                if embed_add:
                    from . import addcard_embed
                    addcard_embed.open_inline(mw)
                else:
                    mw.onAddCard()
            except Exception:
                pass
        # Optional: pre-fill the fields with sample text so we can validate
        # how the design looks with content (vs the empty/placeholder state).
        fill_sample = bool(req.get("fill_sample"))
        try:
            QApplication.processEvents()
        except Exception:
            pass

        close_after = bool(req.get("close_after"))
        test_add = bool(req.get("test_add"))
        if test_add:
            # End-to-end smoke test: fill the editor, click Add, count
            # before/after, write the result to <out>.txt next to the PNG.
            from aqt.qt import QTimer as _TT

            def _smoke() -> None:
                # Simulate two consecutive adds to reproduce the user's
                # "second click crashes" report. Between clicks Anki
                # re-loads a fresh note; if our flow left dangling state
                # the second add should crash.
                log = []
                try:
                    from . import addcard_embed
                    ac = addcard_embed._state.get("addcards")
                    if ac is None:
                        return
                    col = ac.col
                    before = col.note_count()

                    def fill_and_click(tag: str) -> None:
                        ed = ac.editor
                        if ed and ed.note:
                            ed.note.fields[0] = f"front-{tag}"
                            if len(ed.note.fields) > 1:
                                ed.note.fields[1] = f"back-{tag}"
                            try:
                                ed.loadNote()
                            except Exception as e:
                                log.append(f"loadNote-{tag}={e}")
                        try:
                            from aqt.qt import QPushButton as _QPB
                            overlay = addcard_embed._state.get("overlay")
                            if overlay is not None:
                                for child in overlay.findChildren(_QPB):
                                    if child.objectName() == "ba-add":
                                        child.click()
                                        log.append(f"clicked-{tag}=ok")
                                        return
                            log.append(f"no_btn-{tag}")
                        except Exception as e:
                            import traceback
                            log.append(f"click-{tag}-err={e}")
                            try:
                                print(
                                    f"[smoke] click-{tag} failed:\n"
                                    f"{traceback.format_exc()}",
                                    flush=True,
                                )
                            except Exception:
                                pass

                    def _verify() -> None:
                        try:
                            after = col.note_count()
                            with open(out + ".txt", "w") as fh:
                                fh.write(
                                    f"before={before} after={after} "
                                    f"delta={after - before} "
                                    + " ".join(log) + "\n"
                                )
                        except Exception:
                            pass

                    fill_and_click("A")
                    # Wait LONGER than _safe_add's 700ms debounce before
                    # the second click — should be safe now.
                    _TT.singleShot(1200, lambda: fill_and_click("B"))
                    _TT.singleShot(2700, _verify)
                except Exception as e:
                    log.append(f"smoke_err={e}")
            _TT.singleShot(1200, _smoke)
        hover_add = bool(req.get("hover_add"))
        if hover_add:
            # Schedule on a short timer so it runs AFTER the embed has been
            # opened and the layout settled.
            from aqt.qt import QTimer as _QT

            def _hover_add() -> None:
                # Directly trigger the animated reveal — easier than
                # synthesizing a QEnterEvent that reliably reaches the
                # filter through Qt's offscreen-grab path. We give the
                # animation 250ms to finish before the grab happens.
                try:
                    from . import addcard_embed
                    from aqt.qt import QApplication, QTimer as _T2
                    ac = addcard_embed._state.get("addcards")
                    if ac is None:
                        return
                    hover = getattr(ac, "_ba_add_hover", None)
                    if hover is None:
                        return
                    hover._animate_in()
                    QApplication.processEvents()
                    # Force a paint pass after the animation completes
                    # so the next grab() captures the fully-faded-in state.
                    def _post() -> None:
                        try:
                            QApplication.processEvents()
                        except Exception:
                            pass
                    _T2.singleShot(250, _post)
                except Exception:
                    pass
            # Fire the hover early so the 180ms animation has time to
            # complete before the 2000ms grab.
            _QT.singleShot(1000, _hover_add)
        mw_width = req.get("mw_width")
        mw_height = req.get("mw_height")
        if isinstance(mw_width, int) and mw_width > 200:
            try:
                h = mw_height if isinstance(mw_height, int) and mw_height > 200 else mw.height()
                mw.resize(mw_width, h)
                QApplication.processEvents()
            except Exception:
                pass
        elif isinstance(mw_height, int) and mw_height > 200:
            try:
                mw.resize(mw.width(), mw_height)
                QApplication.processEvents()
            except Exception:
                pass
        trigger_shortcut = req.get("trigger_shortcut")  # e.g. "a"
        if trigger_shortcut:
            try:
                from aqt.qt import QShortcut, QKeySequence
                seq = QKeySequence(trigger_shortcut).toString()
                for sc in mw.findChildren(QShortcut):
                    try:
                        if sc.key().toString() == seq:
                            sc.activated.emit()
                            break
                    except Exception:
                        continue
            except Exception:
                pass
        run_js = req.get("run_js")
        if run_js:
            try:
                web = getattr(mw, "web", None)
                if web is not None:
                    web.eval(str(run_js))
                    QApplication.processEvents()
            except Exception:
                pass

        def _grab() -> None:
            widget = None
            if target_title in ("main", "mw"):
                widget = mw
            else:
                for w in QApplication.topLevelWidgets():
                    try:
                        if not w.isVisible():
                            continue
                        title = (w.windowTitle() or "").lower()
                        if target_title and target_title in title:
                            widget = w
                            break
                    except Exception:
                        continue
            if widget is None:
                try:
                    with open(out + ".err", "w") as fh:
                        fh.write(f"no widget for title={target_title!r}\n")
                except Exception:
                    pass
                return
            # No raise_/activateWindow: widget.grab() renders offscreen via Qt's
            # backing store, so the window doesn't need focus or to be on top.
            # Surfacing it would steal focus from whatever the user is doing.
            try:
                QApplication.processEvents()
            except Exception:
                pass
            try:
                pix = widget.grab()
                pix.save(out, "PNG")
            except Exception as e:
                try:
                    with open(out + ".err", "w") as fh:
                        fh.write(f"grab failed: {e}\n")
                except Exception:
                    pass
            if close_after:
                try:
                    from . import addcard_embed
                    addcard_embed.close_inline()
                except Exception:
                    pass

        # Optionally inject sample text into editor fields so we can preview
        # the design with content. We schedule this *before* the grab delay.
        def _fill() -> None:
            try:
                from aqt import dialogs
                ac = dialogs._dialogs.get("AddCards", [None, None])[1]
                if ac is None:
                    try:
                        from . import addcard_embed
                        ac = addcard_embed._state.get("addcards")
                    except Exception:
                        ac = None
                if ac is None or not getattr(ac, "editor", None):
                    return
                ed = ac.editor
                if not getattr(ed, "note", None):
                    return
                samples = [
                    "What is the capital of France?",
                    "Paris — capital and most populous city.",
                    "Located on the Seine river.",
                ]
                for i in range(min(len(ed.note.fields), len(samples))):
                    ed.note.fields[i] = samples[i]
                ed.loadNote()
            except Exception:
                pass
        if fill_sample:
            QTimer.singleShot(400, _fill)
        # Optionally click the toolbar cog to verify the dropdown renders.
        click_cog = bool(req.get("click_cog"))
        def _click_cog() -> None:
            try:
                from aqt import dialogs
                ac = dialogs._dialogs.get("AddCards", [None, None])[1]
                if ac is None:
                    try:
                        from . import addcard_embed
                        ac = addcard_embed._state.get("addcards")
                    except Exception:
                        ac = None
                if ac is None or not getattr(ac, "editor", None):
                    return
                web = ac.editor.web
                # Anki's Svelte popup listens for a real PointerEvent — .click()
                # alone doesn't fire it. Dispatch pointerdown + mousedown + click
                # so the floating-ui library actually toggles the dropdown.
                web.eval("""
                  (function(){
                    var btn = document.querySelector('#settings button');
                    if (!btn) return;
                    function fire(name) {
                      var ev = new MouseEvent(name, {bubbles:true, cancelable:true, view:window, button:0});
                      btn.dispatchEvent(ev);
                    }
                    fire('pointerdown');
                    fire('mousedown');
                    fire('mouseup');
                    fire('click');
                  })();
                """)
            except Exception:
                pass
        if click_cog:
            QTimer.singleShot(700, _click_cog)
        # Optionally trigger the in-page note-type picker so the dropdown is
        # visible in the screenshot.
        click_type = bool(req.get("click_type"))
        def _click_type() -> None:
            try:
                from aqt import dialogs
                from PyQt6.QtWidgets import QPushButton as _QPB
                ac = dialogs._dialogs.get("AddCards", [None, None])[1]
                if ac is None:
                    return
                btns = ac.form.modelArea.findChildren(_QPB)
                if btns:
                    btns[0].click()
            except Exception:
                pass
        if click_type:
            QTimer.singleShot(800, _click_type)

        # Give the WebEngine view time to render (templates load async).
        # 1500ms is conservative; the editor.html bundle plus Svelte hydration
        # can take a beat after window construction.
        delay_ms = int(req.get("delay_ms", 1500))
        QTimer.singleShot(delay_ms, _grab)
    except Exception as e:
        try:
            with open(out + ".err", "w") as fh:
                fh.write(f"screenshot fatal: {e}\n")
        except Exception:
            pass


_SCREENSHOT_DIR = os.path.join(ADDON_SRC, ".context", "screenshot-requests")
_DUMP_DIR = os.path.join(ADDON_SRC, ".context", "dump-requests")


def _dev_dump(request_path: str) -> None:
    """Runs on the Qt main thread. Reads a {out, title} JSON request and
    dumps that window's web HTML to `out`. Used for design inspection."""
    import json
    try:
        with open(request_path) as fh:
            req = json.load(fh)
    except Exception:
        try:
            os.remove(request_path)
        except Exception:
            pass
        return
    try:
        os.remove(request_path)
    except Exception:
        pass
    out = req.get("out")
    target_title = (req.get("title") or "").lower()
    target_kind = (req.get("kind") or "").lower()
    if not out:
        return
    try:
        from aqt.qt import QApplication
        from aqt import dialogs
        try:
            from aqt.qt import QWebEngineView  # type: ignore
        except Exception:
            from PyQt6.QtWebEngineWidgets import QWebEngineView  # type: ignore
        web = None
        widget = None
        # 1) Specific kind: the AddCards editor (in our inline embed the
        # editor lives inside mw, so the topLevelWidgets lookup misses it
        # — find via the dialogs registry or the embed's stash).
        if target_kind == "addcards-editor":
            ac = dialogs._dialogs.get("AddCards", [None, None])[1]
            if ac is None:
                try:
                    from . import addcard_embed
                    ac = addcard_embed._state.get("addcards")
                except Exception:
                    ac = None
            if ac is not None and getattr(ac, "editor", None):
                web = ac.editor.web
        elif target_kind == "stats-embed":
            try:
                from . import stats_embed
                sd = stats_embed._state.get("stats")
                if sd is not None and getattr(sd, "form", None):
                    web = getattr(sd.form, "web", None)
            except Exception:
                pass
        # 2) `main`/`mw`: pin to mw.web. mw has multiple QWebEngineViews
        # (toolbarWeb, web, bottomWeb); findChild() returns the first one
        # constructed (usually the toolbar) which gives a useless 1-line
        # DOM.
        if web is None and target_title in ("main", "mw"):
            widget = mw
            web = getattr(mw, "web", None)
        # 3) Fall back to a top-level window matching `target_title`, then
        # pick the first QWebEngineView under it.
        if web is None:
            for w in QApplication.topLevelWidgets():
                try:
                    if not w.isVisible():
                        continue
                    title = (w.windowTitle() or "").lower()
                    if target_title and target_title in title:
                        widget = w
                        break
                except Exception:
                    continue
            if widget is None:
                with open(out, "w") as fh:
                    fh.write(f"<!-- no widget for title={target_title!r} -->")
                return
            web = widget.findChild(QWebEngineView)
        if web is None:
            with open(out, "w") as fh:
                fh.write("<!-- no QWebEngineView found -->")
            return
        def _write(html: str, out: str = out) -> None:
            try:
                with open(out, "w") as fh:
                    fh.write(html or "")
            except Exception:
                pass
        web.page().toHtml(_write)
    except Exception as e:
        try:
            with open(out, "w") as fh:
                fh.write(f"<!-- dump error: {e} -->")
        except Exception:
            pass


def _dev_watch() -> None:
    web_dir = os.path.join(ADDON_SRC, "web")
    seen: Dict[str, float] = {}
    primed = False
    # _dev_stop.wait() doubles as the sleep AND an instant exit signal.
    while not _dev_stop.is_set() and _dev_active():
        changed = False
        try:
            for name in os.listdir(web_dir):
                path = os.path.join(web_dir, name)
                if not os.path.isfile(path):
                    continue
                mtime = os.path.getmtime(path)
                if seen.get(path) != mtime:
                    if primed:
                        changed = True
                    seen[path] = mtime
        except Exception:
            pass
        primed = True
        if changed and not _dev_stop.is_set():
            try:
                mw.taskman.run_on_main(_dev_reload_views)
            except Exception:
                pass
        # Process pending screenshot requests (one per file in the dir).
        try:
            if os.path.isdir(_SCREENSHOT_DIR):
                for name in sorted(os.listdir(_SCREENSHOT_DIR)):
                    if not name.endswith(".json"):
                        continue
                    req = os.path.join(_SCREENSHOT_DIR, name)
                    try:
                        mw.taskman.run_on_main(
                            lambda p=req: _dev_screenshot(p)
                        )
                    except Exception:
                        pass
        except Exception:
            pass
        # Process pending DOM-dump requests.
        try:
            if os.path.isdir(_DUMP_DIR):
                for name in sorted(os.listdir(_DUMP_DIR)):
                    if not name.endswith(".json"):
                        continue
                    req = os.path.join(_DUMP_DIR, name)
                    try:
                        mw.taskman.run_on_main(lambda p=req: _dev_dump(p))
                    except Exception:
                        pass
        except Exception:
            pass
        _dev_stop.wait(0.5)


def _dev_start() -> None:
    """Start the watcher once a profile is open. Idempotent."""
    global _dev_thread
    if not _dev_active():
        return
    if _dev_thread is not None and _dev_thread.is_alive():
        return
    _dev_stop.clear()
    _dev_thread = threading.Thread(
        target=_dev_watch, name="anki-design-devwatch", daemon=True
    )
    _dev_thread.start()


def _dev_shutdown() -> None:
    """Stop the watcher before Anki tears anything down."""
    _dev_stop.set()


# Dev-only side channel: write a single line to .context/cmd to drive the UI
# from outside the GUI process (used by the screenshot/iteration workflow).
# Commands:
#   review:<did>      - enter review mode for that deck id
#   decks             - return to deck list
#   overview:<did>    - select deck and open overview
#   show              - flip the current card (Show Answer)
#   ease:<1..4>       - rate the current card
#   eval:<js>         - run JS in the reviewer webview (debug)
_dev_cmd_seen_mtime = {"v": 0.0}


def _dev_py_namespace() -> dict:
    """One namespace for `pyeval:` / `pyfile:` snippets.

    Deliberately a single dict, used as *both* globals and locals. The
    earlier form — `exec(code, globals(), ns)` — gave a snippet two scopes:
    its top-level assignments landed in `ns` (the locals), but any function
    it defined resolved free variables against this module's globals, where
    those names don't exist. So a perfectly ordinary snippet

        state = {"n": 0}
        def tick(): state["n"] += 1      # NameError: 'state' is not defined

    blew up at call time, with a traceback that pointed at `<string>` and
    told the reader nothing. A copy of our globals (so add-on helpers stay
    reachable) merged with `mw` gives snippets normal module semantics, and
    the copy keeps their assignments out of the real module namespace."""
    ns = dict(globals())
    ns["mw"] = mw
    ns["result"] = None
    return ns


def _dev_run_cmd(raw: str) -> None:
    try:
        cmd = raw.strip()
        if not cmd:
            return
        state = getattr(mw, "state", "")
        _dev_cmd_log(f"run: {cmd!r} (state={state})")
        if cmd.startswith("review:"):
            _start_studying(int(cmd.split(":", 1)[1]))
        elif cmd == "decks":
            mw.moveToState("deckBrowser")
        elif cmd.startswith("overview:"):
            did = int(cmd.split(":", 1)[1])
            mw.col.decks.select(did)
            mw.moveToState("overview")
        elif cmd == "show":
            # Only valid when actively reviewing a card.
            if state != "review":
                _dev_cmd_log("show: not in review, ignored")
                return
            rv = getattr(mw, "reviewer", None)
            card = getattr(rv, "card", None) if rv else None
            if rv and card and getattr(rv, "state", "") == "question":
                try:
                    rv._showAnswer()
                except Exception as e:
                    _dev_cmd_log(f"show err: {e!r}")
                    if rv.web:
                        rv.web.eval("pycmd('ans')")
        elif cmd.startswith("ease:"):
            if state != "review":
                _dev_cmd_log("ease: not in review, ignored")
                return
            n = int(cmd.split(":", 1)[1])
            rv = getattr(mw, "reviewer", None)
            rv_state = getattr(rv, "state", "") if rv else ""
            _dev_cmd_log(f"ease: rv.state={rv_state}")
            if rv and getattr(rv, "card", None) and rv_state == "answer":
                rv._answerCard(n)
                _dev_cmd_log("ease: _answerCard ok")
        elif cmd.startswith("eval:"):
            js = cmd[5:]
            rv = getattr(mw, "reviewer", None)
            if rv and getattr(rv, "web", None):
                rv.web.eval(js)
            elif getattr(mw, "web", None):
                mw.web.eval(js)
        elif cmd.startswith("mweval:"):
            js = cmd[7:]
            if getattr(mw, "web", None):
                mw.web.eval(js)
        elif cmd.startswith("mwecb:"):
            # Like mweval but writes the result to .context/eval_result.txt
            js = cmd[6:]
            out = os.path.join(ADDON_SRC, ".context", "eval_result.txt")
            if getattr(mw, "web", None):
                def _cb(result, _path=out):
                    try:
                        with open(_path, "w") as fh:
                            fh.write(str(result))
                    except Exception:
                        pass
                mw.web.evalWithCallback(js, _cb)
        elif cmd.startswith("congrats:"):
            # Dev-only: force-load Anki's congrats page for the given deck id.
            # Lets us preview the redesign against any deck without having
            # to drain its actual due queue first.
            try:
                did = int(cmd.split(":", 1)[1])
            except Exception:
                _dev_cmd_log("congrats: bad did")
                return
            try:
                mw.col.decks.select(did)
            except Exception:
                pass
            # Patch the scheduler's _is_finished check to True so Anki's
            # native Overview._renderPage takes its `_show_finished_screen`
            # branch and loads the Svelte congrats page naturally. Restored
            # after a short window so normal navigation isn't affected.
            try:
                sched = mw.col.sched
                orig = getattr(sched, "_orig_is_finished", None) or sched._is_finished
                sched._orig_is_finished = orig
                sched._is_finished = lambda: True  # type: ignore
                _dev_cmd_log("congrats: patched _is_finished")
                def _restore(orig=orig):
                    try:
                        sched._is_finished = orig  # type: ignore
                        _dev_cmd_log("congrats: restored _is_finished")
                    except Exception:
                        pass
                if QTimer is not None:
                    QTimer.singleShot(30000, _restore)
            except Exception as e:
                _dev_cmd_log(f"congrats patch err: {e!r}")
            # Always bounce through deckBrowser so moveToState("overview")
            # really re-renders (Anki short-circuits same-state transitions).
            # Re-select the deck after the deckBrowser pass since the state
            # change may reset col.decks.current().
            try:
                mw.moveToState("deckBrowser")
                if QTimer is not None:
                    def _go(d=did):
                        try:
                            mw.col.decks.select(d)
                        except Exception:
                            pass
                        mw.moveToState("overview")
                    QTimer.singleShot(50, _go)
                else:
                    mw.col.decks.select(did)
                    mw.moveToState("overview")
            except Exception:
                pass
        elif cmd.startswith("dump_main:"):
            # Dump main webview HTML to a file (parallel to dump_card which
            # uses the reviewer webview). Argument is the output filename
            # under .context/.
            out_name = cmd.split(":", 1)[1].strip() or "main.html"
            out = os.path.join(ADDON_SRC, ".context", out_name)
            if getattr(mw, "web", None):
                js = (
                    "JSON.stringify({"
                    "html: document.documentElement.outerHTML,"
                    "url: location.href,"
                    "title: document.title"
                    "})"
                )
                def _cb(result, _path=out):
                    try:
                        with open(_path, "w") as fh:
                            fh.write(str(result))
                    except Exception:
                        pass
                mw.web.evalWithCallback(js, _cb)
        elif cmd.startswith("beval:"):
            # Eval JS in the reviewer's bottom toolbar webview.
            js = cmd[6:]
            rv = getattr(mw, "reviewer", None)
            if rv and getattr(rv, "bottom", None) and getattr(rv.bottom, "web", None):
                rv.bottom.web.eval(js)
        elif cmd == "reload_page":
            # Force a full webview reload — addon JS changes don't propagate
            # via the CSS-only hot-reload; reload picks them up cleanly.
            try:
                if getattr(mw, "web", None) is not None:
                    mw.web.reload()
            except Exception as e:
                _dev_cmd_log(f"reload_page err: {e!r}")
        elif cmd == "decks_list":
            try:
                items = [(d.id, d.name) for d in mw.col.decks.all_names_and_ids()]
                _dev_cmd_log(f"decks: {items}")
                _dev_cmd_log(f"col path: {mw.col.path!r}")
                _dev_cmd_log(f"notes: {mw.col.db.scalar('SELECT COUNT(*) FROM notes')}")
                _dev_cmd_log(f"cards: {mw.col.db.scalar('SELECT COUNT(*) FROM cards')}")
            except Exception as e:
                _dev_cmd_log(f"decks err: {e!r}")
        elif cmd.startswith("create_test:"):
            _create_deck_inline(cmd.split(":", 1)[1])
            _dev_cmd_log(f"create_test ran for: {cmd.split(':',1)[1]!r}")
        elif cmd.startswith("state_info"):
            try:
                st = getattr(mw, "state", "")
                rv = getattr(mw, "reviewer", None)
                rvs = getattr(rv, "state", "") if rv else "no-rv"
                card = getattr(rv, "card", None) if rv else None
                cardid = card.id if card else "no-card"
                web = getattr(mw, "web", None)
                _dev_cmd_log(
                    f"state={st} rv.state={rvs} card={cardid} "
                    f"web={web!r} bottomWeb={getattr(mw, 'bottomWeb', None)!r}"
                )
                if web:
                    _dev_cmd_log(
                        f"web visible={web.isVisible()} size={web.size().width()}x{web.size().height()}"
                    )
                bw = getattr(mw, "bottomWeb", None)
                if bw:
                    _dev_cmd_log(
                        f"bottomWeb visible={bw.isVisible()} size={bw.size().width()}x{bw.size().height()}"
                    )
            except Exception as e:
                _dev_cmd_log(f"state_info err: {e!r}")
        elif cmd.startswith("unbury_deck:"):
            did = int(cmd.split(":", 1)[1])
            try:
                mw.col.sched.unbury_deck(did)
                _dev_cmd_log(f"unburied deck {did}")
            except Exception as e:
                _dev_cmd_log(f"unbury err: {e!r}")
        elif cmd == "dump_fit":
            out = os.path.join(ADDON_SRC, ".context", "fit.txt")
            rv = getattr(mw, "reviewer", None)
            if rv and getattr(rv, "web", None):
                js = (
                    "(function(){"
                    "var qa=document.getElementById('qa');"
                    "var card=qa?qa.querySelector('.card'):null;"
                    "var bodyCls=document.body?document.body.className:'';"
                    "var qaCS=qa?getComputedStyle(qa):null;"
                    "var cardCS=card?getComputedStyle(card):null;"
                    "return JSON.stringify({"
                    "bodyClasses:bodyCls,"
                    "isLong:document.body.classList.contains('ba-rv-long'),"
                    "qaScrollHeight:qa?qa.scrollHeight:null,"
                    "qaClientHeight:qa?qa.clientHeight:null,"
                    "qaPaddingTop:qaCS?qaCS.paddingTop:null,"
                    "qaPaddingBottom:qaCS?qaCS.paddingBottom:null,"
                    "cardFontSize:cardCS?cardCS.fontSize:null,"
                    "cardInlineFontSize:card?card.style.fontSize:null"
                    "});})()"
                )
                def _cb(r, _p=out):
                    try: open(_p,"w").write(str(r))
                    except Exception: pass
                rv.web.evalWithCallback(js, _cb)
        elif cmd == "dump_html":
            out = os.path.join(ADDON_SRC, ".context", "html.txt")
            rv = getattr(mw, "reviewer", None)
            w = mw.web if mw else None
            if w is not None:
                js = (
                    "JSON.stringify({"
                    "title:document.title,"
                    "url:location.href,"
                    "bodyHead:document.body?document.body.innerHTML.substring(0,500):'no body'"
                    "})"
                )
                def _cb(r, _p=out):
                    try: open(_p,"w").write(str(r))
                    except Exception: pass
                w.evalWithCallback(js, _cb)
        elif cmd == "dump_body_css":
            out = os.path.join(ADDON_SRC, ".context", "body_css.txt")
            rv = getattr(mw, "reviewer", None)
            if rv and getattr(rv, "web", None):
                js = (
                    "(function(){"
                    "var b=document.body;"
                    "var cs=getComputedStyle(b);"
                    "var links=Array.from(document.querySelectorAll('link[rel=stylesheet]'))"
                    ".map(function(l){return l.href;});"
                    "return JSON.stringify({"
                    "height:cs.height,maxHeight:cs.maxHeight,"
                    "minHeight:cs.minHeight,overflow:cs.overflow,"
                    "display:cs.display,gridTemplateRows:cs.gridTemplateRows,"
                    "links:links"
                    "});})()"
                )
                def _cb(r, _p=out):
                    try: open(_p,"w").write(str(r))
                    except Exception: pass
                rv.web.evalWithCallback(js, _cb)
        elif cmd == "dump_layout":
            out = os.path.join(ADDON_SRC, ".context", "layout.txt")
            rv = getattr(mw, "reviewer", None)
            if rv and getattr(rv, "web", None):
                js = (
                    "(function(){"
                    "function rect(el){if(!el)return null;"
                    "var r=el.getBoundingClientRect();"
                    "var cs=getComputedStyle(el);"
                    "return {x:r.x,y:r.y,w:r.width,h:r.height,"
                    "display:cs.display,visibility:cs.visibility,"
                    "overflow:cs.overflow,gridRow:cs.gridRow};}"
                    "return JSON.stringify({"
                    "viewport:{w:window.innerWidth,h:window.innerHeight},"
                    "body:rect(document.body),"
                    "qa:rect(document.getElementById('qa')),"
                    "head:rect(document.querySelector('.ba-rv-head')),"
                    "ease:rect(document.querySelector('.ba-rv-ease')),"
                    "scroll:{x:window.scrollX,y:window.scrollY,"
                    "docH:document.documentElement.scrollHeight}"
                    "});})()"
                )
                def _cb(r, _p=out):
                    try: open(_p,"w").write(str(r))
                    except Exception: pass
                rv.web.evalWithCallback(js, _cb)
        elif cmd == "dump_state":
            # Quick state probe: writes JSON to .context/state.txt with
            # window.__baReviewerState, ease.hidden, populated intervals,
            # whether `hr#answer` is present, and the rendered card type.
            out = os.path.join(ADDON_SRC, ".context", "state.txt")
            rv = getattr(mw, "reviewer", None)
            if rv and getattr(rv, "web", None):
                js = (
                    "(function(){"
                    "var ease=document.querySelector('.ba-rv-ease');"
                    "var qa=document.getElementById('qa');"
                    "var hr=qa?qa.querySelector('hr#answer'):null;"
                    "var wrap=qa?qa.querySelector('.ba-rv-answer'):null;"
                    "var bodyCls=document.body?document.body.className:'';"
                    "var intervals=Array.from(document.querySelectorAll("
                    "'.ba-rv-ease-int')).map(function(s){return s.textContent;});"
                    "var hidden=ease?ease.hidden:null;"
                    "var visibleKeys=Array.from(document.querySelectorAll("
                    "'.ba-rv-ease-key')).filter(function(b){return !b.hidden;})"
                    ".map(function(b){return b.getAttribute('data-ease')+':'"
                    "+(b.querySelector('.ba-rv-ease-int')||{textContent:''})"
                    ".textContent;});"
                    "return JSON.stringify({"
                    "jsState:window.__baReviewerState,"
                    "easeHidden:hidden,"
                    "hasHrAnswer:!!hr,"
                    "hasWrap:!!wrap,"
                    "bodyClasses:bodyCls,"
                    "intervals:intervals,"
                    "visibleKeys:visibleKeys"
                    "});})()"
                )
                def _cb(r, _p=out, _rv=rv):
                    try:
                        rv_state = getattr(_rv, "state", "?")
                        cid = _rv.card.id if _rv.card else "?"
                        open(_p, "w").write(
                            f"pyState={rv_state} card={cid}\njs={r}\n"
                        )
                    except Exception:
                        pass
                rv.web.evalWithCallback(js, _cb)
        elif cmd.startswith("dump_ease_main"):
            out = os.path.join(ADDON_SRC, ".context", "ease_main.txt")
            rv = getattr(mw, "reviewer", None)
            if rv and getattr(rv, "web", None):
                js = (
                    "(function(){"
                    "var btns=document.querySelectorAll('.ba-rv-ease-key');"
                    "var out=[];"
                    "btns.forEach(function(b){"
                    "var cs=getComputedStyle(b);"
                    "var ca=getComputedStyle(b,'::after');"
                    "var cb=getComputedStyle(b,'::before');"
                    "out.push({text:b.textContent.trim(),"
                    "td:cs.textDecoration,tdl:cs.textDecorationLine,"
                    "outline:cs.outline,bs:cs.boxShadow,bb:cs.borderBottom,"
                    "after:{content:ca.content,bs:ca.boxShadow,bb:ca.borderBottom},"
                    "before:{content:cb.content,bs:cb.boxShadow}"
                    "});});"
                    "return JSON.stringify(out);})()"
                )
                def _cb(r, _p=out):
                    try: open(_p,"w").write(str(r))
                    except Exception: pass
                rv.web.evalWithCallback(js, _cb)
        elif cmd.startswith("dump_ease"):
            out = os.path.join(ADDON_SRC, ".context", "ease.txt")
            rv = getattr(mw, "reviewer", None)
            if rv and getattr(rv, "bottom", None) and getattr(rv.bottom, "web", None):
                js = (
                    "(function(){"
                    "var btns=document.querySelectorAll('#middle button');"
                    "var out=[];"
                    "btns.forEach(function(b){"
                    "var bb=b.getBoundingClientRect();"
                    "var bcs=getComputedStyle(b,'::before');"
                    "out.push({"
                    "btn:{rect:[bb.x,bb.y,bb.width,bb.height]},"
                    "before:{content:bcs.content,fs:bcs.fontSize,"
                    "w:bcs.width,h:bcs.height,bg:bcs.backgroundColor}"
                    "});});"
                    "return JSON.stringify(out);})()"
                )
                def _cb(r, _p=out):
                    try: open(_p,"w").write(str(r))
                    except Exception: pass
                rv.bottom.web.evalWithCallback(js, _cb)
        elif cmd.startswith("dump_bottom_compute"):
            out = os.path.join(ADDON_SRC, ".context", "bottom_compute.txt")
            rv = getattr(mw, "reviewer", None)
            if rv and getattr(rv, "bottom", None) and getattr(rv.bottom, "web", None):
                js = (
                    "(function(){"
                    "var b=document.body,h=document.documentElement;"
                    "var cs=function(el){return el?getComputedStyle(el):null;};"
                    "return JSON.stringify({"
                    "html:{theme:h.dataset.rfTheme,cls:h.className,"
                    "rfpaper:getComputedStyle(h).getPropertyValue('--rf-paper').trim()},"
                    "body:{bg:cs(b).backgroundColor,fg:cs(b).color}});"
                    "})()"
                )
                def _cb(r, _p=out):
                    try: open(_p,"w").write(str(r))
                    except Exception: pass
                rv.bottom.web.evalWithCallback(js, _cb)
        elif cmd.startswith("dump_compute"):
            out = os.path.join(ADDON_SRC, ".context", "compute.txt")
            rv = getattr(mw, "reviewer", None)
            if rv and getattr(rv, "web", None):
                js = (
                    "(function(){"
                    "var b=document.body,q=document.getElementById('qa');"
                    "var cs=function(el){return el?getComputedStyle(el):null;};"
                    "return JSON.stringify({"
                    "body:{fs:cs(b).fontSize,ff:cs(b).fontFamily},"
                    "qa:{fs:cs(q).fontSize,ff:cs(q).fontFamily}"
                    "});})()"
                )
                def _cb(r, _p=out):
                    try: open(_p,"w").write(str(r))
                    except Exception: pass
                rv.web.evalWithCallback(js, _cb)
        elif cmd.startswith("dump_card"):
            out = os.path.join(ADDON_SRC, ".context", "card.html")
            rv = getattr(mw, "reviewer", None)
            if rv and getattr(rv, "web", None):
                js = (
                    "JSON.stringify({"
                    "html: document.documentElement.outerHTML,"
                    "card: (document.querySelector('.card')||document.body).outerHTML"
                    "})"
                )
                def _cb(result, _path=out):
                    try:
                        with open(_path, "w") as fh:
                            fh.write(str(result))
                    except Exception:
                        pass
                rv.web.evalWithCallback(js, _cb)
        elif cmd.startswith("pyeval:"):
            # Dev-only: exec arbitrary Python on the Qt main thread. `mw` is
            # in scope; anything assigned to `result` is logged. Used by the
            # showcase capture pipeline (window geometry, ui scale, etc.).
            code = raw.split(":", 1)[1]
            try:
                ns = _dev_py_namespace()
                exec(code, ns)
                _dev_cmd_log(f"pyeval ok: result={ns.get('result')!r}")
            except Exception as e:
                _dev_cmd_log(f"pyeval err: {e!r}")
        elif cmd.startswith("pyfile:"):
            # Same, but reads the code from a file — survives newlines.
            path = cmd.split(":", 1)[1].strip()
            try:
                with open(path) as fh:
                    code = fh.read()
                ns2 = _dev_py_namespace()
                exec(code, ns2)
                _dev_cmd_log(f"pyfile ok: result={ns2.get('result')!r}")
            except Exception as e:
                _dev_cmd_log(f"pyfile err: {e!r}")
        elif cmd.startswith("dump_bottom"):
            # Print the bottom bar's outerHTML to stdout (via a tempfile).
            import json as _json
            out = os.path.join(ADDON_SRC, ".context", "bottom.html")
            rv = getattr(mw, "reviewer", None)
            if rv and getattr(rv, "bottom", None) and getattr(rv.bottom, "web", None):
                js = (
                    "(function(){var x={html:document.documentElement.outerHTML,"
                    "links:Array.from(document.querySelectorAll('link')).map(l=>l.href)};"
                    "var b=document.querySelector('button');"
                    "if(b){var cs=getComputedStyle(b);"
                    "x.btn={border:cs.border,bg:cs.backgroundColor,br:cs.borderRadius,"
                    "appearance:cs.appearance,wkApp:cs.webkitAppearance};}"
                    "return JSON.stringify(x);})()"
                )
                def _cb(result, _path=out):
                    try:
                        with open(_path, "w") as fh:
                            fh.write(str(result))
                    except Exception:
                        pass
                rv.bottom.web.evalWithCallback(js, _cb)
        elif cmd.startswith("browse_dims"):
            try:
                from aqt.qt import QFrame, QSplitter
                ov = None
                for f in mw.form.centralwidget.findChildren(QFrame):
                    if f.objectName() == "ba-browse-embed":
                        ov = f
                        break
                if ov is None:
                    _dev_cmd_log("browse_dims: no overlay")
                else:
                    _dev_cmd_log(
                        f"overlay: geom={ov.geometry().getRect()} "
                        f"size={ov.width()}x{ov.height()}"
                    )
                    sp = ov.findChild(QSplitter)
                    if sp is None:
                        _dev_cmd_log("browse_dims: no splitter")
                    else:
                        _dev_cmd_log(
                            f"outer splitter: sizes={sp.sizes()} "
                            f"count={sp.count()} handle_w={sp.handleWidth()} "
                            f"orient={sp.orientation()}"
                        )
                        for i in range(sp.count()):
                            w = sp.widget(i)
                            _dev_cmd_log(
                                f"  child[{i}]: {type(w).__name__} "
                                f"obj={w.objectName()!r} "
                                f"size={w.width()}x{w.height()} "
                                f"min={w.minimumWidth()} max={w.maximumWidth()}"
                            )
                    # Inner Browser splitter (search/table | editor) lives
                    # inside centralwidget. It's the one the user drags to
                    # resize the editor pane.
                    try:
                        from . import browse_embed
                        br = browse_embed._state.get("browser")
                        if br is not None:
                            inner = br.form.splitter
                            _dev_cmd_log(
                                f"inner splitter: sizes={inner.sizes()} "
                                f"count={inner.count()} "
                                f"collapsible={inner.childrenCollapsible()} "
                                f"orient={inner.orientation()}"
                            )
                            for i in range(inner.count()):
                                w = inner.widget(i)
                                _dev_cmd_log(
                                    f"  inner[{i}]: {type(w).__name__} "
                                    f"obj={w.objectName()!r} "
                                    f"size={w.width()}x{w.height()} "
                                    f"visible={w.isVisible()} "
                                    f"min={w.minimumWidth()} max={w.maximumWidth()}"
                                )
                            fa = getattr(br.form, "fieldsArea", None)
                            if fa is not None:
                                _dev_cmd_log(
                                    f"fieldsArea: size={fa.width()}x{fa.height()} "
                                    f"visible={fa.isVisible()} "
                                    f"min={fa.minimumWidth()}x{fa.minimumHeight()}"
                                )
                            ed = getattr(br, "editor", None)
                            ew = getattr(ed, "web", None) if ed else None
                            if ew is not None:
                                _dev_cmd_log(
                                    f"editor.web: size={ew.width()}x{ew.height()} "
                                    f"visible={ew.isVisible()}"
                                )
                    except Exception as e:
                        _dev_cmd_log(f"inner dump err: {e!r}")
            except Exception as e:
                _dev_cmd_log(f"browse_dims err: {e!r}")
        elif cmd.startswith("browse_search:"):
            # Run a search in the embedded Browser and select the first row.
            try:
                from . import browse_embed
                br = browse_embed._state.get("browser")
                q = cmd.split(":", 1)[1]
                if br is not None:
                    br.form.searchEdit.lineEdit().setText(q)
                    br.onSearchActivated()
                    try:
                        idx = br.table._model.index(0, 0)
                        br.table._view.setCurrentIndex(idx)
                        br.table._view.selectionModel().select(
                            idx,
                            br.table._view.selectionModel().SelectionFlag.ClearAndSelect
                            | br.table._view.selectionModel().SelectionFlag.Rows,
                        )
                    except Exception as e:
                        _dev_cmd_log(f"select err: {e!r}")
                    _dev_cmd_log(f"browse_search: {q!r}")
            except Exception as e:
                _dev_cmd_log(f"browse_search err: {e!r}")
        elif cmd.startswith("browse_inner_resize:"):
            try:
                from . import browse_embed
                br = browse_embed._state.get("browser")
                parts = cmd.split(":", 1)[1].split(",")
                want = [int(p.strip()) for p in parts]
                if br is not None:
                    sp = br.form.splitter
                    sp.setSizes(want)
                    try:
                        sp.splitterMoved.emit(sp.sizes()[0], 1)
                    except Exception:
                        pass
                    _dev_cmd_log(
                        f"browse_inner_resize: set {want} -> got {sp.sizes()}"
                    )
            except Exception as e:
                _dev_cmd_log(f"browse_inner_resize err: {e!r}")
        elif cmd.startswith("browse_resize:"):
            try:
                from aqt.qt import QFrame, QSplitter
                parts = cmd.split(":", 1)[1].split(",")
                want = [int(p.strip()) for p in parts]
                ov = None
                for f in mw.form.centralwidget.findChildren(QFrame):
                    if f.objectName() == "ba-browse-embed":
                        ov = f
                        break
                if ov is not None:
                    sp = ov.findChild(QSplitter)
                    if sp is not None:
                        sp.setSizes(want)
                        # Fire splitterMoved to mimic a user drag (so the
                        # clamp handler runs as it would in real usage).
                        try:
                            sp.splitterMoved.emit(sp.sizes()[0], 1)
                        except Exception:
                            pass
                        _dev_cmd_log(
                            f"browse_resize: set {want} -> got {sp.sizes()}"
                        )
            except Exception as e:
                _dev_cmd_log(f"browse_resize err: {e!r}")
    except Exception as e:
        try:
            print("[ba-dev-cmd]", repr(e))
        except Exception:
            pass


def _dev_cmd_log(msg: str) -> None:
    try:
        with open(os.path.join(ADDON_SRC, ".context", "addon.log"), "a") as fh:
            fh.write(f"[{time.strftime('%H:%M:%S')}] {msg}\n")
    except Exception:
        pass


def _dev_cmd_watch() -> None:
    cmd_file = os.path.join(ADDON_SRC, ".context", "cmd")
    _dev_cmd_log("dev_cmd_watch started")
    while not _dev_stop.is_set() and _dev_active():
        try:
            if os.path.exists(cmd_file):
                mtime = os.path.getmtime(cmd_file)
                if mtime != _dev_cmd_seen_mtime["v"]:
                    _dev_cmd_seen_mtime["v"] = mtime
                    try:
                        with open(cmd_file, "r") as fh:
                            whole = fh.read()
                    except Exception as e:
                        _dev_cmd_log(f"read err: {e!r}")
                        whole = ""
                    # Appended (`echo cmd >> .context/cmd`): dispatch only
                    # the delta. Rewritten (`echo cmd > .context/cmd`, or the
                    # same line written again): dispatch the whole file.
                    prev = _dev_cmd_seen_text["v"]
                    if prev and whole.startswith(prev) and len(whole) > len(prev):
                        data = whole[len(prev):]
                    else:
                        data = whole
                    _dev_cmd_seen_text["v"] = whole
                    for line in data.splitlines():
                        if line.strip():
                            _dev_cmd_log(f"cmd: {line!r}")
                            try:
                                mw.taskman.run_on_main(
                                    lambda l=line: _dev_run_cmd(l)
                                )
                            except Exception as e:
                                _dev_cmd_log(f"dispatch err: {e!r}")
        except Exception as e:
            _dev_cmd_log(f"loop err: {e!r}")
        _dev_stop.wait(0.2)
    _dev_cmd_log("dev_cmd_watch exited")


_dev_cmd_started = {"v": False}
_dev_cmd_seen_text = {"v": ""}


def _dev_cmd_start() -> None:
    if not _dev_active():
        return
    if _dev_cmd_started["v"]:
        return
    _dev_cmd_started["v"] = True
    # Heartbeat: truncate the log so each Anki start leaves a fresh file,
    # and stamp it, so "did the add-on load, and when" is answerable
    # without scraping stdout. This used to run at import — which meant
    # any profile that loaded this tree, including a live one that will
    # never arm the channel, wiped the log a demo instance was writing.
    # It belongs here, past the gate.
    try:
        ctx = os.path.join(ADDON_SRC, ".context")
        if os.path.isdir(ctx):
            with open(os.path.join(ctx, "addon.log"), "w") as fh:
                fh.write(f"imported {time.time():.0f}\n")
    except Exception:
        pass
    # Drain any stale cmd file from a prior session so we don't auto-fire
    # a command (like `show`) against the homepage state on startup.
    try:
        stale = os.path.join(ADDON_SRC, ".context", "cmd")
        if os.path.exists(stale):
            _dev_cmd_seen_mtime["v"] = os.path.getmtime(stale)
            with open(stale, "r") as fh:
                _dev_cmd_seen_text["v"] = fh.read()
    except Exception:
        pass
    t = threading.Thread(
        target=_dev_cmd_watch, name="anki-design-dev-cmd", daemon=True
    )
    t.start()


def _dev_showcase_arrange() -> None:
    """Dev-only, opt-in via AD_SHOWCASE env: pin the main window to a known
    geometry and suppress the in-window menubar BEFORE the first webview
    render. Doing this mid-session (resize + menubar relayout) can wedge
    QtWebEngine's surface under a WM-less Xvfb, so it must happen here."""
    if not _dev_active() or not os.environ.get("AD_SHOWCASE"):
        return
    try:
        geo = os.environ.get("AD_SHOWCASE_GEO", "1200x1010")
        w, h = (int(x) for x in geo.split("x"))
        mw.form.menubar.setMaximumHeight(0)
        mw.move(0, 0)
        mw.resize(w, h)
    except Exception:
        pass


gui_hooks.profile_did_open.append(_dev_showcase_arrange)
gui_hooks.profile_did_open.append(_dev_start)
gui_hooks.profile_did_open.append(_dev_cmd_start)
gui_hooks.main_window_did_init.append(_dev_start)
gui_hooks.main_window_did_init.append(_dev_cmd_start)
gui_hooks.profile_will_close.append(_dev_shutdown)
