"""Let in-page HTML5 drags and drops reach the main webview's page.

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
goes through Anki's own handlers (MainWebView imports a dropped file).
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


_EVENTS = ("dragEnterEvent", "dragMoveEvent", "dropEvent")


def install(web: Any) -> bool:
    """Wrap the view's drag enter / move / drop handlers once. Returns True
    when the wrappers are in place.

    Two layers swallow an in-page drag on the main window, and Qt only
    delivers dragMove/drop for a drag whose enter was accepted, so both
    must let it through:
      * AnkiWebView.dropEvent drops everything while `allow_drops` is False;
      * MainWebView.dragEnterEvent / dropEvent (aqt/main.py) treat every
        drag on the deck browser as a file import: a drag without file URLs
        is never accepted and never handed on, so Chromium sees no
        dragenter/dragover/drop at all.
    For an in-page drag each wrapper calls QWebEngineView's own handler,
    so the page's HTML5 handlers decide. Anything else (a file from
    Finder) goes to Anki's handler unchanged, so drag-to-import still works.
    Instance attributes, so the fork is not edited and any AnkiWebView
    subclass is covered."""
    if web is None:
        return False
    if getattr(web, _MARK, False):
        return True
    try:
        from aqt.qt import QWebEngineView
    except Exception:
        return False

    def wrap(name: str) -> Any:
        anki_handler = getattr(web, name)

        def handler(evt: Any) -> None:
            if evt is not None and is_in_page_drag(evt):
                getattr(QWebEngineView, name)(web, evt)
                return
            anki_handler(evt)

        handler.__name__ = name
        return handler

    try:
        wrapped = {name: wrap(name) for name in _EVENTS}
        for name, fn in wrapped.items():
            setattr(web, name, fn)
        setattr(web, _MARK, True)
    except Exception:
        return False
    return True
