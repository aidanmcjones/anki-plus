"""Anki Design — embed Anki's Preferences (with our Anki Design tab
pre-selected) inside the main window as a "tab".

Mirrors `stats_embed.py`. The "Settings" sidebar item already opens
Anki's `Preferences` QDialog with our `AnkiDesignSettingsPage` injected
as a tab. We want it to live inline next to the sidebar instead of
opening as a separate window.

Preferences is a QDialog, so we reparent the whole dialog into our
overlay as a `Qt.WindowType.Widget`. That carries everything along
automatically: the `tabWidget` (Basic, Scheduling, Network, Backups,
the Anki Design tab, plus any other add-on tabs), the bottom
`buttonBox` (Help / Close — which `install_into_preferences()` hides
while our tab is current), and any widgets attached by add-ons via
the `setupOptions` hook.

Close path is async. Native `Preferences.reject()` chains into
`accept_with_callback()` which kicks off a background `set_preferences`
op; the completion callback reads form widgets again
(`update_profile`, `update_global`) and finally calls `done(0)` +
`markClosed("Preferences")`. We hand the overlay teardown to that
callback so the widgets stay alive until the save completes. If the
collection is gone, we skip the save and tear down immediately.
"""

from __future__ import annotations

from typing import Any

from aqt import mw
from aqt.qt import (
    QApplication,
    QColor,
    QEvent,
    QFrame,
    QKeySequence,
    QObject,
    QPalette,
    QShortcut,
    Qt,
    QTimer,
    QVBoxLayout,
)


SIDEBAR_W = 264  # px fallback — see _sidebar_w() for the live value


def _sidebar_w() -> int:
    """Current left-rail width — 264px expanded, 64px collapsed (Item 2).
    See addcard_embed._sidebar_w() for the full explanation; every
    *_embed.py module goes through the same addcard.sidebar_w()."""
    try:
        from . import addcard as _addcard
        return _addcard.sidebar_w()
    except Exception:
        return SIDEBAR_W


class _EmbedFilter(QObject):
    """Re-positions the embed overlay whenever the main window is resized."""

    def __init__(self, overlay: QFrame) -> None:
        super().__init__(overlay)
        self._overlay = overlay

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:
        if event.type() == QEvent.Type.Resize:
            try:
                cw = mw.form.centralwidget
                w = _sidebar_w()
                self._overlay.setGeometry(
                    w,
                    0,
                    cw.width() - w,
                    cw.height(),
                )
            except Exception:
                pass
        return False


_state: dict = {
    "prefs": None,
    "overlay": None,
    "filter": None,
    "closing": False,
}


def reflow() -> None:
    """Reposition the open overlay/curtain to the current sidebar width."""
    try:
        cw = mw.form.centralwidget
        w = _sidebar_w()
        for key in ("overlay", "curtain"):
            widget = _state.get(key)
            if widget is not None:
                widget.setGeometry(w, 0, cw.width() - w, cw.height())
    except Exception:
        pass


def drop_curtain() -> None:
    """Tear down the anti-flash curtain. Safe to call multiple times."""
    c = _state.pop("curtain", None)
    if c is not None:
        try:
            c.deleteLater()
        except Exception:
            pass


def _teardown_now() -> None:
    """Synchronous overlay teardown. Called as the callback after
    `accept_with_callback` finishes saving, or directly when the
    collection isn't available."""
    overlay = _state.get("overlay")
    flt = _state.get("filter")

    _state["prefs"] = None
    _state["overlay"] = None
    _state["filter"] = None
    _state["closing"] = False
    cw_palette = _state.pop("cw_palette", None)
    curtain = _state.pop("curtain", None)
    if curtain is not None:
        try:
            curtain.deleteLater()
        except Exception:
            pass

    if overlay is not None:
        try:
            if flt is not None:
                mw.form.centralwidget.removeEventFilter(flt)
        except Exception:
            pass
        try:
            overlay.deleteLater()
        except Exception:
            pass
    if cw_palette is not None:
        try:
            mw.form.centralwidget.setPalette(cw_palette)
        except Exception:
            pass

    try:
        w = getattr(mw, "web", None)
        if w is not None:
            w.eval("window.__baSetActive && window.__baSetActive('decks');")
    except Exception:
        pass

    # This overlay occluded the video backdrop for as long as it was up
    # (see `_push_video_occlusion` in `__init__.py`); now that its state
    # above has already been cleared to None, `_any_embed_open()` no
    # longer sees it, so this correctly resumes playback — unless some
    # OTHER embed is still open, in which case it's a no-op push of the
    # same "still occluded" state. Runs on every path through here (Close
    # button, Esc, and Done via `_close_enclosing_dialog`), so nothing
    # downstream has to remember to do this as a side effect of some
    # unrelated navigation.
    try:
        from . import _push_video_occlusion
        _push_video_occlusion()
    except Exception:
        pass


def close_inline() -> None:
    """Kick off save + teardown of the embedded Preferences.

    Async: returns immediately, teardown happens inside the
    `accept_with_callback` completion callback so the form widgets
    stay alive while `update_profile` / `update_global` read them."""
    if _state.get("closing"):
        return
    sd = _state.get("prefs")
    if sd is None and _state.get("overlay") is None:
        return

    _state["closing"] = True

    if sd is None or not getattr(mw, "col", None):
        # No dialog or no collection — skip the save chain and tear
        # down immediately.
        _teardown_now()
        return

    try:
        sd.accept_with_callback(_teardown_now)
    except Exception:
        _teardown_now()


def open_inline(parent_mw: Any = None) -> None:
    """Open Preferences embedded in the main window's content area,
    with the Anki Design tab pre-selected.

    Falls back to the standalone window if anything goes wrong."""
    parent_mw = parent_mw or mw
    if _state.get("overlay") is not None:
        try:
            _state["overlay"].raise_()
        except Exception:
            pass
        return
    if _state.get("curtain") is not None:
        return

    # --- Curtain ----------------------------------------------------
    from . import addcard as _addcard
    palette, _ = _addcard._resolve_palette()
    paper_qc = QColor(palette["paper"])
    cw = parent_mw.form.centralwidget

    curtain = QFrame(cw)
    curtain.setObjectName("ba-settings-curtain")
    curtain.setAutoFillBackground(True)
    _cu_pal = curtain.palette()
    _cu_pal.setColor(QPalette.ColorRole.Window, paper_qc)
    curtain.setPalette(_cu_pal)
    curtain.setStyleSheet(
        "QFrame#ba-settings-curtain { background: " + palette["paper"] + "; }"
    )
    curtain.setGeometry(_sidebar_w(), 0, cw.width() - _sidebar_w(), cw.height())
    curtain.show()
    curtain.raise_()
    _state["curtain"] = curtain
    try:
        curtain.repaint()
        QApplication.processEvents()
    except Exception:
        pass

    # Make sure our Anki Design tab has been installed BEFORE we
    # construct Preferences — the patched `setupOptions` injects the
    # tab during `__init__`.
    try:
        from .settings import install_into_preferences, _select_anki_design_tab
        install_into_preferences()
    except Exception:
        _select_anki_design_tab = None  # type: ignore[assignment]

    try:
        from aqt.preferences import Preferences
        # Preferences.__init__ ends with self.show(). Subclass to no-op
        # so the standalone window never flashes.
        class _EmbeddedPreferences(Preferences):  # type: ignore[misc, valid-type]
            def show(self) -> None:  # noqa: D401 — Qt override
                pass

            def activateWindow(self) -> None:  # noqa: D401 — Qt override
                pass

        sd = _EmbeddedPreferences(parent_mw)
    except Exception:
        try:
            curtain.deleteLater()
        except Exception:
            pass
        _state["curtain"] = None
        try:
            from .settings import open_settings
            open_settings(parent_mw)
        except Exception:
            pass
        return

    try:
        # Register with the dialog manager so other code paths that do
        # `aqt.dialogs.open("Preferences", ...)` find our instance
        # instead of opening a second window. `accept_with_callback`
        # (run on close) calls `markClosed("Preferences")` which
        # clears the registration.
        try:
            import aqt as _aqt
            _aqt.dialogs._dialogs["Preferences"][1] = sd  # type: ignore[index]
        except Exception:
            pass

        # Pre-select the Anki Design tab so the embed opens on our page.
        if _select_anki_design_tab is not None:
            try:
                _select_anki_design_tab(sd)
            except Exception:
                pass

        overlay = QFrame(parent_mw.form.centralwidget)
        overlay.setObjectName("ba-settings-embed")
        overlay.setAutoFillBackground(True)
        _ov_pal = overlay.palette()
        _ov_pal.setColor(QPalette.ColorRole.Window, paper_qc)
        overlay.setPalette(_ov_pal)
        overlay.setStyleSheet(
            "QFrame#ba-settings-embed { background: " + palette["paper"] + "; "
            "border-left: 1px solid " + palette["line"] + "; }"
        )

        v = QVBoxLayout(overlay)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)

        # Reparent the whole dialog. tabWidget, buttonBox, our injected
        # Anki Design tab, and any add-on-injected tabs all come along
        # because they're parented to the dialog.
        sd.setParent(overlay)
        sd.setWindowFlags(Qt.WindowType.Widget)
        sd.setVisible(True)
        v.addWidget(sd, 1)

        # Hijack the Close button (buttonBox.rejected → Dialog.reject):
        # close_inline runs the same save chain (via
        # accept_with_callback) but also tears down our overlay in the
        # completion callback.
        try:
            bb = sd.form.buttonBox
            try:
                bb.rejected.disconnect()
            except Exception:
                pass
            bb.rejected.connect(close_inline)
        except Exception:
            pass

        # Tint mw.centralwidget paper so any first-frame Qt gap is invisible.
        try:
            _state["cw_palette"] = QPalette(cw.palette())
            _cw_pal = cw.palette()
            _cw_pal.setColor(QPalette.ColorRole.Window, paper_qc)
            cw.setPalette(_cw_pal)
        except Exception:
            pass

        _ow = _sidebar_w()
        overlay.setGeometry(
            _ow, 0, cw.width() - _ow, cw.height()
        )
        overlay.show()
        overlay.raise_()
        try:
            curtain.raise_()
        except Exception:
            pass

        # Preferences is all native Qt widgets — first paint is
        # essentially immediate. A short timer is plenty.
        QTimer.singleShot(200, drop_curtain)

        flt = _EmbedFilter(overlay)
        cw.installEventFilter(flt)

        # Esc closes the embed. Preferences's own Esc shortcut (added
        # via add_close_shortcut(self)) might still be active on the
        # dialog widget, but it would call reject() which goes through
        # the same chain — having both is redundant but harmless.
        try:
            esc = QShortcut(QKeySequence("Escape"), overlay)
            esc.setAutoRepeat(False)
            esc.setContext(Qt.ShortcutContext.WindowShortcut)
            esc.activated.connect(close_inline)
        except Exception:
            pass

        _state["prefs"] = sd
        _state["overlay"] = overlay
        _state["filter"] = flt
        _state["closing"] = False

        # Highlight "Settings" in the sidebar.
        try:
            w = getattr(parent_mw, "web", None)
            if w is not None:
                w.eval(
                    "window.__baSetActive && window.__baSetActive('settings');"
                )
        except Exception:
            pass
    except Exception as e:
        import traceback
        print(
            f"[anki-design.settings_embed] failed: {e}\n"
            f"{traceback.format_exc()}",
            flush=True,
        )
        try:
            _teardown_now()
        except Exception:
            pass
        try:
            sd.show()
        except Exception:
            pass
