"""File -> (area, kind) classification for ankiship.

An *area* is the function a file belongs to (browse, sidebar, reviewer, ...).
A test file lands in the area of the feature it tests, so a fix and its test
travel in one commit. Rules are ordered: the first match wins.

Two profiles ship built in: ``addon`` (the Anki+ add-on at the root of
anki-plus main) and ``engine`` (the Anki engine fork). A repo can override the
profile with ``git config ship.profile <name>``.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import List, Optional, Tuple

# (area, regex searched against the lower-cased repo-relative path)
ADDON_RULES: List[Tuple[str, str]] = [
    ("bug-report", r"bugreport|bug_report"),
    ("browse", r"browse"),
    ("fullscreen", r"fullscreen|cocoa_smoke"),
    ("backdrop", r"nature|video-backdrop|backdrop|scene|sky_phase"),
    ("scheduling", r"restudy|deadline"),
    ("stats", r"stats|heatmap"),
    ("search", r"cmdk"),
    ("sidebar", r"sidebar|decklist|deck_reorder|deck_drop|deck_names|homedeck|subsections|deckopts"),
    ("tags", r"tag_organizer|(^|/)tags?[_.]"),
    ("editor", r"addcard|editor"),
    ("reviewer", r"reviewer|card_scale|card-scale|card_styling|card_reverse|congrats|study_state|wrap_fields"),
    ("stability", r"renderer_recovery|render_memory|webview_drops"),
    ("settings", r"settings|(^|/)config\.(json|md)$|(^|/)meta\.json$|ankihub"),
    ("theme", r"theme|tokens|colors|logo|toolbar|fonts/"),
    ("build", r"(^|/)(build\.py|makefile|manifest\.json|\.gitignore)$|^scripts/"),
    ("test-harness", r"^tests/|(^|/)live_test\.py$"),
    ("docs", r"\.md$|^docs/"),
]

ENGINE_RULES: List[Tuple[str, str]] = [
    ("launcher", r"^extra/macos-launcher/"),
    ("deck-skills", r"^extra/[^/]*anki-deck/"),
    ("scheduling", r"scheduler|fsrs|restudy|auto_optimize|studying\.ftl|congrats"),
    ("stats", r"stats|graphs|velocity|statistics\.ftl"),
    ("editor", r"routes/editor|image-occlusion|cardgen"),
    ("deck-options", r"routes/deck-options"),
    ("i18n", r"^ftl/"),
    ("deps", r"(^|/)(package\.json|yarn\.lock|uv\.lock|cargo\.(toml|lock)|pyproject\.toml)$"),
    ("ci", r"^\.github/"),
    ("docs", r"\.md$|^docs"),
    ("desktop", r"^qt/"),
    ("web", r"^ts/"),
    ("core", r"^(rslib|pylib|proto)/"),
]

PROFILES = {"addon": ADDON_RULES, "engine": ENGINE_RULES}

TEST_RE = re.compile(r"(^|/)tests?/|(^|/)test_[^/]*$|_test\.py$|\.(spec|test)\.[jt]s$")
DOC_RE = re.compile(r"\.md$|^docs/")
BUILD_AREAS = {"build", "deps", "ci", "test-harness"}


def detect_profile(repo: Path) -> str:
    if (repo / "Cargo.toml").exists() and (repo / "rslib").is_dir():
        return "engine"
    return "addon"


def area_of(path: str, profile: str) -> str:
    p = path.lower()
    for area, rx in PROFILES[profile]:
        if re.search(rx, p):
            return area
    return "misc" if profile == "addon" else "core"


def is_test(path: str) -> bool:
    return bool(TEST_RE.search(path.lower()))


def is_doc(path: str) -> bool:
    return bool(DOC_RE.search(path.lower()))


def kind_of(files: List[Tuple[str, str]], area: str, forced: Optional[str] = None) -> str:
    """Conventional-commit type for one group. ``files`` is [(status, path)]
    with status A (added), M, D or R. Order: forced > docs > test > build >
    feat (new non-test source) > fix."""
    if forced:
        return forced
    paths = [p for _, p in files]
    if all(is_doc(p) for p in paths):
        return "docs"
    if all(is_test(p) for p in paths):
        return "test"
    if area in BUILD_AREAS:
        return "build"
    if any(s == "A" and not is_test(p) and not is_doc(p) for s, p in files):
        return "feat"
    return "fix"
