"""Anki Design — the Browser's rendered-card preview pane.

The stock Browser shows you a *note editor* and hides the actual card
behind Ctrl+Shift+P, in a separate floating window. That's backwards:
when you're browsing you're looking for a card, and a card is a rendered
thing — cloze holes, note-type CSS, images, tables. So the Browse tab's
right-hand pane leads with the card as the reviewer would draw it, and
the fields editor is a mode you switch into (the pencil in the pane's
header — see `browse_embed`), not a permanent half of the pane.

The whole card, both sides
--------------------------
One view, front *and* back. Most templates end `{{FrontSide}}<hr
id=answer>{{Back}}`, so the answer render already contains the question
and is the whole card; that is exactly what Anki's own previewer means by
"show both sides". For the templates that don't replay the front, we
stack them ourselves with the same `<hr id=answer>` divider the reviewer
uses, so "the full card" means the same thing for every note type.

There is consequently no Question/Answer switch and no click-to-flip:
with both sides on screen there is nothing to flip to. Text selection and
the right-click Google lookup are unaffected — they were competing with
the flip gesture before.

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

import html as _html
import json
import time
from typing import Any, Dict, List, Optional

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
        return None

    # -- state -----------------------------------------------------------
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

        if card.id != self._last_card_id:
            self._last_card_id = card.id
            self._last_render_key = None

        note = None
        try:
            note = card.note()
            note.load()
            key = (card.id, note.mod, card.mod)
        except Exception:
            key = (card.id, time.time())
        if key == self._last_render_key and not force:
            return

        try:
            text = self._whole_card_html(card)
        except Exception:
            self._render_placeholder(_ERR_TEXT)
            return

        try:
            text = self.mw.prepare_card_text_for_display(text)
        except Exception:
            pass
        try:
            # "previewAnswer" and not a kind of our own: an add-on filtering
            # card text has no reason to learn about this pane, and the
            # answer side is what it is being handed.
            text = gui_hooks.card_will_show(text, card, "previewAnswer")
        except Exception:
            pass

        try:
            from aqt.theme import theme_manager

            bodyclass = theme_manager.body_classes_for_card_ord(card.ord)
        except Exception:
            bodyclass = ""

        try:
            js = f"_showAnswer({json.dumps(text)}, '{bodyclass}');"
            js += (
                "window.__baPvTags && window.__baPvTags("
                + json.dumps(self._tags(note)) + ");"
            )
            self.web.eval(js)
            self._last_render_key = key
        except Exception:
            pass

    def _whole_card_html(self, card: Any) -> str:
        """Front and back in one view.

        `answer_text` usually opens with the rendered question, because
        `{{FrontSide}}` is how nearly every back template starts — that is
        the whole card already, and it's what Anki's previewer shows for
        "both sides". A template that doesn't replay the front would
        otherwise render back-only here, which is not "the full card", so
        those get stacked with the reviewer's own `<hr id=answer>` divider.

        Built from `render_output` rather than `card.question()` /
        `card.answer()` because those each prepend the note type's
        `<style>` block, and concatenating them would inject the same
        stylesheet twice.
        """
        out = card.render_output(reload=True)
        question = (out.question_text or "").strip()
        answer = out.answer_text or ""
        if question and question not in answer:
            body = f'{question}<hr id="answer">{answer}'
        else:
            body = answer
        return f"<style>{out.css}</style>{body}"

    def _tags(self, note: Any) -> List[str]:
        """The note's tags, for the strip under the card.

        Read-only here on purpose: the pane's job in this mode is to show
        you the card, and a tag you can accidentally edit while skimming is
        a worse deal than one you have to press the pencil to change.
        """
        if note is None:
            return []
        try:
            return [str(t) for t in (note.tags or []) if str(t).strip()]
        except Exception:
            return []

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
            f'<div class="ba-pv-empty-title">{_html.escape(message)}</div>'
            + (f'<div class="ba-pv-empty-sub">{_html.escape(sub)}</div>' if sub else "")
            + "</div>"
        )
        try:
            self.web.eval(
                f"_showQuestion({json.dumps(html)}, '', 'ba-pv-placeholder');"
                "window.__baPvTags && window.__baPvTags([]);"
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
