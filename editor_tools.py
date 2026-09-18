"""Media storage for the shared editor crop tool."""

import base64
import hashlib


def save_crop(payload: str, media) -> str:
    from aqt.qt import QImage

    # The browser sends a newly rendered PNG, never a destination path.
    if len(payload) > 32 * 1024 * 1024:
        raise ValueError("The cropped image is too large.")
    data = base64.b64decode(payload, validate=True)
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ValueError("The cropped image must be a PNG.")
    image = QImage.fromData(data, "PNG")
    if image.isNull():
        raise ValueError("The cropped image could not be read.")
    name = "anki-crop-" + hashlib.sha256(data).hexdigest()[:24] + ".png"
    return media.write_data(name, data)


def on_message(handled, message, context):
    from aqt import mw
    from aqt.editor import Editor
    from aqt.reviewer import Reviewer

    prefix = "ba:image-crop:"
    if handled[0] or not isinstance(message, str) or not message.startswith(prefix):
        return handled
    if not isinstance(context, (Editor, Reviewer)) or not mw.col:
        return (True, {"error": "The editor is no longer open."})
    try:
        return (True, {"filename": save_crop(message[len(prefix):], mw.col.media)})
    except Exception as exc:
        return (True, {"error": str(exc) or "Could not save the cropped image."})
