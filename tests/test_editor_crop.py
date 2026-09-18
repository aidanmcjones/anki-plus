"""Run with Anki's Python environment: python tests/test_editor_crop.py."""

import base64
import importlib.util
from pathlib import Path
import unittest

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
        from aqt.qt import QBuffer, QIODevice, QImage

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
