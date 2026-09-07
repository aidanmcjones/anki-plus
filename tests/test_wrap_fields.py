"""Regression tests for editreviewer._wrap_fields.

Runs standalone (no Anki install needed): `python3 tests/test_wrap_fields.py`
or via pytest. Stubs the `aqt` module before import, since editreviewer
does `from aqt import gui_hooks, mw` at module level.
"""

import sys
import types
import os

# Stub aqt before importing the module under test.
aqt_stub = types.ModuleType("aqt")
aqt_stub.gui_hooks = types.SimpleNamespace(
    card_will_show=[],
    state_shortcuts_will_change=[],
    main_window_did_init=[],
)
aqt_stub.mw = None
sys.modules.setdefault("aqt", aqt_stub)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import editreviewer  # noqa: E402


class FakeNote:
    def __init__(self, fields):
        self._fields = fields

    def keys(self):
        return list(self._fields.keys())

    def __getitem__(self, name):
        return self._fields[name]


class FakeCard:
    def __init__(self, fields):
        self._note = FakeNote(fields)

    def note(self):
        return self._note


STYLE = "<style>.card { font-family: arial; }</style>"


def test_leucine_style_survives():
    card = FakeCard({"FullName": "leucine", "ThreeLetter": "leu", "OneLetter": "l"})
    html = STYLE + "What is the structure of leucine"
    out = editreviewer._wrap_fields(html, card, "reviewQuestion")
    assert STYLE in out, f"style block corrupted: {out!r}"
    assert 'data-ba-field="FullName"' in out
    assert ">leucine</div>" in out


def test_attr_only_value_not_wrapped():
    card = FakeCard({"Image": "paste.png"})
    html = '<img src="paste.png">Question text'
    out = editreviewer._wrap_fields(html, card, "reviewQuestion")
    assert out == html, f"attribute value was wrapped: {out!r}"


def test_normal_wrap_still_works():
    card = FakeCard({"Front": "mitochondria"})
    html = STYLE + "<div>What is the mitochondria?</div>"
    out = editreviewer._wrap_fields(html, card, "reviewQuestion")
    assert 'data-ba-field="Front"' in out
    assert ">mitochondria</div>" in out
    assert STYLE in out


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"PASS {name}")
    print("all tests passed")
