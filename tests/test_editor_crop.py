"""Run with Anki's Python environment: python tests/test_editor_crop.py.

save_crop() round-trips real PNG bytes through QImage (decode, re-encode,
alpha channel, dimensions), so unlike the widget-behavior tests elsewhere in
this directory a fake Qt stub can't stand in for it here — there is no
"same fake-Qt fallback pattern" that would exercise a real PNG codec. This
falls back instead:

1. `aqt.qt` (the real add-on runtime's Qt re-export module), if a full aqt
   is importable.
2. Plain PyQt6, if aqt isn't importable but the Qt bindings are (e.g. the
   Anki source checkout's pyenv, which has PyQt6 but not a built `aqt`
   package — it's missing the generated anki.buildinfo module). Stubs a
   minimal aqt.qt into sys.modules from it so editor_tools.py's own
   `from aqt.qt import QImage` resolves the same way.
3. Neither importable (plain system python3 with no Qt bindings at all)
   prints a SKIP line and exits 0 instead of failing at import.
"""

import base64
import importlib.util
import sys
import types
from pathlib import Path
import unittest

try:
    from aqt.qt import QBuffer, QIODevice, QImage
except Exception:
    try:
        from PyQt6.QtCore import QBuffer, QIODevice
        from PyQt6.QtGui import QImage
    except Exception:
        print("SKIP test_editor_crop: no Qt bindings available (need aqt or PyQt6)")
        sys.exit(0)
    if "aqt.qt" not in sys.modules:
        aqt_qt = types.ModuleType("aqt.qt")
        aqt_qt.QBuffer = QBuffer
        aqt_qt.QIODevice = QIODevice
        aqt_qt.QImage = QImage
        aqt_pkg = sys.modules.setdefault("aqt", types.ModuleType("aqt"))
        aqt_pkg.qt = aqt_qt
        sys.modules["aqt.qt"] = aqt_qt

spec = importlib.util.spec_from_file_location(
    "editor_tools", Path(__file__).parents[1] / "editor_tools.py"
)
tools = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tools)


class Media:
    def __init__(self):
        self.files = {"original.png": b"original media"}

    def write_data(self, name, data):
        self.files[name] = data
        return name


class CropStorageTests(unittest.TestCase):
    def test_stores_valid_png_without_overwriting_original(self):
        image = QImage(32, 16, QImage.Format.Format_ARGB32)
        image.fill(0)
        buffer = QBuffer()
        buffer.open(QIODevice.OpenModeFlag.WriteOnly)
        image.save(buffer, "PNG")
        data = bytes(buffer.data())
        payload = base64.b64encode(data).decode()
        media = Media()
        name = tools.save_crop(payload, media)
        self.assertEqual(name, tools.save_crop(payload, media))
        self.assertEqual(media.files["original.png"], b"original media")
        self.assertEqual(len(media.files), 2)
        stored = QImage.fromData(media.files[name], "PNG")
        self.assertEqual((stored.width(), stored.height()), (32, 16))
        self.assertTrue(stored.hasAlphaChannel())

    def test_rejects_invalid_or_oversized_data_without_writing(self):
        for payload in ["not base64!", base64.b64encode(b"not png").decode(),
                        base64.b64encode(b"\x89PNG\r\n\x1a\ninvalid").decode(),
                        "a" * (32 * 1024 * 1024 + 1)]:
            media = Media()
            with self.assertRaises(ValueError):
                tools.save_crop(payload, media)
            self.assertEqual(media.files, {"original.png": b"original media"})


if __name__ == "__main__":
    unittest.main()
