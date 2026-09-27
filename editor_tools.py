"""Media storage and clipboard access for the shared editor image tools.

Three pycmds from web/editor-tools.js land here:

- ``ba:image-crop:<png b64>``  store a freshly cropped PNG in the media folder.
- ``ba:image-paste:<b64>``     store an image pasted into the reviewer's quick
  editor (Anki's full editor has its own paste path; the quick editor's
  contenteditables had none, so a clipboard bitmap ended up inline as a
  base64 data: URL or nowhere at all).
- ``ba:image-copy:<png b64>``  put the image on the system clipboard, for the
  context menu's Copy image / Cut image. Done here rather than through
  navigator.clipboard so it works inside QtWebEngine without a permission
  prompt and pastes into any field, Anki's full editor, or another app.
"""

from __future__ import annotations

import base64
import hashlib

MAX_IMAGE_BYTES = 32 * 1024 * 1024

# Signature -> extension for what a clipboard or data: URL can carry.
_SIGNATURES = (
    (b"\x89PNG\r\n\x1a\n", ".png"),
    (b"\xff\xd8\xff", ".jpg"),
    (b"GIF87a", ".gif"),
    (b"GIF89a", ".gif"),
)


def image_extension(data: bytes) -> str | None:
    for magic, ext in _SIGNATURES:
        if data.startswith(magic):
            return ext
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    return None


def _decode(payload: str) -> bytes:
    if len(payload) > MAX_IMAGE_BYTES:
        raise ValueError("The image is too large.")
    return base64.b64decode(payload, validate=True)


def _decode_image(payload: str, png_only: bool):
    """Base64 payload -> (bytes, extension, QImage), or ValueError."""
    from aqt.qt import QImage

    data = _decode(payload)
    ext = image_extension(data)
    if ext is None or (png_only and ext != ".png"):
        raise ValueError("The image must be a PNG." if png_only else "The clipboard does not hold an image.")
    image = QImage.fromData(data, ext[1:].upper())
    if image.isNull():
        raise ValueError("The image could not be read.")
    return data, ext, image


def _hashed_name(prefix: str, data: bytes, ext: str) -> str:
    return prefix + hashlib.sha256(data).hexdigest()[:24] + ext


def save_crop(payload: str, media) -> str:
    # The browser sends a newly rendered PNG, never a destination path.
    data, ext, _ = _decode_image(payload, png_only=True)
    return media.write_data(_hashed_name("anki-crop-", data, ext), data)


def save_pasted_image(payload: str, media) -> str:
    """Store a pasted image under a content-hashed name, like Anki's own
    paste-<checksum> files, so pasting the same image twice adds one file."""
    data, ext, _ = _decode_image(payload, png_only=False)
    return media.write_data(_hashed_name("paste-", data, ext), data)


def copy_image(payload: str, clipboard) -> None:
    """Put a PNG on the clipboard as both a Qt image and raw PNG bytes."""
    from aqt.qt import QByteArray, QMimeData

    data, _, image = _decode_image(payload, png_only=True)
    mime = QMimeData()
    mime.setImageData(image)
    mime.setData("image/png", QByteArray(data))
    clipboard.setMimeData(mime)


def handle(command: str, payload: str, media, clipboard) -> dict:
    """Run one image command; the dict goes back to the JS callback."""
    if command == "crop":
        return {"filename": save_crop(payload, media)}
    if command == "paste":
        return {"filename": save_pasted_image(payload, media)}
    if command == "copy":
        copy_image(payload, clipboard)
        return {"ok": True}
    raise ValueError("Unknown image command.")


def on_message(handled, message, context):
    from aqt import mw
    from aqt.editor import Editor
    from aqt.qt import QApplication
    from aqt.reviewer import Reviewer

    prefix = "ba:image-"
    if handled[0] or not isinstance(message, str) or not message.startswith(prefix):
        return handled
    command, _, payload = message[len(prefix):].partition(":")
    if command not in ("crop", "paste", "copy"):
        return handled
    if not isinstance(context, (Editor, Reviewer)) or not mw.col:
        return (True, {"error": "The editor is no longer open."})
    try:
        return (True, handle(command, payload, mw.col.media, QApplication.clipboard()))
    except Exception as exc:
        return (True, {"error": str(exc) or "Could not handle the image."})
