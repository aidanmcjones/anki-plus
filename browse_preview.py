"""Anki Design — the Browser's rendered-card preview pane.

The stock Browser shows you a *note editor* and hides the actual card
behind Ctrl+Shift+P, in a separate floating window. That's backwards:
when you're browsing you're looking for a card, and a card is a rendered
thing — cloze holes, note-type CSS, images, tables. So the Browse tab's
right-hand pane becomes a vertical split: the card as the reviewer would
draw it on top, the fields underneath.

How the rendering matches study exactly
---------------------------------------
We use the same pipeline Anki's own `Previewer` uses, which is the same
one the `Reviewer` uses:

  * `AnkiWebView(kind=PREVIEWER)` loaded with `mw.reviewer.revHtml()` and
    Anki's `css/reviewer.css` + mathjax + `js/reviewer.js`;
  * `card.question(reload=True)` / `card.answer()` for the text, so the
    note type's templates and CSS are applied by the backend exactly as
    in review;
  * `mw.prepare_card_text_for_display()` (media URLs, TTS) and the
    `card_will_show` hook, so add-ons that filter card text still run;
  * `theme_manager.body_classes_for_card_ord()` so `card1`/`card2` and
    night mode land on `<body>` the same way.

Because the webview declares *this object* as its stdHtml context,
`on_webview_will_set_content` in `__init__.py` recognises it and layers
Anki Design's own `theme.css` + `reviewer.css` on top — which is what
makes the pane read as "the card, as you will see it while studying"
rather than "the card, as stock Anki draws it".

`av_player` is deliberately NOT driven here: a browse pane that starts
playing audio every time you arrow through rows would be hostile. Media
images and video still render; only autoplay is suppressed.
"""

from __future__ import annotations

import json
import time
from typing import Any, Dict, Optional

from aqt import gui_hooks, mw
from aqt.qt import (
    QVBoxLayout,
    QWidget,
)


ADDON = __name__.split(".")[0]

# Re-render debounce. Arrowing down a long list fires a row change per
# keypress; rendering every one of them would peg a WebEngine process.
RENDER_DEBOUNCE_MS = 140


def _config() -> Dict[str, Any]:
    try:
        return mw.addonManager.getConfig(ADDON) or {}
    except Exception:
        return {}


def enabled() -> bool:
    return bool(_config().get("browse_render_preview", True))


class PreviewPane(QWidget):
    """A card renderer that lives inside the Browse tab.

    Deliberately not a subclass of Anki's `Previewer`: that class is a
    `QDialog` whose whole lifecycle (open/restoreGeom/finished/close) is
    built around being a window. We reuse its *rendering*, not its shell.
    """

    def __init__(self, browser: Any, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("ba-browse-preview")
        self.browser = browser
        self.mw = browser.mw
        # "answer" by default: the whole card is what you came to look at.
        # Clicking the card flips to the question side.
        self._state = "answer"
        self._last_render_key: Any = None
        self._last_card_id = 0
        self._timer: Any = None
        self._dead = False

        from aqt.webview import AnkiWebView, AnkiWebViewKind

        self.web: Any = AnkiWebView(kind=AnkiWebViewKind.PREVIEWER, parent=self)
        self.web.setObjectName("ba-browse-preview-web")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self.web, 1)

        self._setup_web()

    # -- web -------------------------------------------------------------
    def _setup_web(self) -> None:
        try:
            self.web.stdHtml(
                self.mw.reviewer.revHtml(),
                css=["css/reviewer.css"],
                js=[
                    "js/mathjax.js",
                    "js/vendor/mathjax/tex-chtml-full.js",
                    "js/reviewer.js",
                ],
                context=self,
            )
            # No gesture-free autoplay in a browse pane, ever.
            self.web.setPlaybackRequiresGesture(True)
            self.web.set_bridge_command(self._on_bridge_cmd, self)
        except Exception:
            pass

    def _on_bridge_cmd(self, cmd: str) -> Any:
        if cmd == "ba:pv-toggle":
            self.toggle_side()
        elif cmd.startswith("ba:pv-side:"):
            side = cmd.split(":")[-1]
            if side in ("question", "answer") and side != self._state:
                self._state = side
                self._last_render_key = None
                self.render(force=True)
        return None

    # -- state -----------------------------------------------------------
    def toggle_side(self) -> None:
        self._state = "question" if self._state == "answer" else "answer"
        self._last_render_key = None
        self.render(force=True)

    def card(self) -> Any:
        """The single selected card, mirroring `BrowserPreviewer.card()`.
        A multi-row selection has no single card to draw."""
        try:
            if self.browser.singleCard:
                return self.browser.card
        except Exception:
            pass
        return None

    # -- rendering -------------------------------------------------------
    def schedule(self) -> None:
        """Debounced re-render, called from `browser_did_change_row`."""
        if self._dead:
            return
        self._cancel_timer()
        try:
            self._timer = self.mw.progress.timer(
                RENDER_DEBOUNCE_MS, self.render, False, parent=self
            )
        except Exception:
            self.render()

    def _cancel_timer(self) -> None:
        if self._timer is not None:
            try:
                self._timer.stop()
            except Exception:
                pass
            self._timer = None

    def render(self, force: bool = False) -> None:
        self._cancel_timer()
        if self._dead:
            return
        card = self.card()
        if card is None:
            self._render_placeholder()
            return

        # Flip back to the question side when the card itself changes, so
        # stepping through rows doesn't leave you looking at answers you
        # never asked to see... except we default to "answer" because the
        # point of this pane is *seeing the card*. So: keep whichever side
        # the user last chose, and only reset the render cache.
        if card.id != self._last_card_id:
            self._last_card_id = card.id
            self._last_render_key = None

        try:
            note = card.note()
            note.load()
            key = (self._state, card.id, note.mod, card.mod)
        except Exception:
            key = (self._state, card.id, time.time())
        if key == self._last_render_key and not force:
            return

        try:
            question = card.question(reload=True)
            answer = card.answer()
        except Exception:
            self._render_placeholder(_ERR_TEXT)
            return

        text = answer if self._state == "answer" else question
        try:
            text = self.mw.prepare_card_text_for_display(text)
        except Exception:
            pass
        try:
            kind = "preview" + self._state.capitalize()
            text = gui_hooks.card_will_show(text, card, kind)
        except Exception:
            pass

        try:
            from aqt.theme import theme_manager

            bodyclass = theme_manager.body_classes_for_card_ord(card.ord)
        except Exception:
            bodyclass = ""

        try:
            if self._state == "question":
                escaped_answer = self.mw.col.media.escape_media_filenames(answer)
                js = (
                    f"_showQuestion({json.dumps(text)}, "
                    f"{json.dumps(escaped_answer)}, '{bodyclass}');"
                )
            else:
                js = f"_showAnswer({json.dumps(text)}, '{bodyclass}');"
            js += f"window.__baPvSide && window.__baPvSide('{self._state}');"
            self.web.eval(js)
            self._last_render_key = key
        except Exception:
            pass

    def _render_placeholder(self, message: str = "") -> None:
        """Nothing selected, or several rows selected. Shown in the design
        language rather than Anki's bare "Please select 1 card"."""
        self._last_render_key = None
        self._last_card_id = 0
        if not message:
            try:
                count = self.browser.table.len_selection()
            except Exception:
                count = 0
            if count > 1:
                message = f"{count} cards selected"
                sub = "Select a single row to see it rendered."
            else:
                message = "No card selected"
                sub = "Pick a row to see the card as you'll study it."
        else:
            sub = ""
        html = (
            '<div class="ba-pv-empty">'
            f'<div class="ba-pv-empty-title">{message}</div>'
            + (f'<div class="ba-pv-empty-sub">{sub}</div>' if sub else "")
            + "</div>"
        )
        try:
            self.web.eval(
                f"_showQuestion({json.dumps(html)}, '', 'ba-pv-placeholder');"
                "window.__baPvSide && window.__baPvSide('none');"
            )
        except Exception:
            pass

    # -- teardown --------------------------------------------------------
    def cleanup(self) -> None:
        """Mirror `Previewer._on_close`. Skipping this is what produced the
        theme-change crash upstream fixed in FindDuplicates: a webview that
        outlives its window still answers `theme_did_change` and touches a
        deleted page."""
        if self._dead:
            return
        self._dead = True
        self._cancel_timer()
        web = self.web
        self.web = None  # type: ignore[assignment]
        if web is not None:
            try:
                web.cleanup()
            except Exception:
                pass
            try:
                web.setParent(None)
                web.deleteLater()
            except Exception:
                pass


_ERR_TEXT = "This card could not be rendered"
