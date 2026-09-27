"""The AnkiHub add-on's unsolicited "Sign in to AnkiHub." window must not
appear after an AnkiWeb sync, while deliberate sign-in paths keep working.

Runs standalone: `python3 tests/test_ankihub_login_nag.py`. Stubs aqt and a
minimal AnkiHub add-on (package `1322529746`, its AnkiWeb id) before
importing the add-on.

Background: AnkiHub wraps `AnkiQt._sync_collection_and_media` and, with its
default `auto_sync: on_ankiweb_sync`, its after_sync callback calls
`AnkiHubLogin.display_login()` whenever the user is not signed in. That
callback resolves `AnkiHubLogin` as a module global of
`<ankihub>.gui.auto_sync` at call time. The startup sync runs before our
quiet-sync patch replaces the instance method, so without a fix the prompt
appears on every launch.
"""

import importlib.util
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

    def fire(self, *a, **k):
        for fn in list(self.fns):
            fn(*a, **k)


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


# --- Minimal AnkiHub add-on -------------------------------------------------
class FakeAnkiHubLogin:
    """Stands in for ankihub.gui.menu.AnkiHubLogin."""

    shown = 0

    @classmethod
    def display_login(cls, on_success=None):
        cls.shown += 1
        return cls


def _make_auto_sync_module(name, login_cls):
    """Mirror the shape of ankihub/gui/auto_sync.py: the login class is a
    module global, looked up at call time from the after_sync callback."""
    mod = _stub(name, AnkiHubLogin=login_cls, logged_in=False)

    def new_after_sync():
        if not mod.logged_in:
            mod.AnkiHubLogin.display_login()

    mod.new_after_sync = new_after_sync
    return mod


_stub("1322529746")
_stub("1322529746.gui")
menu = _stub("1322529746.gui.menu", AnkiHubLogin=FakeAnkiHubLogin)
auto_sync = _make_auto_sync_module("1322529746.gui.auto_sync", FakeAnkiHubLogin)

# --- Import the add-on ------------------------------------------------------
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
spec = importlib.util.spec_from_file_location(
    "ba_addon", os.path.join(os.path.dirname(__file__), "..", "__init__.py"),
)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

# 1. The after-sync nag is silenced once the add-on has loaded.
auto_sync.new_after_sync()
assert FakeAnkiHubLogin.shown == 0, (
    "AnkiHub login window shown after AnkiWeb sync (shown=%d)" % FakeAnkiHubLogin.shown
)

# 2. Deliberate sign-in (AnkiHub menu, Preferences > Syncing) still opens it.
menu.AnkiHubLogin.display_login()
assert FakeAnkiHubLogin.shown == 1, FakeAnkiHubLogin.shown

# 3. Signed-in users are unaffected: the callback simply never prompts.
auto_sync.logged_in = True
auto_sync.new_after_sync()
assert FakeAnkiHubLogin.shown == 1, FakeAnkiHubLogin.shown

# 4. An AnkiHub loaded after us (or under a different package name) is
#    quieted before its after_sync can fire: sync_will_start runs inside
#    the native sync, ahead of the after_sync callback.
class LateLogin(FakeAnkiHubLogin):
    shown = 0


_stub("ankihub")
_stub("ankihub.gui")
late = _make_auto_sync_module("ankihub.gui.auto_sync", LateLogin)
aqt.gui_hooks.sync_will_start.fire()
late.new_after_sync()
assert LateLogin.shown == 0, "late-loaded AnkiHub still nags (shown=%d)" % LateLogin.shown

# 5. Modules that merely share the suffix but are not AnkiHub are untouched.
other = _stub("other.gui.auto_sync", unrelated=True)
aqt.gui_hooks.profile_did_open.fire()
assert not hasattr(other, "AnkiHubLogin"), "stand-in injected into an unrelated module"

print("PASS test_ankihub_login_nag")
