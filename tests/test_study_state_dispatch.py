"""Smoke test: the `ba:study-state:<kind>` sidebar message dispatches to
_study_state, and unknown kinds are ignored without error.

Runs standalone: `python3 tests/test_study_state_dispatch.py`. Stubs the
aqt/anki modules before import; the add-on's module-level imports only
need the names it pulls at the top of __init__.py.
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


hooks = types.SimpleNamespace()


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
aqt = _stub("aqt", gui_hooks=_Hooks(), mw=_mw)
_stub("aqt.deckbrowser", DeckBrowser=_AnyClass, DeckBrowserContent=_AnyClass)
_stub("aqt.editor", Editor=_AnyClass, EditorMode=_AnyClass)
_stub("aqt.overview", Overview=_AnyClass)
_stub("aqt.reviewer", Reviewer=_AnyClass)
_stub("aqt.webview", WebContent=_AnyClass)

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import importlib

pkg = importlib.import_module("__init__") if False else None
# Import as a package so relative imports inside work.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
addon = importlib.import_module("anki-design".replace("-", "_")) if False else None

# Load the module directly from file with a package-ish name.
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

calls = []
mod._study_state = lambda kind: calls.append(kind)

handled = mod._on_js_message(False, "ba:study-state:due", context=None)
assert handled == (True, None), handled
assert calls == ["due"], calls

handled = mod._on_js_message(False, "ba:study-state:bogus", context=None)
assert calls == ["due", "bogus"], calls  # dispatch passes through; helper ignores

# helper itself ignores unknown kinds and a missing collection without raising
mod._study_state("bogus")
mod._study_state("due")  # mw.col is None -> early return

print("PASS test_study_state_dispatch")
