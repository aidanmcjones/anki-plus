"""Regression tests for the Backdrop block of the settings page: the Scene
and Nature-video pickers must stay usable whatever Backdrop is set to, and
picking from one of them switches Backdrop to the matching mode.

The bug: with Backdrop on "Scenes", the whole Nature-video section (the
quick-pick dropdown and the scrollable clip list) was `setEnabled(False)`.
The page's stylesheet gives disabled dropdowns and checkboxes no distinct
look, so the user saw a dropdown that wouldn't open and a clip list that
wouldn't scroll — with no hint as to why.

Runs standalone (no Anki or PyQt install needed):
`python3 tests/test_settings_backdrop.py`. `aqt` is stubbed with a small
fake Qt that models just enough — parent chains, inherited enabled state,
combo / checkbox / radio signals — to build and drive that one block of
the page through `AnkiDesignSettingsPage._add_backdrop_rows`.
"""

import importlib.util
import json
import os
import sys
import tempfile
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


# --------------------------------------------------------------------------- #
# Fake Qt
# --------------------------------------------------------------------------- #

class _Signal:
    def __init__(self):
        self._slots = []

    def connect(self, fn):
        self._slots.append(fn)

    def emit(self, *args):
        for fn in list(self._slots):
            fn(*args)


class _NoOp:
    """Any Qt setter we don't model (setContentsMargins, setSpacing,
    setFixedHeight, …) is accepted and ignored."""

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        return lambda *a, **k: None


class QWidget(_NoOp):
    def __init__(self, parent=None):
        self._parent = None
        self._children = []
        self._enabled = True
        self._blocked = False
        self._props = {}
        if parent is not None:
            self.setParent(parent)

    def setParent(self, parent):
        if self._parent is not None:
            self._parent._children.remove(self)
        self._parent = parent
        if parent is not None:
            parent._children.append(self)

    def parentWidget(self):
        return self._parent

    def setEnabled(self, on):
        self._enabled = bool(on)

    def isEnabled(self):
        # Qt semantics: a widget is only effectively enabled when every
        # ancestor is too — disabling a container disables its children.
        w = self
        while w is not None:
            if not w._enabled:
                return False
            w = w._parent
        return True

    def setProperty(self, key, value):
        self._props[key] = value

    def property(self, key):
        return self._props.get(key)

    def blockSignals(self, block):
        self._blocked = bool(block)

    def findChildren(self, cls):
        out = []
        for c in self._children:
            if isinstance(c, cls):
                out.append(c)
            out.extend(c.findChildren(cls))
        return out


class _Layout(_NoOp):
    def __init__(self, owner=None):
        self._owner = owner
        self._widgets = []

    def addWidget(self, w, *a, **k):
        self._widgets.append(w)
        if self._owner is not None:
            w.setParent(self._owner)

    def addLayout(self, layout, *a, **k):
        layout._owner = self._owner
        for w in layout._widgets:
            w.setParent(self._owner)


class QVBoxLayout(_Layout):
    pass


class QHBoxLayout(_Layout):
    pass


class QLabel(QWidget):
    def __init__(self, text="", parent=None):
        super().__init__(parent)
        self._text = text

    def text(self):
        return self._text


class QFrame(QWidget):
    pass


class QScrollArea(QWidget):
    def setWidget(self, w):
        w.setParent(self)


class QAbstractButton(QWidget):
    def __init__(self, text="", parent=None):
        super().__init__(parent)
        self._text = text
        self._checked = False
        self._group = None
        self.toggled = _Signal()
        self.clicked = _Signal()

    def text(self):
        return self._text

    def isChecked(self):
        return self._checked

    def setChecked(self, on):
        on = bool(on)
        if on == self._checked:
            return
        self._checked = on
        if on and self._group is not None:
            for other in self._group._buttons:
                if other is not self and other._checked:
                    other.setChecked(False)
        if not self._blocked:
            self.toggled.emit(on)

    def click(self):
        self.setChecked(not self._checked)
        self.clicked.emit()


class QCheckBox(QAbstractButton):
    pass


class QRadioButton(QAbstractButton):
    pass


class QPushButton(QAbstractButton):
    pass


class QButtonGroup(_NoOp):
    def __init__(self, parent=None):
        self._buttons = []

    def addButton(self, button, *a):
        self._buttons.append(button)
        button._group = self


class QComboBox(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._items = []
        self._idx = -1
        self.currentIndexChanged = _Signal()

    def addItem(self, label, data=None):
        self._items.append((label, data))
        if self._idx < 0:
            self._idx = 0

    def findData(self, data):
        for i, (_label, d) in enumerate(self._items):
            if d == data:
                return i
        return -1

    def setCurrentIndex(self, i):
        if i == self._idx:
            return
        self._idx = i
        if not self._blocked:
            self.currentIndexChanged.emit(i)

    def currentIndex(self):
        return self._idx

    def currentData(self):
        return self._items[self._idx][1] if self._idx >= 0 else None

    def currentText(self):
        return self._items[self._idx][0] if self._idx >= 0 else ""

    def itemData(self, i):
        return self._items[i][1]

    def count(self):
        return len(self._items)


class QSpinBox(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._value = 0
        self.valueChanged = _Signal()

    def setValue(self, v):
        if v == self._value:
            return
        self._value = v
        if not self._blocked:
            self.valueChanged.emit(v)

    def value(self):
        return self._value


class QLineEdit(QWidget):
    pass


class QDialog(QWidget):
    pass


class QDialogButtonBox(QWidget):
    pass


class _Anything(_NoOp):
    """Stands in for Qt namespaces/enums/value types (Qt, QSize, QFont,
    QColor, …): any attribute access or call yields another _Anything."""

    def __call__(self, *a, **k):
        return _Anything()

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        return _Anything()


def _stub(name, **attrs):
    mod = sys.modules.get(name) or types.ModuleType(name)
    for k, v in attrs.items():
        setattr(mod, k, v)
    sys.modules[name] = mod
    return mod


class _AddonManager:
    def __init__(self, cfg):
        self.cfg = dict(cfg)
        self.writes = []

    def getConfig(self, *a, **k):
        return dict(self.cfg)

    def writeConfig(self, _addon, cfg):
        self.cfg = dict(cfg)
        self.writes.append(dict(cfg))

    def addonConfigDefaults(self, *a, **k):
        return {}


_mw = types.SimpleNamespace(addonManager=_AddonManager({}), state="deckBrowser")

_stub("aqt", mw=_mw)
_stub(
    "aqt.qt",
    QApplication=_Anything(), QButtonGroup=QButtonGroup, QCheckBox=QCheckBox,
    QColor=_Anything(), QColorDialog=_Anything(), QComboBox=QComboBox,
    QDialog=QDialog, QDialogButtonBox=QDialogButtonBox, QFont=_Anything(),
    QFontDatabase=_Anything(), QFrame=QFrame, QHBoxLayout=QHBoxLayout,
    QIcon=_Anything(), QLabel=QLabel, QLineEdit=QLineEdit,
    QPalette=_Anything(), QPushButton=QPushButton, QRadioButton=QRadioButton,
    QScrollArea=QScrollArea, QSize=_Anything(), QSpinBox=QSpinBox,
    Qt=_Anything(), QUrl=_Anything(), QVBoxLayout=QVBoxLayout, QWidget=QWidget,
)

import nature  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "ba_settings", os.path.join(ROOT, "settings.py")
)
settings = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(settings)
# Loaded as a bare module, so `from . import nature` inside settings.py fell
# back to None — hand it the real library reader so the clip list builds.
settings._nature = nature

# Building the whole page needs far more Qt than this fake models; the
# tests only exercise the Backdrop block, so build nothing by default and
# call _add_backdrop_rows explicitly.
settings.AnkiDesignSettingsPage._build = lambda self: None


# --------------------------------------------------------------------------- #
# Harness
# --------------------------------------------------------------------------- #

_LIBRARY = {
    "videos": [
        {"file": "desert/dunes.webm", "biome": "desert", "title": "Huacachina Dunes"},
        {"file": "forest/wolf.webm", "biome": "forest", "title": "Gray Wolf"},
        {"file": "forest/eagle.webm", "biome": "forest", "title": "Bald Eagle"},
    ]
}


class _Backdrop:
    """Builds just the Backdrop block against a throwaway nature library
    and the given config, and exposes the controls a user would touch."""

    def __init__(self, cfg):
        self._tmp = tempfile.TemporaryDirectory()
        self._orig = (nature.ROOT, nature.INDEX_PATH)
        nature.ROOT = self._tmp.name
        nature.INDEX_PATH = os.path.join(nature.ROOT, "index.json")
        for entry in _LIBRARY["videos"]:
            full = os.path.join(nature.ROOT, entry["file"])
            os.makedirs(os.path.dirname(full), exist_ok=True)
            with open(full, "wb") as fh:
                fh.write(b"\0")
        with open(nature.INDEX_PATH, "w", encoding="utf-8") as fh:
            json.dump(_LIBRARY, fh)

        self.manager = _AddonManager({"theme": "dark", **cfg})
        _mw.addonManager = self.manager
        self.page = settings.AnkiDesignSettingsPage()
        self.root = QWidget()
        self.page._add_backdrop_rows(QVBoxLayout(self.root))

    def close(self):
        nature.ROOT, nature.INDEX_PATH = self._orig
        self._tmp.cleanup()

    @property
    def cfg(self):
        return self.manager.cfg

    def radio(self, label):
        return next(
            rb for rb in self.root.findChildren(QRadioButton) if rb.text() == label
        )

    def combo_with(self, data):
        return next(
            c for c in self.root.findChildren(QComboBox) if c.findData(data) >= 0
        )

    @property
    def video_combo(self):
        return self.combo_with("__custom__")

    @property
    def scene_combo(self):
        return self.combo_with("dunes")

    @property
    def clip_boxes(self):
        return [cb for cb in self.root.findChildren(QCheckBox) if cb.property("file")]

    @property
    def clip_list(self):
        return self.root.findChildren(QScrollArea)[0]


def _run(cfg, body):
    b = _Backdrop(cfg)
    try:
        body(b)
    finally:
        b.close()


# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #

def test_video_picker_stays_usable_when_backdrop_is_scenes():
    # The ticket: Backdrop on Scenes, a curated library on disk — the
    # Nature-video dropdown wouldn't open and the clip list wouldn't scroll,
    # because the whole section had been setEnabled(False).
    def body(b):
        assert b.video_combo.isEnabled(), "video dropdown is disabled"
        assert b.clip_list.isEnabled(), "clip list is disabled"
        assert all(cb.isEnabled() for cb in b.clip_boxes), "clip checkboxes disabled"
        assert b.scene_combo.isEnabled()
    _run({"backdrop": "scene", "video_selection": {"mode": "custom", "files": ["forest/wolf.webm"]}}, body)


def test_scene_picker_stays_usable_when_backdrop_is_video():
    def body(b):
        assert b.scene_combo.isEnabled()
        assert b.video_combo.isEnabled()
    _run({"backdrop": "video"}, body)


def test_checking_a_clip_switches_backdrop_to_video():
    def body(b):
        wolf = next(cb for cb in b.clip_boxes if cb.property("file") == "forest/wolf.webm")
        wolf.setChecked(True)
        assert b.cfg["video_selection"] == {"mode": "custom", "files": ["forest/wolf.webm"]}
        assert b.cfg["backdrop"] == "video", b.cfg
        assert b.radio("Nature video").isChecked()
        assert not b.radio("Scenes").isChecked()
        assert b.video_combo.currentData() == "__custom__"
    _run({"backdrop": "scene"}, body)


def test_picking_from_video_dropdown_switches_backdrop_to_video():
    def body(b):
        combo = b.video_combo
        combo.setCurrentIndex(combo.findData("biome:forest"))
        assert b.cfg["video_selection"] == "biome:forest"
        assert b.cfg["backdrop"] == "video", b.cfg
        assert b.radio("Nature video").isChecked()
    _run({"backdrop": "aurora"}, body)


def test_picking_a_scene_switches_backdrop_to_scenes():
    def body(b):
        combo = b.scene_combo
        combo.setCurrentIndex(combo.findData("dunes"))
        assert b.cfg["scene"] == "dunes"
        assert b.cfg["backdrop"] == "scene", b.cfg
        assert b.radio("Scenes").isChecked()
        assert not b.radio("Nature video").isChecked()
    _run({"backdrop": "video"}, body)


def test_backdrop_radio_still_writes_and_pickers_stay_live():
    def body(b):
        b.radio("Aurora").setChecked(True)
        assert b.cfg["backdrop"] == "aurora"
        assert b.video_combo.isEnabled()
        assert b.scene_combo.isEnabled()
        # Re-picking the mode already active writes nothing new.
        n = len(b.manager.writes)
        b.radio("Aurora").setChecked(True)
        assert len(b.manager.writes) == n
    _run({"backdrop": "scene"}, body)


def test_building_the_block_writes_no_config():
    def body(b):
        assert b.manager.writes == []
        assert b.radio("Scenes").isChecked()
        assert b.video_combo.currentData() == "__custom__"
        assert [cb.property("file") for cb in b.clip_boxes if cb.isChecked()] == ["forest/eagle.webm"]
    _run({"backdrop": "scene", "video_selection": {"mode": "custom", "files": ["forest/eagle.webm"]}}, body)


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"PASS {name}")
    print("all tests passed")
