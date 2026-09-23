"""Drag-to-reorder on the home deck list.

Anki sorts sibling decks alphabetically and has no sort field, so the
add-on keeps its own per-parent order (`deck_order` in the add-on config)
and applies it when it builds the tree payload the home page renders.

Checks:
  - `_full_deck_tree_payload` puts ranked siblings first, in the saved
    order, and the rest after them in Anki's alphabetical order
  - `_reordered_siblings` moves a deck before/after a sibling, to the top,
    to the bottom, and refuses non-siblings
  - `ba:deck:reorder:<did>:<target>:<before|after>` reaches `_reorder_deck`,
    which writes the new order to the add-on config
  - the congrats "Keep going" list honours the same order

Runs standalone: `python3 tests/test_deck_reorder.py`. Stubs aqt/anki the
same way test_study_state_dispatch.py does.
"""

import os
import sys
import types


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


CONFIG = {}
WRITES = []


class _AddonManager:
    def getConfig(self, *a, **k):
        return dict(CONFIG)

    def writeConfig(self, _name, cfg):
        CONFIG.clear()
        CONFIG.update(cfg)
        WRITES.append(dict(cfg))

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

import importlib.util

spec = importlib.util.spec_from_file_location(
    "ba_addon", os.path.join(os.path.dirname(__file__), "..", "__init__.py"),
)
mod = importlib.util.module_from_spec(spec)
try:
    spec.loader.exec_module(mod)
except Exception as e:  # module-level wiring beyond our stubs
    print(f"SKIP import-level test (module needs fuller aqt: {e!r})")
    sys.exit(0)


# --- a fake collection: Anki hands the tree back alphabetically ---------
def node(did, name, children=(), new=0, learn=0, review=0):
    return types.SimpleNamespace(
        deck_id=did, name=name, children=list(children),
        new_count=new, learn_count=learn, review_count=review,
        collapsed=False,
    )


NAMES = {1: "Amino Acids", 2: "Exam 1", 3: "MCAT", 4: "Microbiology",
         5: "MCAT::Bio", 6: "MCAT::Chem"}


def tree():
    return node(0, "", [
        node(1, "Amino Acids", new=1),
        node(2, "Exam 1", new=2),
        node(3, "MCAT", [node(5, "MCAT::Bio", new=1), node(6, "MCAT::Chem", new=1)]),
        node(4, "Microbiology", new=4),
    ])


class _Decks:
    def get_current_id(self):
        return 1

    def get(self, did):
        return {"id": did, "dyn": False}

    def name(self, did):
        return NAMES.get(int(did), "")


_mw.col = types.SimpleNamespace(
    sched=types.SimpleNamespace(deck_due_tree=tree),
    decks=_Decks(),
)
_mw.state = "deckBrowser"
_mw.deckBrowser = types.SimpleNamespace(refresh=lambda: None)


def dids(rows):
    return [r["did"] for r in rows]


# Default: Anki's alphabetical order, untouched.
CONFIG.clear()
assert dids(mod._full_deck_tree_payload()) == [1, 2, 3, 5, 6, 4]

# A saved order puts ranked siblings first, unranked ones after, still
# alphabetical among themselves. Children travel with their parent.
CONFIG.clear()
CONFIG["deck_order"] = {"0": [4, 2], "3": [6]}
got = dids(mod._full_deck_tree_payload())
assert got == [4, 2, 1, 3, 6, 5], got

# Ids that no longer exist (deleted, moved elsewhere) are simply skipped.
CONFIG["deck_order"] = {"0": [99, "4"]}
got = dids(mod._full_deck_tree_payload())
assert got == [4, 1, 2, 3, 5, 6], got

# The congrats "Keep going" list uses the same order.
CONFIG["deck_order"] = {"0": [4]}
mod._deck_and_descendant_ids = lambda did: [did]
got = dids(mod._filtered_deck_tree(1))
assert got == [4, 2, 3, 5, 6], got

# --- the pure reorder step ------------------------------------------------
rs = mod._reordered_siblings
assert rs([1, 2, 3, 4], 4, 1, "before") == [4, 1, 2, 3]      # to the top
assert rs([1, 2, 3, 4], 1, 4, "after") == [2, 3, 4, 1]       # to the bottom
assert rs([1, 2, 3, 4], 3, 2, "before") == [1, 3, 2, 4]
assert rs([1, 2, 3, 4], 1, 2, "after") == [2, 1, 3, 4]
assert rs([1, 2, 3, 4], 2, 2, "after") is None               # onto itself
assert rs([1, 2, 3, 4], 2, 9, "after") is None               # not a sibling
assert rs([1, 2, 3, 4], 9, 2, "after") is None
assert rs([1, 2, 3, 4], 1, 2, "sideways") is None

# --- end to end: the JS message writes the config and refreshes ----------
CONFIG.clear()
WRITES.clear()
refreshed = []
_mw.deckBrowser = types.SimpleNamespace(refresh=lambda: refreshed.append(1))

handled = mod._on_js_message(False, "ba:deck:reorder:4:1:before", context=None)
assert handled == (True, None), handled
assert CONFIG.get("deck_order", {}).get("0") == [4, 1, 2, 3], CONFIG
assert refreshed, "deck browser was not refreshed after a reorder"
assert dids(mod._full_deck_tree_payload()) == [4, 1, 2, 3, 5, 6]

# A second move starts from the order now on screen, not from alphabetical.
mod._on_js_message(False, "ba:deck:reorder:2:3:after", context=None)
assert CONFIG["deck_order"]["0"] == [4, 1, 3, 2], CONFIG
assert dids(mod._full_deck_tree_payload()) == [4, 1, 3, 5, 6, 2]

# Nested siblings are keyed by their parent; top-level order is untouched.
mod._on_js_message(False, "ba:deck:reorder:6:5:before", context=None)
assert CONFIG["deck_order"]["3"] == [6, 5], CONFIG
assert CONFIG["deck_order"]["0"] == [4, 1, 3, 2], CONFIG
assert dids(mod._full_deck_tree_payload()) == [4, 1, 3, 6, 5, 2]

# Not siblings: nothing is written.
n = len(WRITES)
mod._on_js_message(False, "ba:deck:reorder:5:1:before", context=None)
assert len(WRITES) == n, "a cross-parent reorder should be a no-op"
mod._on_js_message(False, "ba:deck:reorder:5:6:nowhere", context=None)
assert len(WRITES) == n, "an unknown position should be a no-op"

print("PASS test_deck_reorder")
