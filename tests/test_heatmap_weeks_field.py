"""Regression test: the "Minimum weeks shown" spinbox in the Anki Design
settings page must be adjustable regardless of the heatmap toggle.

The page used to call `setEnabled(False)` on the whole weeks row whenever
"Show review-activity heatmap" was off. The page's stylesheet pins the
spinbox/label colours, so a disabled row looked *exactly* like an enabled
one — the user saw a normal spinbox that silently ignored every click,
keystroke and scroll ("Can't adjust minimum weeks shown"). The value is
harmless to edit while the heatmap is off (it just gets stored for when
the heatmap is turned on), so the row simply stays live now.

Runs standalone: `python3 tests/test_heatmap_weeks_field.py`. Builds the
real `AnkiDesignSettingsPage` with a stubbed `aqt.mw`. Uses PyQt6
offscreen when it's importable; otherwise a small fake Qt (below) that
models the bits this test cares about — widget parenting, the enabled
flag's ancestor semantics, and the spinbox / checkbox signals — while
every other Qt call is a tolerant no-op.
"""

import importlib
import os
import sys
import types

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PyQt6 import QtCore, QtGui, QtWidgets
    _REAL_QT = True
except Exception:
    _REAL_QT = False


def _stub(name, **attrs):
    mod = sys.modules.get(name) or types.ModuleType(name)
    for k, v in attrs.items():
        setattr(mod, k, v)
    sys.modules[name] = mod
    return mod


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
    """Just enough of aqt's AddonManager for the settings page: an
    in-memory config that records every write."""

    def __init__(self, cfg):
        self.cfg = dict(cfg)
        self.writes = []

    def getConfig(self, *a, **k):
        return dict(self.cfg)

    def writeConfig(self, addon, cfg):
        self.cfg = dict(cfg)
        self.writes.append(dict(cfg))

    def addonConfigDefaults(self, *a, **k):
        return {}

    def addonFromModule(self, m):
        return str(m).split(".")[0]

    def setWebExports(self, *a, **k):
        pass

    def setConfigUpdatedAction(self, *a, **k):
        pass


# --------------------------------------------------------------------------- #
# Fake Qt (fallback when PyQt6 isn't installed)
# --------------------------------------------------------------------------- #
class _Any:
    """A value that survives whatever the page does with it: callable,
    attribute-chainable, falsy, empty, zero. Stands in for enums, colours,
    fonts, sizes and every unmodelled return value."""

    def __getattr__(self, name):
        return _Any()

    def __call__(self, *a, **k):
        return _Any()

    def __getitem__(self, k):
        return _Any()

    def __iter__(self):
        return iter(())

    def __len__(self):
        return 0

    def __bool__(self):
        return False

    def __int__(self):
        return 0

    def __index__(self):
        return 0

    def __float__(self):
        return 0.0

    def __str__(self):
        return ""

    def __format__(self, spec):
        return ""

    def __eq__(self, other):
        return False

    def __hash__(self):
        return 0

    def __lt__(self, other):
        return False

    def __le__(self, other):
        return False

    def __gt__(self, other):
        return False

    def __ge__(self, other):
        return False

    def __add__(self, other):
        return _Any()

    __radd__ = __sub__ = __rsub__ = __mul__ = __rmul__ = __add__
    __truediv__ = __rtruediv__ = __floordiv__ = __mod__ = __add__


class _Signal:
    def __init__(self, owner):
        self._owner = owner
        self._slots = []

    def connect(self, fn):
        self._slots.append(fn)

    def disconnect(self, *a):
        self._slots.clear()

    def emit(self, *args):
        if getattr(self._owner, "_signals_blocked", False):
            return
        for fn in list(self._slots):
            fn(*args)


class _Meta(type):
    """Class-level attribute access (`Qt.AlignmentFlag.X`,
    `QFontDatabase.families()`, `QApplication.palette()`) yields _Any."""

    def __getattr__(cls, name):
        return _Any()


class _Obj(metaclass=_Meta):
    def __init__(self, *a, **k):
        self._parent = None
        self._children = []
        self._enabled = True
        self._props = {}
        self._text = a[0] if a and isinstance(a[0], str) else ""
        parent = next((x for x in a if isinstance(x, _Widget)), k.get("parent"))
        if parent is not None:
            self.setParent(parent)

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        return _Any()

    # -- tree -- #
    def setParent(self, parent):
        if self._parent is not None and self in self._parent._children:
            self._parent._children.remove(self)
        self._parent = parent
        if parent is not None:
            parent._children.append(self)

    def parent(self):
        return self._parent

    def parentWidget(self):
        p = self._parent
        while p is not None and not isinstance(p, _Widget):
            p = p._parent
        return p

    def findChildren(self, cls):
        out = []
        for c in self._children:
            if isinstance(c, cls):
                out.append(c)
            out.extend(c.findChildren(cls))
        return out

    def deleteLater(self):
        pass

    # -- state -- #
    def setEnabled(self, b):
        self._enabled = bool(b)

    def isEnabled(self):
        if not self._enabled:
            return False
        p = self.parentWidget()
        return True if p is None else p.isEnabled()

    def setProperty(self, k, v):
        self._props[k] = v
        return True

    def property(self, k):
        return self._props.get(k)

    def setText(self, t):
        self._text = t

    def text(self):
        return self._text

    def blockSignals(self, b):
        was = getattr(self, "_signals_blocked", False)
        self._signals_blocked = bool(b)
        return was


class _Widget(_Obj):
    def setLayout(self, layout):
        layout._adopt(self)


class _Layout(_Obj):
    def __init__(self, *a, **k):
        super().__init__()
        self._owner = None
        self._pending = []
        owner = next((x for x in a if isinstance(x, _Widget)), None)
        if owner is not None:
            self._adopt(owner)

    def _adopt(self, owner):
        self._owner = owner
        for item in self._pending:
            if isinstance(item, _Layout):
                item._adopt(owner)
            else:
                item.setParent(owner)
        self._pending = []

    def addWidget(self, w, *a, **k):
        if self._owner is not None:
            w.setParent(self._owner)
        else:
            self._pending.append(w)

    def addLayout(self, l, *a, **k):
        if self._owner is not None:
            l._adopt(self._owner)
        else:
            self._pending.append(l)

    insertWidget = addWidget


class _ScrollArea(_Widget):
    def setWidget(self, w):
        w.setParent(self)


class _SpinBox(_Widget):
    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self._min, self._max, self._val = 0, 99, 0
        self.valueChanged = _Signal(self)

    def setRange(self, lo, hi):
        self._min, self._max = int(lo), int(hi)

    def setMinimum(self, lo):
        self._min = int(lo)

    def setMaximum(self, hi):
        self._max = int(hi)

    def minimum(self):
        return self._min

    def maximum(self):
        return self._max

    def value(self):
        return self._val

    def setValue(self, v):
        v = max(self._min, min(self._max, int(v)))
        if v != self._val:
            self._val = v
            self.valueChanged.emit(v)

    def stepUp(self):
        self.setValue(self._val + 1)

    def stepDown(self):
        self.setValue(self._val - 1)


class _CheckBox(_Widget):
    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self._checked = False
        self.toggled = _Signal(self)
        self.stateChanged = _Signal(self)
        self.clicked = _Signal(self)

    def setChecked(self, b):
        b = bool(b)
        if b != self._checked:
            self._checked = b
            self.toggled.emit(b)
            self.stateChanged.emit(2 if b else 0)

    def isChecked(self):
        return self._checked


class _ComboBox(_Widget):
    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self._items = []  # [text, data]
        self._idx = -1
        self._edit = ""
        self.currentIndexChanged = _Signal(self)

    def addItem(self, text, userData=None):
        self._items.append([text, userData])
        if self._idx < 0:
            self._idx = 0

    def insertSeparator(self, i):
        self._items.insert(i, [None, None])

    def count(self):
        return len(self._items)

    def itemData(self, i, role=None):
        return self._items[i][1] if 0 <= i < len(self._items) else None

    def setItemData(self, i, v, role=None):
        pass

    def itemText(self, i):
        return self._items[i][0] if 0 <= i < len(self._items) else ""

    def findData(self, d):
        for i, (_t, v) in enumerate(self._items):
            if v == d:
                return i
        return -1

    def findText(self, t):
        for i, (txt, _v) in enumerate(self._items):
            if txt == t:
                return i
        return -1

    def currentIndex(self):
        return self._idx

    def setCurrentIndex(self, i):
        i = int(i)
        if i != self._idx:
            self._idx = i
            self.currentIndexChanged.emit(i)

    def currentData(self):
        return self.itemData(self._idx)

    def currentText(self):
        return self._edit or self.itemText(self._idx)

    def setEditText(self, t):
        self._edit = t


class _ButtonGroup(_Obj):
    def __init__(self, *a, **k):
        super().__init__()
        self._buttons = {}
        self.idClicked = _Signal(self)
        self.buttonClicked = _Signal(self)
        self.idToggled = _Signal(self)

    def addButton(self, b, i=-1):
        self._buttons[i] = b

    def button(self, i):
        return self._buttons.get(i)

    def checkedId(self):
        for i, b in self._buttons.items():
            if getattr(b, "isChecked", lambda: False)():
                return i
        return -1


class _RadioButton(_CheckBox):
    pass


def _fake_qt_module():
    qt = _stub("aqt.qt")
    widget_names = [
        "QWidget", "QLabel", "QPushButton", "QLineEdit", "QFrame",
        "QDialog", "QDialogButtonBox", "QColorDialog",
    ]
    for n in widget_names:
        setattr(qt, n, type(n, (_Widget,), {}))
    for n in ("QVBoxLayout", "QHBoxLayout", "QGridLayout", "QFormLayout"):
        setattr(qt, n, type(n, (_Layout,), {}))
    qt.QScrollArea = type("QScrollArea", (_ScrollArea,), {})
    qt.QSpinBox = type("QSpinBox", (_SpinBox,), {})
    qt.QCheckBox = type("QCheckBox", (_CheckBox,), {})
    qt.QRadioButton = type("QRadioButton", (_RadioButton,), {})
    qt.QComboBox = type("QComboBox", (_ComboBox,), {})
    qt.QButtonGroup = type("QButtonGroup", (_ButtonGroup,), {})
    for n in (
        "QApplication", "QColor", "QFont", "QFontDatabase", "QIcon",
        "QPalette", "QSize", "QUrl", "Qt",
    ):
        setattr(qt, n, type(n, (_Obj,), {}))
    return qt


if _REAL_QT:
    # `aqt.qt` re-exports PyQt6's names; mirror that with the real modules
    # so the page builds genuine widgets.
    qt_mod = _stub("aqt.qt")
    for m in (QtCore, QtGui, QtWidgets):
        for name in dir(m):
            if not name.startswith("_"):
                setattr(qt_mod, name, getattr(m, name))
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    QSpinBox, QCheckBox = QtWidgets.QSpinBox, QtWidgets.QCheckBox
else:
    qt_mod = _fake_qt_module()
    QSpinBox, QCheckBox = qt_mod.QSpinBox, qt_mod.QCheckBox


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PKG = "ba_addon_settings_pkg"


def _load_settings(cfg):
    """Fresh import of settings.py as a submodule of a synthetic package
    rooted at the repo, so its `from . import colors/nature` resolve."""
    for k in list(sys.modules):
        if k == PKG or k.startswith(PKG + "."):
            del sys.modules[k]
    mw = types.SimpleNamespace(
        col=None, addonManager=_AddonManager(cfg), state="",
        onNoteTypes=lambda *a, **k: None,
    )
    mw.addonManager.onAddonsDialog = lambda *a, **k: None
    aqt = _stub("aqt", mw=mw, gui_hooks=_Hooks())
    aqt.qt = qt_mod
    pkg = types.ModuleType(PKG)
    pkg.__path__ = [ROOT]
    sys.modules[PKG] = pkg
    settings = importlib.import_module(f"{PKG}.settings")
    return settings, mw


def _weeks_spinbox(page):
    """The one QSpinBox with the heatmap-weeks range (8..260)."""
    boxes = [
        s for s in page.findChildren(QSpinBox)
        if s.minimum() == 8 and s.maximum() == 260
    ]
    assert len(boxes) == 1, f"expected one weeks spinbox, found {len(boxes)}"
    return boxes[0]


def _heatmap_toggle(page):
    toggles = [
        cb for cb in page.findChildren(QCheckBox)
        if "heatmap" in cb.text().lower()
    ]
    assert len(toggles) == 1, [cb.text() for cb in toggles]
    return toggles[0]


def test_weeks_field_is_live_while_heatmap_is_off():
    settings, mw = _load_settings({
        "theme": "dark", "show_heatmap": False, "heatmap_weeks": 53,
    })
    page = settings.AnkiDesignSettingsPage()
    spin = _weeks_spinbox(page)
    assert spin.value() == 53, spin.value()
    assert not _heatmap_toggle(page).isChecked()

    # isEnabled() is False if *any* ancestor is disabled — this is exactly
    # what the user hit: a normal-looking spinbox that ignored input.
    assert spin.isEnabled(), (
        "'Minimum weeks shown' is disabled while the heatmap toggle is off; "
        "the page's QSS gives no visual cue, so it just looks broken"
    )

    # And an edit made in that state must actually land in config.
    spin.setValue(20)
    assert mw.addonManager.cfg.get("heatmap_weeks") == 20, mw.addonManager.cfg
    page.deleteLater()


def test_weeks_field_stays_live_after_toggling_heatmap_off():
    settings, mw = _load_settings({
        "theme": "dark", "show_heatmap": True, "heatmap_weeks": 30,
    })
    page = settings.AnkiDesignSettingsPage()
    spin = _weeks_spinbox(page)
    assert spin.value() == 30, spin.value()
    assert spin.isEnabled()

    _heatmap_toggle(page).setChecked(False)
    assert mw.addonManager.cfg.get("show_heatmap") is False

    assert spin.isEnabled(), (
        "turning the heatmap off must not lock the weeks field"
    )
    spin.setValue(12)
    assert mw.addonManager.cfg.get("heatmap_weeks") == 12, mw.addonManager.cfg
    page.deleteLater()


if __name__ == "__main__":
    test_weeks_field_is_live_while_heatmap_is_off()
    test_weeks_field_stays_live_after_toggling_heatmap_off()
    print(f"PASS test_heatmap_weeks_field ({'PyQt6' if _REAL_QT else 'fake Qt'})")
