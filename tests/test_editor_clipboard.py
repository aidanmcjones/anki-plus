"""python3 tests/test_editor_clipboard.py

Ticket 20260923-102640: the editor image menu's Copy image / Cut image put
the bitmap on the system clipboard, and an image pasted into the reviewer's
quick editor is stored in the media folder instead of inline as base64.

Uses the real Qt bindings when aqt or PyQt6 is importable (the storage path
round-trips through QImage). Without them a small stand-in for aqt.qt is
installed: it recognises the same signatures editor_tools sniffs, records
what lands on the clipboard, and lets the naming, format and dispatch logic
run on a plain system python3.
"""

import base64
import importlib.util
import sys
import types
import unittest
from pathlib import Path

try:
    from aqt.qt import QBuffer, QByteArray, QIODevice, QImage, QMimeData
    REAL_QT = True
except Exception:
    try:
        from PyQt6.QtCore import QBuffer, QByteArray, QIODevice, QMimeData
        from PyQt6.QtGui import QImage
        REAL_QT = True
    except Exception:
        REAL_QT = False

if REAL_QT:
    if "aqt.qt" not in sys.modules:
        aqt_qt = types.ModuleType("aqt.qt")
        for name, obj in [("QBuffer", QBuffer), ("QByteArray", QByteArray), ("QIODevice", QIODevice),
                          ("QImage", QImage), ("QMimeData", QMimeData)]:
            setattr(aqt_qt, name, obj)
        aqt_pkg = sys.modules.setdefault("aqt", types.ModuleType("aqt"))
        aqt_pkg.qt = aqt_qt
        sys.modules["aqt.qt"] = aqt_qt
else:
    class QImage:
        _MAGIC = (b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff", b"GIF8", b"RIFF")

        def __init__(self, data=b""):
            self.data = data

        @classmethod
        def fromData(cls, data, fmt=None):
            return cls(bytes(data) if bytes(data).startswith(cls._MAGIC) and len(data) > 12 else b"")

        def isNull(self):
            return not self.data

    class QByteArray(bytes):
        pass

    class QMimeData:
        def __init__(self):
            self.image = None
            self.formats = {}

        def setImageData(self, image):
            self.image = image

        def setData(self, mime, data):
            self.formats[mime] = bytes(data)

    aqt_qt = types.ModuleType("aqt.qt")
    aqt_qt.QImage, aqt_qt.QByteArray, aqt_qt.QMimeData = QImage, QByteArray, QMimeData
    aqt_pkg = sys.modules.setdefault("aqt", types.ModuleType("aqt"))
    aqt_pkg.qt = aqt_qt
    sys.modules["aqt.qt"] = aqt_qt

spec = importlib.util.spec_from_file_location(
    "editor_tools", Path(__file__).parents[1] / "editor_tools.py"
)
tools = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tools)


def png_bytes() -> bytes:
    if REAL_QT:
        image = QImage(8, 4, QImage.Format.Format_ARGB32)
        image.fill(0)
        buffer = QBuffer()
        buffer.open(QIODevice.OpenModeFlag.WriteOnly)
        image.save(buffer, "PNG")
        return bytes(buffer.data())
    return b"\x89PNG\r\n\x1a\n" + b"\x00" * 40


def jpeg_bytes() -> bytes:
    if REAL_QT:
        image = QImage(8, 4, QImage.Format.Format_RGB32)
        image.fill(0)
        buffer = QBuffer()
        buffer.open(QIODevice.OpenModeFlag.WriteOnly)
        image.save(buffer, "JPG")
        return bytes(buffer.data())
    return b"\xff\xd8\xff\xe0" + b"\x00" * 40


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


class Media:
    def __init__(self):
        self.files = {"original.png": b"original media"}

    def write_data(self, name, data):
        self.files[name] = data
        return name


class Clipboard:
    def __init__(self):
        self.mime = None

    def setMimeData(self, mime):
        self.mime = mime


class PasteStorageTests(unittest.TestCase):
    def test_png_and_jpeg_are_stored_under_hashed_paste_names(self):
        media = Media()
        png = tools.save_pasted_image(b64(png_bytes()), media)
        jpg = tools.save_pasted_image(b64(jpeg_bytes()), media)
        self.assertTrue(png.startswith("paste-") and png.endswith(".png"), png)
        self.assertTrue(jpg.startswith("paste-") and jpg.endswith(".jpg"), jpg)
        self.assertEqual(media.files[png], png_bytes())
        self.assertEqual(media.files["original.png"], b"original media")
        # Same bytes again: same name, no second file.
        self.assertEqual(png, tools.save_pasted_image(b64(png_bytes()), media))
        self.assertEqual(len(media.files), 3)

    def test_rejects_non_images_without_writing(self):
        for payload in ["not base64!", b64(b"<svg onload=alert(1)></svg>"), b64(b"\x89PNG\r\n\x1a\n"),
                        "a" * (tools.MAX_IMAGE_BYTES + 1)]:
            media = Media()
            with self.assertRaises(ValueError):
                tools.save_pasted_image(payload, media)
            self.assertEqual(media.files, {"original.png": b"original media"})

    def test_extension_sniffing(self):
        self.assertEqual(tools.image_extension(b"GIF89a" + b"\x00" * 8), ".gif")
        self.assertEqual(tools.image_extension(b"RIFF\x00\x00\x00\x00WEBPVP8 "), ".webp")
        self.assertIsNone(tools.image_extension(b"RIFF\x00\x00\x00\x00WAVE"))
        self.assertIsNone(tools.image_extension(b""))


class CopyTests(unittest.TestCase):
    def test_copy_puts_image_and_png_bytes_on_the_clipboard(self):
        clipboard = Clipboard()
        tools.copy_image(b64(png_bytes()), clipboard)
        mime = clipboard.mime
        self.assertIsNotNone(mime)
        if REAL_QT:
            self.assertTrue(mime.hasImage())
            self.assertEqual(bytes(mime.data("image/png")), png_bytes())
        else:
            self.assertFalse(mime.image.isNull())
            self.assertEqual(mime.formats["image/png"], png_bytes())

    def test_copy_only_accepts_png(self):
        clipboard = Clipboard()
        with self.assertRaises(ValueError):
            tools.copy_image(b64(jpeg_bytes()), clipboard)
        self.assertIsNone(clipboard.mime)


class DispatchTests(unittest.TestCase):
    def test_handle_routes_each_command(self):
        media, clipboard = Media(), Clipboard()
        self.assertEqual(tools.handle("crop", b64(png_bytes()), media, clipboard)["filename"][:10], "anki-crop-")
        self.assertEqual(tools.handle("paste", b64(png_bytes()), media, clipboard)["filename"][:6], "paste-")
        self.assertEqual(tools.handle("copy", b64(png_bytes()), media, clipboard), {"ok": True})
        self.assertIsNotNone(clipboard.mime)
        with self.assertRaises(ValueError):
            tools.handle("delete", b64(png_bytes()), media, clipboard)


if __name__ == "__main__":
    unittest.main()
