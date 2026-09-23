"""Let in-page HTML5 drops reach the main webview's page.

Anki's AnkiWebView.dropEvent swallows every drop unless the view's
`allow_drops` is True, and every setHtml()/load_url() resets it to False.
The deck browser and the congrats page are rendered with setHtml, so on
those screens Chromium saw dragover (the insertion line drew) but never
drop: the deck list's `ba:deck:reorder` and `ba:deck:reparent` pycmds
could not fire in the real app.

Anki blocks drops so a file dragged in from Finder cannot navigate the
view away. That guard stays: only a drag that started inside the app
(`source()` set) or carries no URLs/files is passed on to Chromium, and
the page's own drop handlers decide what happens. Everything else still
goes through Anki's own dropEvent.
"""

from typing import Any

_MARK = "_ba_drop_passthrough"


def is_in_page_drag(evt: Any) -> bool:
    try:
        if evt.source() is not None:
            return True
    except Exception:
        pass
    try:
        md = evt.mimeData()
        return md is not None and not md.hasUrls()
    except Exception:
        return False


def install(web: Any) -> bool:
    """Wrap `web.dropEvent` once. Returns True when the wrapper is in place."""
    if web is None:
        return False
    if getattr(web, _MARK, False):
        return True
    try:
        from aqt.qt import QWebEngineView
    except Exception:
        return False
    anki_drop = web.dropEvent

    def dropEvent(evt: Any) -> None:
        if evt is not None and not getattr(web, "allow_drops", False) \
                and is_in_page_drag(evt):
            QWebEngineView.dropEvent(web, evt)
            return
        anki_drop(evt)

    try:
        web.dropEvent = dropEvent
        setattr(web, _MARK, True)
    except Exception:
        return False
    return True
