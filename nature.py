"""Anki Design — nature-video backdrop library.

Reads `user_files/nature/index.json`, the manifest a separate curation
step maintains, and reduces it (plus the `video_selection` config value)
to what the page needs: a filtered, order-stable list of playable clips.

`user_files/` is Anki's conventional per-addon data directory — it
survives add-on updates. It is never bundled into a shipped `.ankiaddon`
because `build.py`'s `EXCLUDE_DIRS` walks it out at package time (that
exclusion is the actual guard; nothing about the directory name itself is
special to Anki), so this module treats its absence as completely normal
(a fresh install, or a dev checkout before curation has run) rather than
an error. Only a
genuinely malformed `index.json` gets logged, and only once, so a missing
library never spams stderr and never raises into the render path.

Kept dependency-free (stdlib only) so it can be imported from both the
web-injection side (`__init__.py`) and the Qt settings page
(`settings.py`) without pulling either into the other.
"""

import json
import os
from typing import Any, Dict, List

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.join(HERE, "user_files", "nature")
INDEX_PATH = os.path.join(ROOT, "index.json")

# Below 5s a rotation reads as a glitch rather than a change of scene, for
# the same reason scene_shuffle_seconds has a floor; above a day it has
# stopped being a "rotation" setting at all. 0 is meaningful (never
# rotate) and bypasses the floor entirely — see rotate_seconds().
ROTATE_SECONDS_DEFAULT = 300
ROTATE_SECONDS_MIN = 5
ROTATE_SECONDS_MAX = 24 * 3600

_warned_corrupt = False


def _warn_once(exc: Exception) -> None:
    """Surface a genuinely broken index.json once per process, never as a
    dialog — this runs on every deck-browser/overview render, and a user
    who never touches video mode should never see it at all."""
    global _warned_corrupt
    if _warned_corrupt:
        return
    _warned_corrupt = True
    try:
        import sys
        print(
            f"[anki-design] user_files/nature/index.json is malformed "
            f"({exc}); nature video backdrop will show nothing until "
            f"it's fixed.",
            file=sys.stderr,
        )
    except Exception:
        pass


def read_index() -> List[Dict[str, str]]:
    """Every usable entry in index.json, as ``{"file", "biome", "title"}``
    dicts. A missing file or dir returns ``[]`` silently (the common case:
    curation hasn't run yet). A present-but-broken index.json — bad JSON,
    a `videos` key that isn't a list — also returns ``[]``, but logs once.
    Entries that are the wrong shape, whose `file` would escape the
    nature/ tree (absolute path, `..` segment), or whose `file` doesn't
    exist on disk, are skipped individually rather than invalidating the
    whole library — this last check is what keeps a stale index.json (a
    clip removed by curation but not re-indexed) from producing an
    entry the page can never actually play, which would otherwise read
    as a permanently blank backdrop instead of degrading to the same
    aurora fallback a missing index gets."""
    try:
        with open(INDEX_PATH, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return []
    except Exception as exc:
        _warn_once(exc)
        return []
    raw = data.get("videos") if isinstance(data, dict) else None
    if not isinstance(raw, list):
        _warn_once(ValueError("'videos' key missing or not a list"))
        return []
    out: List[Dict[str, str]] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        file = str(entry.get("file") or "").strip().replace("\\", "/")
        if not file or file.startswith("/") or ".." in file.split("/"):
            continue
        if not os.path.exists(os.path.join(ROOT, file)):
            continue
        biome = str(entry.get("biome") or "").strip() or "other"
        title = str(entry.get("title") or file).strip()
        out.append({"file": file, "biome": biome, "title": title})
    return out


def biomes(videos: List[Dict[str, str]]) -> List[str]:
    """Distinct biome names actually present, in first-seen (= index.json)
    order — so the settings dropdown only offers biomes with a real clip
    behind them, in a stable order across dialog opens."""
    seen: List[str] = []
    for v in videos:
        b = v.get("biome", "")
        if b and b not in seen:
            seen.append(b)
    return seen


def normalize_selection(raw: Any, videos: List[Dict[str, str]]) -> Any:
    """Reduce the raw `video_selection` config value to one this library
    can honour right now: `"shuffle"`, `"biome:<name>"` for a biome the
    index actually has, an exact file path the index actually has, or —
    the explicit multi-pick form — `{"mode": "custom", "files": [...]}`.
    Anything else — a deleted clip, a biome curation dropped, a typo, a
    custom list that named nothing still present — falls back to
    `"shuffle"` rather than rendering nothing.

    The custom form's return type is the dict itself (not a string), so
    callers that only ever handled strings before still work: they either
    thread it straight into `filtered_for_selection` (which understands
    it) or use it purely for equality/logging, and Python's dict/str
    values are never confused for one another by `==`."""
    if isinstance(raw, dict) and raw.get("mode") == "custom":
        valid = {v["file"] for v in videos}
        files = raw.get("files")
        kept = (
            [f for f in files if isinstance(f, str) and f in valid]
            if isinstance(files, list)
            else []
        )
        return {"mode": "custom", "files": kept} if kept else "shuffle"
    choice = str(raw or "shuffle").strip() or "shuffle"
    if choice == "shuffle":
        return "shuffle"
    if choice.startswith("biome:"):
        name = choice[len("biome:"):]
        return choice if name in biomes(videos) else "shuffle"
    return choice if any(v["file"] == choice for v in videos) else "shuffle"


def filtered_for_selection(
    videos: List[Dict[str, str]], selection: Any
) -> List[Dict[str, str]]:
    """The subset `selection` allows. Never empty when `videos` isn't: an
    unmatched filter (shouldn't happen once normalize_selection has run,
    but config can be hand-edited between renders) falls back to the full
    library rather than to nothing.

    `selection` is usually a string (`"shuffle"`, `"biome:<name>"`, an
    exact file path) but may also be the custom multi-pick form,
    `{"mode": "custom", "files": [...]}` — checked first, and ahead of
    any `str` handling, since a dict has no `.startswith`. An empty (or
    entirely stale — every named file since deleted) `files` list matches
    nothing and falls back to the full library, the same rule every other
    unmatched selection already gets; files that no longer exist simply
    fail to match rather than raising, since `videos` only ever contains
    entries `read_index` already confirmed are on disk."""
    if not videos:
        return []
    if isinstance(selection, dict) and selection.get("mode") == "custom":
        files = set(selection.get("files") or [])
        matched = [v for v in videos if v.get("file") in files]
        return matched or list(videos)
    if not isinstance(selection, str):
        # Anything else — a dict with a mode other than "custom", a
        # number, a list — is a shape normalize_selection never emits,
        # but this module's contract (see the module docstring) is to
        # degrade rather than raise on a hand-edited config, and the
        # str-only handling below would AttributeError on .startswith.
        return list(videos)
    if selection == "shuffle":
        return list(videos)
    if selection.startswith("biome:"):
        name = selection[len("biome:"):]
        matched = [v for v in videos if v.get("biome") == name]
        return matched or list(videos)
    matched = [v for v in videos if v.get("file") == selection]
    return matched or list(videos)


def rotate_seconds(raw: Any) -> int:
    """Clamped rotation interval in seconds. 0 is the deliberate "never" —
    kept out of the clamp so a hand-edited or UI-selected 0 can't be
    dragged back up to the floor."""
    try:
        secs = int(raw)
    except (TypeError, ValueError, OverflowError):
        secs = ROTATE_SECONDS_DEFAULT
    if secs <= 0:
        return 0
    return max(ROTATE_SECONDS_MIN, min(ROTATE_SECONDS_MAX, secs))
