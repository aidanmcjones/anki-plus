"""Unit tests for tag_organizer.py's pure planning logic.

Runs standalone: `python3 tests/test_tag_organizer.py`. `aqt` is stubbed
(the module only needs `aqt.mw` at import). The live test
tests/live/test_tag_organizer.py covers the app side.
"""

import importlib.util
import os
import sys
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

aqt = types.ModuleType("aqt")
aqt.mw = None
sys.modules.setdefault("aqt", aqt)

spec = importlib.util.spec_from_file_location("tag_organizer", os.path.join(ROOT, "tag_organizer.py"))
to = importlib.util.module_from_spec(spec)
spec.loader.exec_module(to)

IGN = to.DEFAULT_IGNORE
ROOTS = ["FunBiochem", "MCAT", "Microbiology"]


def plan(note_tags, note_course, cand=None):
    if cand is None:
        cand = sorted({t for ts in note_tags.values() for t in ts})
    return to.plan_renames(note_tags, note_course, cand, ROOTS, IGN)


def test_derive_root():
    assert to.derive_root("Organic Chemistry") == "Organic_Chemistry"
    assert to.derive_root("Cell & Mol. Bio") == "Cell_Mol_Bio"
    assert to.derive_root("  ") == ""


def test_flat_tag_one_course():
    r, m = plan({1: ["hi_yield"], 2: ["hi_yield"]}, {1: "FunBiochem", 2: "FunBiochem"})
    assert r == [("hi_yield", "FunBiochem::hi_yield")] and m == []


def test_foreign_nested_moves_whole_subtree():
    r, _ = plan({1: ["Kaplan::Ch1"], 2: ["Kaplan::Ch2"]}, {1: "MCAT", 2: "MCAT"})
    assert r == [("Kaplan", "MCAT::Kaplan")]


def test_mixed_parent_splits():
    r, m = plan({1: ["Kaplan::Ch1"], 2: ["Kaplan::Ch2"]}, {1: "MCAT", 2: "FunBiochem"})
    assert sorted(r) == [("Kaplan::Ch1", "MCAT::Kaplan::Ch1"), ("Kaplan::Ch2", "FunBiochem::Kaplan::Ch2")]
    assert m == []


def test_shared_and_no_course_left_alone():
    r, m = plan({1: ["shared"], 2: ["shared"], 3: ["orphan"]}, {1: "MCAT", 2: "FunBiochem", 3: None})
    assert r == [] and sorted(m) == ["orphan", "shared"]


def test_ignored_and_course_roots_never_move():
    tags = ["marked", "leech", "Type::X", "AnkiHub_Subdeck::X", "AnkiHub_Foo", "MCAT::x", "funbiochem::y"]
    r, m = plan({1: tags}, {1: "MCAT"})
    assert r == [] and m == []


def test_incremental_only_candidates():
    nt = {1: ["old", "new"]}
    r, _ = to.plan_renames(nt, {1: "MCAT"}, ["new"], ROOTS, IGN)
    assert r == [("new", "MCAT::new")]


def test_summary():
    assert to.summary([("a", "MCAT::a")]) == "Filed 1 tag under MCAT::"
    assert to.summary([("a", "MCAT::a"), ("b", "FunBiochem::b")]) == "Filed 2 tags under FunBiochem::, MCAT::"


if __name__ == "__main__":
    n = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            n += 1
    print(f"ok: {n} tests")
