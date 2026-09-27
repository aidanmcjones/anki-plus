"""Regression tests for nature.py: the nature-video-backdrop library reader
and its pure selection/rotation helpers.

Runs standalone (no Anki install needed): `python3 tests/test_nature.py`.
nature.py is stdlib-only by design (see its module docstring), so unlike
test_sky_phase.py / test_wrap_fields.py this needs no aqt/anki stubbing —
just the repo root on sys.path.
"""

import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import nature  # noqa: E402


# --------------------------------------------------------------------------- #
# read_index()
# --------------------------------------------------------------------------- #

class _TempLibrary:
    """Points nature.ROOT/INDEX_PATH at a throwaway directory for the
    duration of a `with` block, restoring the real paths after — so these
    tests never touch (or depend on) a real user_files/nature/ checkout."""

    def __enter__(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_root = nature.ROOT
        self._orig_index = nature.INDEX_PATH
        nature.ROOT = self._tmp.name
        nature.INDEX_PATH = os.path.join(nature.ROOT, "index.json")
        return self

    def __exit__(self, *exc):
        nature.ROOT = self._orig_root
        nature.INDEX_PATH = self._orig_index
        self._tmp.cleanup()
        return False

    def write_index(self, data):
        with open(nature.INDEX_PATH, "w", encoding="utf-8") as fh:
            json.dump(data, fh)

    def touch(self, rel_path):
        full = os.path.join(nature.ROOT, rel_path)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "wb") as fh:
            fh.write(b"\0")


def test_read_index_missing_file_returns_empty_list():
    with _TempLibrary():
        # No index.json written at all — the common case (fresh install, or
        # a dev checkout before curation has run).
        assert nature.read_index() == []


def test_read_index_malformed_json_returns_empty_list():
    with _TempLibrary() as lib:
        with open(nature.INDEX_PATH, "w", encoding="utf-8") as fh:
            fh.write("{not valid json")
        assert nature.read_index() == []


def test_read_index_videos_key_wrong_type_returns_empty_list():
    with _TempLibrary() as lib:
        lib.write_index({"videos": "not-a-list"})
        assert nature.read_index() == []


def test_read_index_drops_entries_whose_file_is_missing_on_disk():
    # B2: a stale index.json entry (curation removed the clip but didn't
    # re-index) must be dropped, not returned as an unplayable entry — this
    # is what lets a stale index degrade to the same [] aurora fallback a
    # missing index gets, instead of a permanently blank video backdrop.
    with _TempLibrary() as lib:
        lib.touch("ocean/whale.webm")
        lib.write_index({
            "videos": [
                {"file": "ocean/whale.webm", "biome": "ocean", "title": "Whale"},
                {"file": "forest/deleted.webm", "biome": "forest", "title": "Gone"},
            ]
        })
        out = nature.read_index()
        assert [v["file"] for v in out] == ["ocean/whale.webm"]


def test_read_index_skips_path_traversal_and_absolute_entries():
    with _TempLibrary() as lib:
        lib.touch("ocean/whale.webm")
        lib.write_index({
            "videos": [
                {"file": "ocean/whale.webm", "biome": "ocean", "title": "Whale"},
                {"file": "../outside.webm", "biome": "x", "title": "Escape"},
                {"file": "/etc/passwd", "biome": "x", "title": "Absolute"},
                {"file": "a/../../b.webm", "biome": "x", "title": "Mid-traversal"},
                {"file": "", "biome": "x", "title": "Empty"},
                "not-a-dict",
            ]
        })
        out = nature.read_index()
        assert [v["file"] for v in out] == ["ocean/whale.webm"]


def test_read_index_defaults_biome_and_title():
    with _TempLibrary() as lib:
        lib.touch("clip.webm")
        lib.write_index({"videos": [{"file": "clip.webm"}]})
        out = nature.read_index()
        assert out == [{"file": "clip.webm", "biome": "other", "title": "clip.webm"}]


def test_read_index_normalizes_backslashes():
    with _TempLibrary() as lib:
        lib.touch(os.path.join("ocean", "whale.webm"))
        lib.write_index({"videos": [{"file": "ocean\\whale.webm", "biome": "ocean"}]})
        out = nature.read_index()
        assert out and out[0]["file"] == "ocean/whale.webm"


# --------------------------------------------------------------------------- #
# biomes()
# --------------------------------------------------------------------------- #

def test_biomes_first_seen_order_and_dedup():
    videos = [
        {"file": "a", "biome": "ocean"},
        {"file": "b", "biome": "forest"},
        {"file": "c", "biome": "ocean"},
        {"file": "d", "biome": ""},
    ]
    assert nature.biomes(videos) == ["ocean", "forest"]


# --------------------------------------------------------------------------- #
# normalize_selection()
# --------------------------------------------------------------------------- #

_VIDEOS = [
    {"file": "ocean/whale.webm", "biome": "ocean", "title": "Whale"},
    {"file": "forest/deer.webm", "biome": "forest", "title": "Deer"},
]


def test_normalize_selection_defaults_to_shuffle():
    assert nature.normalize_selection(None, _VIDEOS) == "shuffle"
    assert nature.normalize_selection("", _VIDEOS) == "shuffle"
    assert nature.normalize_selection("shuffle", _VIDEOS) == "shuffle"


def test_normalize_selection_keeps_valid_biome():
    assert nature.normalize_selection("biome:ocean", _VIDEOS) == "biome:ocean"


def test_normalize_selection_drops_biome_no_longer_present():
    # Curation dropped the biome entirely (every clip in it removed) — fall
    # back to shuffle rather than rendering nothing.
    assert nature.normalize_selection("biome:tundra", _VIDEOS) == "shuffle"


def test_normalize_selection_keeps_valid_exact_file():
    assert (
        nature.normalize_selection("ocean/whale.webm", _VIDEOS)
        == "ocean/whale.webm"
    )


def test_normalize_selection_drops_deleted_file():
    assert nature.normalize_selection("ocean/deleted.webm", _VIDEOS) == "shuffle"


def test_normalize_selection_custom_keeps_valid_files():
    raw = {"mode": "custom", "files": ["ocean/whale.webm", "forest/deer.webm"]}
    assert nature.normalize_selection(raw, _VIDEOS) == {
        "mode": "custom",
        "files": ["ocean/whale.webm", "forest/deer.webm"],
    }


def test_normalize_selection_custom_drops_missing_files_but_keeps_rest():
    raw = {"mode": "custom", "files": ["ocean/whale.webm", "ocean/deleted.webm"]}
    assert nature.normalize_selection(raw, _VIDEOS) == {
        "mode": "custom",
        "files": ["ocean/whale.webm"],
    }


def test_normalize_selection_custom_empty_files_falls_back_to_shuffle():
    assert nature.normalize_selection({"mode": "custom", "files": []}, _VIDEOS) == "shuffle"


def test_normalize_selection_custom_all_files_missing_falls_back_to_shuffle():
    raw = {"mode": "custom", "files": ["nowhere/gone.webm"]}
    assert nature.normalize_selection(raw, _VIDEOS) == "shuffle"


def test_normalize_selection_custom_non_list_files_falls_back_to_shuffle():
    assert nature.normalize_selection({"mode": "custom", "files": "not-a-list"}, _VIDEOS) == "shuffle"
    assert nature.normalize_selection({"mode": "custom"}, _VIDEOS) == "shuffle"


# --------------------------------------------------------------------------- #
# filtered_for_selection()
# --------------------------------------------------------------------------- #

def test_filtered_for_selection_empty_library():
    assert nature.filtered_for_selection([], "shuffle") == []


def test_filtered_for_selection_shuffle_returns_everything():
    assert nature.filtered_for_selection(_VIDEOS, "shuffle") == _VIDEOS


def test_filtered_for_selection_biome_match():
    out = nature.filtered_for_selection(_VIDEOS, "biome:ocean")
    assert out == [_VIDEOS[0]]


def test_filtered_for_selection_biome_no_match_falls_back_to_full_library():
    # Config hand-edited or stale between normalize_selection() runs —
    # shouldn't happen, but filtered_for_selection must not go blank.
    out = nature.filtered_for_selection(_VIDEOS, "biome:tundra")
    assert out == _VIDEOS


def test_filtered_for_selection_exact_file_match():
    out = nature.filtered_for_selection(_VIDEOS, "forest/deer.webm")
    assert out == [_VIDEOS[1]]


def test_filtered_for_selection_exact_file_no_match_falls_back_to_full_library():
    out = nature.filtered_for_selection(_VIDEOS, "ocean/deleted.webm")
    assert out == _VIDEOS


def test_filtered_for_selection_custom_returns_only_checked_files():
    selection = {"mode": "custom", "files": ["forest/deer.webm"]}
    assert nature.filtered_for_selection(_VIDEOS, selection) == [_VIDEOS[1]]


def test_filtered_for_selection_custom_preserves_videos_order_not_files_order():
    # Order comes from the library (index.json / videos), not from the
    # order files were listed in the selection — so rotation order stays
    # stable regardless of how the checkbox list happened to serialize.
    selection = {"mode": "custom", "files": ["forest/deer.webm", "ocean/whale.webm"]}
    assert nature.filtered_for_selection(_VIDEOS, selection) == _VIDEOS


def test_filtered_for_selection_custom_empty_files_falls_back_to_full_library():
    out = nature.filtered_for_selection(_VIDEOS, {"mode": "custom", "files": []})
    assert out == _VIDEOS


def test_filtered_for_selection_custom_missing_files_dropped_rest_kept():
    selection = {"mode": "custom", "files": ["ocean/whale.webm", "nowhere/gone.webm"]}
    assert nature.filtered_for_selection(_VIDEOS, selection) == [_VIDEOS[0]]


def test_filtered_for_selection_custom_all_files_missing_falls_back_to_full_library():
    selection = {"mode": "custom", "files": ["nowhere/gone.webm"]}
    assert nature.filtered_for_selection(_VIDEOS, selection) == _VIDEOS


def test_filtered_for_selection_non_string_non_custom_dict_falls_back_to_full_library():
    # normalize_selection() never emits this shape (a dict whose "mode"
    # isn't "custom"), but filtered_for_selection's docstring promises
    # hand-edit robustness independent of that — without the isinstance
    # guard this used to fall through to `selection.startswith(...)` and
    # raise AttributeError instead of degrading.
    assert nature.filtered_for_selection(_VIDEOS, {"mode": "bogus"}) == _VIDEOS
    assert nature.filtered_for_selection(_VIDEOS, 42) == _VIDEOS
    assert nature.filtered_for_selection(_VIDEOS, None) == _VIDEOS


# --------------------------------------------------------------------------- #
# rotate_seconds()
# --------------------------------------------------------------------------- #

def test_rotate_seconds_default_on_garbage():
    assert nature.rotate_seconds("not-a-number") == nature.ROTATE_SECONDS_DEFAULT
    assert nature.rotate_seconds(None) == nature.ROTATE_SECONDS_DEFAULT
    assert nature.rotate_seconds([1, 2]) == nature.ROTATE_SECONDS_DEFAULT


def test_rotate_seconds_overflow_falls_back_to_default():
    # B4: int(float("inf")) raises OverflowError, not ValueError/TypeError —
    # rotate_seconds must catch that too instead of propagating it into the
    # render path.
    assert nature.rotate_seconds(float("inf")) == nature.ROTATE_SECONDS_DEFAULT
    assert nature.rotate_seconds(float("-inf")) == nature.ROTATE_SECONDS_DEFAULT


def test_rotate_seconds_zero_and_negative_mean_never():
    assert nature.rotate_seconds(0) == 0
    assert nature.rotate_seconds(-5) == 0


def test_rotate_seconds_clamped_to_floor_and_ceiling():
    assert nature.rotate_seconds(1) == nature.ROTATE_SECONDS_MIN
    assert nature.rotate_seconds(10 ** 9) == nature.ROTATE_SECONDS_MAX


def test_rotate_seconds_passthrough_in_range():
    assert nature.rotate_seconds(60) == 60


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"PASS {name}")
    print("all tests passed")
