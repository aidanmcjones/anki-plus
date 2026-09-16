"""Anki Design — nature-video backdrop library.

Reads `user_files/nature/index.json`, the manifest a separate curation
step maintains, and reduces it (plus the `video_selection` config value)
to what the page needs: a filtered, order-stable list of playable clips.

`user_files/` is Anki's conventional per-addon data directory — it
survives add-on updates and is never bundled into a shipped `.ankiaddon`,
so this module treats its absence as completely normal (a fresh install,
or a dev checkout before curation has run) rather than an error. Only a
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
    Entries that are the wrong shape, or whose `file` would escape the
    nature/ tree (absolute path, `..` segment), are skipped individually
    rather than invalidating the whole library."""
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


def normalize_selection(raw: Any, videos: List[Dict[str, str]]) -> str:
    """Reduce the raw `video_selection` config value to one this library
    can honour right now: `"shuffle"`, `"biome:<name>"` for a biome the
    index actually has, or an exact file path the index actually has.
    Anything else — a deleted clip, a biome curation dropped, a typo —
    falls back to `"shuffle"` rather than rendering nothing."""
    choice = str(raw or "shuffle").strip() or "shuffle"
    if choice == "shuffle":
        return "shuffle"
    if choice.startswith("biome:"):
        name = choice[len("biome:"):]
        return choice if name in biomes(videos) else "shuffle"
    return choice if any(v["file"] == choice for v in videos) else "shuffle"


def filtered_for_selection(
    videos: List[Dict[str, str]], selection: str
) -> List[Dict[str, str]]:
    """The subset `selection` allows. Never empty when `videos` isn't: an
    unmatched filter (shouldn't happen once normalize_selection has run,
    but config can be hand-edited between renders) falls back to the full
    library rather than to nothing."""
    if not videos:
        return []
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
    except (TypeError, ValueError):
        secs = ROTATE_SECONDS_DEFAULT
    if secs <= 0:
        return 0
    return max(ROTATE_SECONDS_MIN, min(ROTATE_SECONDS_MAX, secs))
