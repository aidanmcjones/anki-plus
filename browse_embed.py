"""Anki Design — embed the card Browser inside the main window as a "tab".

Mirrors `addcard_embed.py` exactly. The user wants Browse to behave like a
tab next to the sidebar instead of opening as a separate QMainWindow:

  - The sidebar's `ba:browse` pycmd calls `open_inline(mw)` (see
    `__init__.py`) instead of `mw.onBrowse()`.
  - `open_inline` constructs a Browser instance (we no-op `.show()` on
    a subclass so its standalone window never flashes on screen).
  - We grab its `centralWidget` and reparent it onto an overlay QFrame
    placed on top of `mw.form.centralwidget`, offset by the sidebar width
    so the sidebar rendered inside `mw.web` stays visible to the left.
  - The Browser window itself stays hidden (its widgets remain alive, so
    all menubar actions, shortcuts, and add-on hooks continue to work).
  - The overlay resizes with the main window via an installed event
    filter.
  - We register our instance with `aqt.dialogs` so any other code path
    that does `aqt.dialogs.open("Browser", ...)` finds our existing
    Browser and calls `reopen()` on it instead of opening a second one.

This is a first cut: the chrome is whatever Anki's stock browser uses.
Visual redesign is intentionally deferred — the user wants the layout
moved in as-is first.
"""

from __future__ import annotations

from typing import Any

from aqt import gui_hooks, mw
from aqt.qt import (
    QApplication,
    QColor,
    QCursor,
    QDockWidget,
    QEvent,
    QFrame,
    QHBoxLayout,
    QKeySequence,
    QLabel,
    QObject,
    QPalette,
    QPushButton,
    QShortcut,
    QSplitter,
    Qt,
    QTimer,
    QVBoxLayout,
    QWidget,
)


SIDEBAR_W = 264  # px — matches --rf-side-w in web/theme.css; same as addcard_embed

# Browser sidebar (the tag/decks tree inside the embedded Browser). The
# Anki default of ~240px gets visibly squeezed on small windows because
# QSplitter scales setSizes() proportionally — pin a comfortable preferred
# width and a hard minimum so it can't shrink past readability.
SIDEBAR_DOCK_PREF = 280
SIDEBAR_DOCK_MIN = 220
SIDEBAR_DOCK_MAX = 480  # past this the central pane (table + editor) starts to look empty
CENTRAL_MIN = 420  # search + card table + editor pane need this much

# Inner Browser splitter (search+table | editor). Anki's restoreSplitter
# call ("editor3") often hands back [all, 0] for a fresh profile, which
# leaves the editor pane invisible — and setChildrenCollapsible(False)
# doesn't save us because the editor container (verticalLayoutWidget) has
# no minimum width, so QSplitter happily shrinks it past zero. We give
# the editor side a real minimum and force a sensible initial split.
EDITOR_MIN = 320   # narrowest the field area stays legible
EDITOR_PREF = 460  # default share on first open
TABLE_MIN = 360    # card table + search bar need at least this much

# Editor side: one pane, two modes. Read (the rendered card, both sides,
# tags underneath) and Edit (Anki's fields editor), switched by the pencil
# in the pane's header. They used to be stacked in a vertical splitter,
# which gave each of them half a pane — enough for neither, and a card cut
# off mid-answer above a field list cut off mid-field.
PREVIEW_MIN = 180
FIELDS_MIN = 170


def _clamp_splitter(splitter: "QSplitter", central: Any) -> None:
    """Re-set sizes after a drag so the docks stay within [MIN, MAX] AND
    the handles sit flush against their widgets. Without this the handle
    can drag past a dock's maxWidth, leaving a dead empty band of overlay
    background between the dock and the centre pane."""
    try:
        count = splitter.count()
        sizes = list(splitter.sizes())
        avail = splitter.width() - splitter.handleWidth() * max(0, count - 1)
        sidebar_total = 0
        for i in range(count):
            w = splitter.widget(i)
            if w is central:
                continue
            sizes[i] = max(SIDEBAR_DOCK_MIN, min(SIDEBAR_DOCK_MAX, sizes[i]))
            sidebar_total += sizes[i]
        for i in range(count):
            if splitter.widget(i) is central:
                sizes[i] = max(CENTRAL_MIN, avail - sidebar_total)
        # Only re-set if anything actually changed (avoid signal loop).
        if sizes != list(splitter.sizes()):
            splitter.blockSignals(True)
            try:
                splitter.setSizes(sizes)
            finally:
                splitter.blockSignals(False)
    except Exception:
        pass


def _apply_inner_split(inner: "QSplitter") -> None:
    """Force a sensible [table | editor] split on the Browser's inner
    splitter. Deferred via QTimer so inner.width() reflects the laid-out
    overlay, not the construction-time 640px default. Without this the
    editor pane shows up at 0px wide and looks 'missing'."""

    def _set() -> None:
        try:
            avail = inner.width() - inner.handleWidth()
            if avail <= 0:
                return
            editor_w = max(EDITOR_MIN, min(EDITOR_PREF, avail // 2))
            table_w = max(TABLE_MIN, avail - editor_w)
            inner.setSizes([table_w, editor_w])
        except Exception:
            pass

    QTimer.singleShot(0, _set)


def _apply_initial_splitter_sizes(splitter: "QSplitter", central: Any) -> None:
    """Set initial splitter sizes from the overlay's actual width so a
    narrow window doesn't end up with a 100px sidebar after QSplitter
    proportionally scales setSizes(). Deferred via QTimer.singleShot(0)
    so the overlay has been laid out and width() is meaningful."""

    def _set() -> None:
        try:
            count = splitter.count()
            handles = max(0, count - 1) * splitter.handleWidth()
            avail = max(0, splitter.width() - handles)
            non_central = count - 1
            sidebars = min(SIDEBAR_DOCK_PREF, max(SIDEBAR_DOCK_MIN, (avail - CENTRAL_MIN) // max(1, non_central)))
            central_w = max(CENTRAL_MIN, avail - sidebars * non_central)
            sizes = []
            for i in range(count):
                if splitter.widget(i) is central:
                    sizes.append(central_w)
                else:
                    sizes.append(sidebars)
            splitter.setSizes(sizes)
        except Exception:
            pass

    QTimer.singleShot(0, _set)


# Pencil (read mode) and its "done" counterpart (edit mode). Drawn rather
# than themed from a resource so they match the sidebar's stroke icons.
_PENCIL_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" '
    'width="16" height="16" fill="none" stroke="{c}" stroke-width="1.7" '
    'stroke-linecap="round" stroke-linejoin="round">'
    '<path d="M4 20h4l11-11-4-4L4 16v4z"/><path d="M14 6l4 4"/></svg>'
)
_DONE_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" '
    'width="16" height="16" fill="none" stroke="{c}" stroke-width="1.9" '
    'stroke-linecap="round" stroke-linejoin="round">'
    '<path d="M5 12.5l4.5 4.5L19 7"/></svg>'
)


def _svg_icon(markup: str, color: str) -> Any:
    """An SVG string as a QIcon, tinted to the current palette."""
    from aqt.qt import QIcon, QPixmap

    pix = QPixmap()
    pix.loadFromData(markup.format(c=color).encode("utf-8"), "SVG")
    return QIcon(pix)


def _install_preview(br: Any) -> None:
    """Turn the Browser's editor side into one pane with two modes.

    Anki's `verticalLayoutWidget` holds a QVBoxLayout whose only content is
    `horizontalLayout2` → `fieldsArea`. We lift `fieldsArea` into a holder,
    put it and the rendered-card pane into a QStackedWidget under a thin
    header, and hand that back to the layout. Going through the layout
    (rather than reparenting `verticalLayoutWidget` itself) keeps every
    Browser code path that pokes at `form.splitter.widget(1)` or
    `form.fieldsArea` working untouched — including the editor's own
    `loadNote`, which keeps running while the editor is the hidden page,
    so switching to Edit shows the current row immediately.
    """
    from . import browse_preview

    if not browse_preview.enabled():
        return

    from aqt.qt import QSize, QStackedWidget, QToolButton

    form = br.form
    vl = form.verticalLayout
    fields = form.fieldsArea
    parent = form.verticalLayoutWidget

    pane = browse_preview.PreviewPane(br, parent=parent)
    pane.setMinimumHeight(PREVIEW_MIN)

    holder = QWidget(parent)
    holder.setObjectName("ba-browse-fields-holder")
    hl = QVBoxLayout(holder)
    hl.setContentsMargins(0, 0, 0, 0)
    hl.setSpacing(0)
    hl.addWidget(fields, 1)
    holder.setMinimumHeight(FIELDS_MIN)

    stack = QStackedWidget(parent)
    stack.setObjectName("ba-browse-pane-stack")
    stack.addWidget(pane)     # 0 — read
    stack.addWidget(holder)   # 1 — edit

    # -- header ------------------------------------------------------
    header = QWidget(parent)
    header.setObjectName("ba-browse-pane-head")
    hrow = QHBoxLayout(header)
    hrow.setContentsMargins(16, 6, 12, 6)
    hrow.setSpacing(6)
    label = QLabel("Card")
    label.setObjectName("ba-browse-pane-title")
    hrow.addWidget(label)
    hrow.addStretch(1)
    edit_btn = QToolButton(header)
    edit_btn.setObjectName("ba-browse-pane-edit")
    edit_btn.setCheckable(True)
    edit_btn.setCursor(Qt.CursorShape.PointingHandCursor)
    edit_btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
    edit_btn.setIconSize(QSize(16, 16))
    hrow.addWidget(edit_btn)

    def _paint_header() -> None:
        """Icon + label for whichever mode we're in, in palette colours."""
        try:
            from . import addcard as _addcard

            p, _dark = _addcard._resolve_palette()
            editing = stack.currentIndex() == 1
            edit_btn.setIcon(
                _svg_icon(_DONE_SVG if editing else _PENCIL_SVG, p["ink_dim"])
            )
            edit_btn.setText("Done" if editing else "Edit")
            edit_btn.setToolTip(
                "Back to the card (⌘E)" if editing
                else "Edit this note's fields (⌘E)"
            )
            label.setText("Editing" if editing else "Card")
        except Exception:
            pass

    def _set_mode(editing: bool) -> None:
        stack.setCurrentIndex(1 if editing else 0)
        if edit_btn.isChecked() != editing:
            edit_btn.setChecked(editing)
        _paint_header()
        if editing:
            # The editor kept loading notes while it was hidden, so there
            # is nothing to reload — just put the caret somewhere useful.
            try:
                br.editor.web.setFocus()
            except Exception:
                pass
        else:
            # Anything typed in the fields is written on focus loss; make
            # sure the card we re-render is the saved one.
            try:
                br.editor.call_after_note_saved(
                    lambda: pane.render(force=True), keepFocus=False
                )
            except Exception:
                pane.render(force=True)

    edit_btn.toggled.connect(_set_mode)

    def _on_page_changed(index: int) -> None:
        # QStackedLayout switches to a page that gets shown directly, and
        # Anki's editor calls `widget.show()` on every load_note. Rather
        # than fight that, follow it: a header that says "Card" over the
        # fields editor is worse than either mode.
        editing = index == 1
        if edit_btn.isChecked() != editing:
            edit_btn.blockSignals(True)
            edit_btn.setChecked(editing)
            edit_btn.blockSignals(False)
        _paint_header()

    stack.currentChanged.connect(_on_page_changed)

    wrap = QWidget(parent)
    wrap.setObjectName("ba-browse-pane")
    wl = QVBoxLayout(wrap)
    wl.setContentsMargins(0, 0, 0, 0)
    wl.setSpacing(0)
    wl.addWidget(header)
    wl.addWidget(stack, 1)

    # Drain whatever the .ui put in the layout (the now-empty
    # horizontalLayout2) and install ours in its place.
    while vl.count():
        item = vl.takeAt(0)
        if item is None:
            break
        child = item.layout()
        if child is not None:
            child.setParent(None)
    vl.addWidget(wrap, 1)

    def _on_row(browser: Any, _pane=pane, _br=br) -> None:
        if browser is not _br:
            return
        try:
            # Both modes stay live: the editor loads the row itself, and
            # the card re-renders behind it, so switching mode never shows
            # you the previous row for a frame.
            _pane.schedule()
        except Exception:
            pass

    gui_hooks.browser_did_change_row.append(_on_row)

    # ⌘E / Ctrl+E from anywhere in the Browser. The editor's own webview
    # swallows most keys, so the shortcut is on the whole central widget.
    try:
        sc = QShortcut(QKeySequence("Ctrl+E"), br.form.centralwidget)
        sc.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        sc.activated.connect(lambda: edit_btn.setChecked(not edit_btn.isChecked()))
        _state["edit_shortcut"] = sc
    except Exception:
        pass

    _state["preview"] = pane
    _state["preview_hook"] = _on_row
    _state["pane_stack"] = stack
    _state["pane_head"] = header
    _state["pane_edit_btn"] = edit_btn
    _state["pane_paint"] = _paint_header
    _paint_header()
    # First paint: the webview needs a beat to finish loading reviewer.js
    # before `_showAnswer` means anything.
    QTimer.singleShot(350, pane.render)


def _teardown_preview() -> None:
    hook = _state.pop("preview_hook", None)
    if hook is not None:
        try:
            gui_hooks.browser_did_change_row.remove(hook)
        except Exception:
            pass
    sc = _state.pop("edit_shortcut", None)
    if sc is not None:
        try:
            sc.setParent(None)
            sc.deleteLater()
        except Exception:
            pass
    for key in ("pane_stack", "pane_head", "pane_edit_btn", "pane_paint"):
        _state.pop(key, None)
    pane = _state.pop("preview", None)
    if pane is not None:
        try:
            pane.cleanup()
        except Exception:
            pass


def _palette_styles() -> str:
    """QSS for the overlay frame. The Browser's own internals carry their
    Qt-native styles — we just paint the wrapper paper-colored so the gap
    between sidebar and embed reads as one continuous page."""
    from . import addcard as _addcard
    palette, _ = _addcard._resolve_palette()
    return (
        "QFrame#ba-browse-embed { background: " + palette["paper"] + "; "
        "border-left: 1px solid " + palette["line"] + "; }"
    )


def refresh_palette() -> None:
    """Re-tint the embed's own chrome after a theme flip.

    The overlay frame, the outer splitter's handles and the main window's
    central widget are all painted from the palette captured when the
    embed opened. Anki's `theme_did_change` repaints its own widgets but
    knows nothing about ours, so without this a light/dark flip leaves a
    dark seam between the sidebar rail and the table."""
    overlay = _state.get("overlay")
    if overlay is None:
        return
    from . import addcard as _addcard

    palette, _ = _addcard._resolve_palette()
    paper = QColor(palette["paper"])
    try:
        pal = overlay.palette()
        pal.setColor(QPalette.ColorRole.Window, paper)
        overlay.setPalette(pal)
        overlay.setStyleSheet(_palette_styles())
    except Exception:
        pass
    sp = _state.get("splitter")
    if sp is not None:
        try:
            sp.setStyleSheet(
                "QSplitter::handle { background: " + palette["line"] + "; }"
                "QSplitter::handle:hover { background: " + palette["line2"] + "; }"
            )
        except Exception:
            pass
    try:
        cw = mw.form.centralwidget
        cw_pal = cw.palette()
        cw_pal.setColor(QPalette.ColorRole.Window, paper)
        cw.setPalette(cw_pal)
    except Exception:
        pass


class _EmbedFilter(QObject):
    """Re-positions the embed overlay whenever the main window is resized.
    Also re-applies the sidebar clamp so QSplitter's proportional resize
    can't drag the sidebar past SIDEBAR_DOCK_MAX into the empty-gap state."""

    def __init__(self, overlay: QFrame) -> None:
        super().__init__(overlay)
        self._overlay = overlay

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:
        if event.type() == QEvent.Type.Resize:
            try:
                cw = mw.form.centralwidget
                self._overlay.setGeometry(
                    SIDEBAR_W,
                    0,
                    cw.width() - SIDEBAR_W,
                    cw.height(),
                )
            except Exception:
                pass
            sp = _state.get("splitter")
            c = _state.get("central")
            if sp is not None and c is not None:
                _clamp_splitter(sp, c)
        return False


_state: dict = {"browser": None, "overlay": None, "filter": None}


def _on_op_executed_embedded(changes: Any, handler: object | None) -> None:
    """Force the table repaint that stock Browser skips for an embedded
    instance.

    Root cause (found investigating a report that Browse's card table
    painted empty right after a sync): stock `Browser.on_operation_did_execute`
    -> `Table.op_executed` only calls `redraw_cells()` (which emits
    `dataChanged` and makes the QTableView actually repaint) when
    `current_window() is self` — i.e. when Qt considers this Browser's
    *own* top-level window the one holding focus. `current_window()` is
    `QApplication.focusWidget().window()` (qt/aqt/utils.py). Our embedded
    Browser is a permanently-hidden QMainWindow — `show()`/`activateWindow()`
    are no-op'd and its real content lives reparented inside `mw`'s window
    — so it can never be `current_window()`. Its `focused` is always
    `False`, so `mark_cache_stale()` still runs (the row cache is
    invalidated) but `redraw_cells()` never does: the table can end up
    showing stale/blank cells until something else forces an unconditional
    full model rebuild, e.g. the Cards/Notes toggle (`Table.toggle_state`).

    This fires on every `operation_did_execute`, which notably includes
    the synthesized op `mw.reset()` emits — and this add-on's own silent
    sync wrapper (`__init__.py: on_collection_sync_finished`) calls
    `mw.reset()` unconditionally, on a *failed* sync too. So: sync
    (even one that errors out with no account configured) -> mw.reset()
    -> operation_did_execute -> embedded Table.op_executed(focused=False)
    -> cache invalidated, view never told to repaint -> empty-looking
    table until the user forces a rebuild by hand.

    Narrow, event-driven fix: whenever our embed is open, do the repaint
    step ourselves. Stock's own listener (registered in Browser.setupHooks
    at construction time) already ran and marked the cache stale by the
    time this fires, since hooks run in registration order and we only
    register after the embed is fully open — so redraw_cells() here is
    sufficient, not a duplicate of the cache invalidation.

    The Browser's own sidebar tree (`Sidebar.op_executed`, deck/tag counts)
    has the identical `if focused: self.refresh_if_needed()` gate, so it's
    fixed here too, for free."""
    br = _state.get("browser")
    if br is None:
        return
    try:
        br.table.redraw_cells()
    except Exception:
        pass
    try:
        br.sidebar.refresh_if_needed()
    except Exception:
        pass


def _on_add_note_embedded(note: Any) -> None:
    """Pull a just-added note into Browse's visible results.

    `_on_op_executed_embedded` (above) repaints existing rows/sidebar
    counts, but it can't make a brand-new row appear: `Table.op_executed`
    only calls `mark_cache_stale()` (invalidates cached field content for
    rows already in the result set) — the row ID list itself is a snapshot
    taken by the last `Table.search()` call, and nothing re-runs that
    search on note-add in stock Anki either (aqt/addcards.py never touches
    an open Browser). `Browser.search()` (self.table.search(self.
    _lastSearchTxt)) is the same call the search box itself makes, so this
    just re-issues the user's own last search — a normal, expected refresh,
    not a rebuild-from-scratch workaround."""
    br = _state.get("browser")
    if br is None:
        return
    try:
        br.search()
    except Exception:
        pass


def _open_add_in_browse(parent_mw: Any) -> None:
    """Add-card button inside the embedded Browse's search row.

    Reuses `addcard_embed.open_inline` wholesale — no parallel Add
    implementation. This is the only way to reach Add now: there is no
    standalone "Add" rail item any more (see `__init__.py`'s `_open_add`,
    which the "A" shortcut still calls — it lands here too, opening
    Browse first if it isn't already open). This opens the Add panel
    stacked on top of Browse's own overlay: both are QFrames positioned
    at the same geometry on `parent_mw.form.centralwidget`, so the Add
    panel visually covers the table without destroying Browse underneath.
    Closing Add (Esc / save-and-close) reveals Browse exactly as it was —
    same search, same selection — and `addcard_embed.close_inline`'s
    sidebar-tab restore is taught (see below) to land back on "browse"
    instead of "decks" when this embed is still open underneath.

    One thing stacking breaks that a straight reuse-call can't fix by
    itself: Browse's own Esc shortcut (closes Browse) and AddCards' Esc
    shortcut (closes Add) are both `Qt.ShortcutContext.WindowShortcut`,
    and both now live in the same top-level window (`parent_mw`) at once
    — two enabled QShortcuts on the same key in the same window are
    ambiguous to Qt and neither fires. Disable Browse's for as long as
    Add is stacked on top; a wrapper layered on top of addcard_embed's
    own `ac._close` (the same wrap-and-chain pattern addcard_embed.
    open_inline already uses internally) re-enables it once Add closes."""
    br_esc = _state.get("esc_shortcut")
    try:
        if br_esc is not None:
            br_esc.setEnabled(False)
    except Exception:
        pass
    try:
        from . import addcard_embed
        addcard_embed.open_inline(parent_mw)
        ac = addcard_embed._state.get("addcards")
        if ac is not None:
            orig_close = ac._close

            def _reenable_browse_esc(orig=orig_close, esc=br_esc) -> None:
                try:
                    orig()
                finally:
                    try:
                        if esc is not None:
                            esc.setEnabled(True)
                    except Exception:
                        pass

            ac._close = _reenable_browse_esc  # type: ignore[assignment]
    except Exception:
        try:
            if br_esc is not None:
                br_esc.setEnabled(True)
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


def close_inline() -> None:
    """Tear down the embedded Browser and restore the deck browser.

    No-op if there is no embed currently open — callers (state-change
    monkey-patches, sidebar `decks` pycmd) can call this cheaply on
    every navigation event."""
    overlay = _state.get("overlay")
    br = _state.get("browser")
    flt = _state.get("filter")
    if overlay is None and br is None and flt is None:
        return

    # Drop the preview pane's webview before anything else: a webview that
    # outlives its window keeps answering theme_did_change and touches a
    # deleted page (the crash class upstream fixed in FindDuplicates).
    _teardown_preview()

    # Stop forcing repaints (see _on_op_executed_embedded) and note-add
    # refreshes (see _on_add_note_embedded) now that the embed is going
    # away.
    try:
        gui_hooks.operation_did_execute.remove(_on_op_executed_embedded)
    except Exception:
        pass
    try:
        gui_hooks.add_cards_did_add_note.remove(_on_add_note_embedded)
    except Exception:
        pass

    # Clear state FIRST so anything that re-enters via a close callback
    # returns immediately.
    _state["browser"] = None
    _state["overlay"] = None
    _state["filter"] = None
    _state.pop("splitter", None)
    _state.pop("central", None)
    _state.pop("esc_shortcut", None)
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

    if br is not None:
        # Synchronous cleanup. Browser's `_closeWindow()` is the same
        # path its closeEvent ends up taking after the
        # call_after_note_saved roundtrip — we skip the roundtrip
        # because we don't have a webview-driven submit flow here.
        try:
            br._closeWindow()  # type: ignore[attr-defined]
        except Exception:
            try:
                br.close()
            except Exception:
                pass

    # Restore the sidebar's active tab.
    try:
        w = getattr(mw, "web", None)
        if w is not None:
            w.eval("window.__baSetActive && window.__baSetActive('decks');")
    except Exception:
        pass


def open_inline(parent_mw: Any = None) -> None:
    """Open the Browser embedded in the main window's content area.

    Falls back to the standalone Browser window if the embed setup
    fails for any reason."""
    parent_mw = parent_mw or mw
    if _state.get("overlay") is not None:
        # Already open — bring it forward.
        try:
            _state["overlay"].raise_()
        except Exception:
            pass
        return
    if _state.get("curtain") is not None:
        # A curtain is up — we're already mid-open from a previous call.
        return

    # --- Curtain ----------------------------------------------------
    from . import addcard as _addcard
    palette, _ = _addcard._resolve_palette()
    paper_qc = QColor(palette["paper"])
    cw = parent_mw.form.centralwidget

    curtain = QFrame(cw)
    curtain.setObjectName("ba-browse-curtain")
    curtain.setAutoFillBackground(True)
    _cu_pal = curtain.palette()
    _cu_pal.setColor(QPalette.ColorRole.Window, paper_qc)
    curtain.setPalette(_cu_pal)
    curtain.setStyleSheet(
        "QFrame#ba-browse-curtain { background: " + palette["paper"] + "; }"
    )
    curtain.setGeometry(SIDEBAR_W, 0, cw.width() - SIDEBAR_W, cw.height())
    curtain.show()
    curtain.raise_()
    _state["curtain"] = curtain
    try:
        curtain.repaint()
        QApplication.processEvents()
    except Exception:
        pass

    try:
        from aqt.browser import Browser
        # Anki's Browser.__init__ ends with `self.show()`, which would
        # flash the standalone QMainWindow on screen for one paint.
        # Subclassing to no-op `show()` keeps it invisible.
        class _EmbeddedBrowser(Browser):  # type: ignore[misc, valid-type]
            def show(self) -> None:  # noqa: D401 — Qt override
                pass

            def activateWindow(self) -> None:  # noqa: N802 — Qt override
                # This "window" is a hidden shell — its widgets were
                # reparented into `mw` — so the stock implementation
                # activates nothing. The editor calls this after every
                # modal to get DOM focus back into the field it was
                # editing; addcard.reactivate_editor explains what breaks
                # when it silently does nothing.
                from . import addcard as _addcard

                _addcard.reactivate_editor(self)
        br = _EmbeddedBrowser(parent_mw)
    except Exception:
        try:
            curtain.deleteLater()
        except Exception:
            pass
        _state["curtain"] = None
        try:
            parent_mw.onBrowse()
        except Exception:
            pass
        return

    try:
        # Register with the dialog manager so future
        # `aqt.dialogs.open("Browser", ...)` calls reopen() our instance
        # instead of creating a second Browser. _closeWindow() (called
        # from close_inline) calls dialogs.markClosed("Browser") which
        # tears this registration down.
        try:
            import aqt as _aqt
            _aqt.dialogs._dialogs["Browser"][1] = br  # type: ignore[index]
        except Exception:
            pass

        central = br.centralWidget()

        # "+ Add" — lets the user add a note without leaving Browse. Sits
        # in the same QGridLayout Anki already docks the Cards/Notes
        # switch (col 0) and the search box (col 1) into
        # (Browser.setup_table: `self.form.gridLayout.addWidget(switch, 0,
        # 0)`), so it inherits the same row and native layout/resize
        # behaviour for free — col 2, to the right of the search box.
        # Styled to match addcard.py's `#ba-footer QPushButton#ba-add`
        # (the Add screen's own submit button) so the two "Add" affordances
        # read as the same control everywhere they appear.
        try:
            from . import addcard as _addcard
            palette, _ = _addcard._resolve_palette()
            accent = _addcard._config().get("accent", "#6c8cff")
            add_btn = QPushButton("+ Add")
            add_btn.setObjectName("ba-browse-add")
            add_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
            add_btn.setToolTip("Add a note without leaving Browse")
            add_btn.setStyleSheet(f"""
                QPushButton#ba-browse-add {{
                    color: {palette['paper']};
                    background: {palette['ink']};
                    border: 1px solid {palette['ink']};
                    border-radius: 6px;
                    padding: 5px 14px;
                    font-family: {_addcard.SANS};
                    font-size: 10.5pt;
                    font-weight: 500;
                    letter-spacing: 0.15px;
                }}
                QPushButton#ba-browse-add:hover {{
                    background: {palette['ink_dim']};
                    border-color: {palette['ink_dim']};
                }}
                QPushButton#ba-browse-add:pressed {{
                    background: {palette['ink_faint']};
                    border-color: {palette['ink_faint']};
                }}
                QPushButton#ba-browse-add:focus {{
                    outline: none;
                    border-color: {accent};
                }}
            """)
            add_btn.clicked.connect(
                lambda _checked=False, pm=parent_mw: _open_add_in_browse(pm)
            )
            br.form.gridLayout.addWidget(add_btn, 0, 2)
        except Exception as e:
            print(f"[anki-design.browse_embed] add button failed: {e}",
                  flush=True)

        # Inner splitter inside central: [search+table | editor]. Three
        # things have to be true for the editor pane to behave like a real
        # sidebar:
        #
        #   1. It needs a meaningful minimum width — its parent widget
        #      (verticalLayoutWidget) has min=0 in Anki's .ui, so
        #      setChildrenCollapsible(False) doesn't actually stop a drag
        #      from shrinking the editor to zero.
        #   2. The initial split has to be non-degenerate — restoreSplitter
        #      ("editor3") routinely hands back [all, 0] for fresh profiles
        #      and the editor disappears on open.
        #   3. The pane has to stay visible across selection changes —
        #      Browser.on_all_or_selected_rows_changed calls
        #      `splitter.widget(1).setVisible(self.singleCard)` via the
        #      @ensure_editor_saved decorator, which defers the call
        #      through Editor.call_after_note_saved. Overriding the method
        #      can't catch that race, so we patch setVisible on the widget
        #      itself to ignore False.
        try:
            inner = getattr(br.form, "splitter", None)
            if (
                inner is not None
                and inner.count() == 2
                and inner.orientation() == Qt.Orientation.Horizontal
            ):
                table_side = inner.widget(0)
                editor_side = inner.widget(1)
                table_side.setMinimumWidth(TABLE_MIN)
                editor_side.setMinimumWidth(EDITOR_MIN)
                # Pin both the splitter pane AND the editor's own widget
                # (Editor.set_note(None, hide=True) calls self.widget.hide()
                # — which in Browser mode is self.form.fieldsArea — so even
                # with the splitter pane forced visible, fieldsArea would
                # collapse to invisible whenever the selection cleared).
                for w in (editor_side, getattr(br.form, "fieldsArea", None)):
                    if w is None:
                        continue
                    _orig = w.setVisible

                    def _keep_visible(_v: bool, _f=_orig) -> None:
                        _f(True)

                    w.setVisible = _keep_visible  # type: ignore[assignment]
                    w.setVisible(True)
                _apply_inner_split(inner)
        except Exception:
            pass

        # Rendered-card pane above the fields. Deliberately after the
        # setVisible pinning above: _install_preview reparents fieldsArea
        # into a holder, and the pin has to already be on the widget so
        # the reparent carries it along.
        try:
            _install_preview(br)
        except Exception as e:
            print(f"[anki-design.browse_embed] preview pane failed: {e}",
                  flush=True)

        # Browser attaches its sidebar tree (decks / tags / saved searches)
        # as a QDockWidget directly to the QMainWindow, NOT inside
        # centralwidget. Discover every QDockWidget child so add-ons that
        # add their own docks are also brought along, and group them by
        # the dock area Browser placed them in.
        left_docks: list = []
        right_docks: list = []
        for dock in br.findChildren(QDockWidget):
            try:
                area = br.dockWidgetArea(dock)
            except Exception:
                area = Qt.DockWidgetArea.LeftDockWidgetArea
            inner = dock.widget()
            if inner is None:
                continue
            if area == Qt.DockWidgetArea.RightDockWidgetArea:
                right_docks.append(inner)
            else:
                # Treat anything that isn't explicitly right (left, top,
                # bottom, no-area) as left, matching the Browser's stock
                # placement of the sidebar.
                left_docks.append(inner)

        overlay = QFrame(parent_mw.form.centralwidget)
        overlay.setObjectName("ba-browse-embed")
        overlay.setAutoFillBackground(True)
        _ov_pal = overlay.palette()
        _ov_pal.setColor(QPalette.ColorRole.Window, paper_qc)
        overlay.setPalette(_ov_pal)
        overlay.setStyleSheet(_palette_styles())

        v = QVBoxLayout(overlay)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)

        # Layout: [left docks] | [centralwidget: search+table | editor] |
        # [right docks], wrapped in a QSplitter so each boundary stays
        # drag-resizable — matching the QDockWidget UX in native Browser.
        central.setParent(None)
        central.setWindowFlags(Qt.WindowType.Widget)

        if left_docks or right_docks:
            splitter = QSplitter(Qt.Orientation.Horizontal, overlay)
            splitter.setChildrenCollapsible(False)
            splitter.setHandleWidth(6)
            splitter.setStyleSheet(
                "QSplitter::handle { background: " + palette["line"] + "; }"
                "QSplitter::handle:hover { background: "
                + palette["line2"] + "; }"
            )
            # Use min/max width on the dock contents AND clamp via
            # splitterMoved — Qt respects min/max for widget size but lets the
            # handle drag past max, leaving a dead visual gap. The signal
            # handler re-sets sizes so the handle snaps to where it should.
            for inner in left_docks:
                inner.setParent(None)
                inner.setMinimumWidth(SIDEBAR_DOCK_MIN)
                inner.setMaximumWidth(SIDEBAR_DOCK_MAX)
                splitter.addWidget(inner)
            central.setMinimumWidth(CENTRAL_MIN)
            splitter.addWidget(central)
            for inner in right_docks:
                inner.setParent(None)
                inner.setMinimumWidth(SIDEBAR_DOCK_MIN)
                inner.setMaximumWidth(SIDEBAR_DOCK_MAX)
                splitter.addWidget(inner)
            splitter.splitterMoved.connect(
                lambda *_a, sp=splitter, c=central: _clamp_splitter(sp, c)
            )
            _state["central"] = central
            for i in range(splitter.count()):
                splitter.setStretchFactor(
                    i, 1 if splitter.widget(i) is central else 0
                )
            v.addWidget(splitter, 1)
            _state["splitter"] = splitter
            _apply_initial_splitter_sizes(splitter, central)
        else:
            central.setParent(overlay)
            v.addWidget(central, 1)

        # Hide the original Browser QMainWindow.
        br.setVisible(False)

        # Tint mw.centralwidget paper so any Qt-painted gap is invisible.
        try:
            _state["cw_palette"] = QPalette(cw.palette())
            _cw_pal = cw.palette()
            _cw_pal.setColor(QPalette.ColorRole.Window, paper_qc)
            cw.setPalette(_cw_pal)
        except Exception:
            pass

        overlay.setGeometry(
            SIDEBAR_W, 0, cw.width() - SIDEBAR_W, cw.height()
        )
        overlay.show()
        overlay.raise_()
        try:
            curtain.raise_()
        except Exception:
            pass

        # Curtain drop: backstop on editor webview load + grace, hard cap.
        try:
            web = getattr(br.editor, "web", None) if br.editor else None
            if web is not None:
                page = web.page()
                if page is not None:
                    def _on_loaded(_ok: bool) -> None:
                        QTimer.singleShot(280, drop_curtain)
                    page.loadFinished.connect(_on_loaded)
        except Exception:
            pass
        QTimer.singleShot(900, drop_curtain)

        flt = _EmbedFilter(overlay)
        cw.installEventFilter(flt)

        # Esc closes the embed. Browser binds its own Esc inside
        # keyPressEvent on the QMainWindow, but our QMainWindow is
        # hidden so it never gets focus events.
        try:
            esc = QShortcut(QKeySequence("Escape"), overlay)
            esc.setAutoRepeat(False)
            esc.setContext(Qt.ShortcutContext.WindowShortcut)
            esc.activated.connect(close_inline)
            # Stashed so _open_add_in_browse can disable it while an Add
            # panel is stacked on top — two WindowShortcut QShortcuts on
            # the same key in the same top-level window are ambiguous to
            # Qt and neither fires.
            _state["esc_shortcut"] = esc
        except Exception:
            pass

        _state["browser"] = br
        _state["overlay"] = overlay
        _state["filter"] = flt

        # See _on_op_executed_embedded: the embedded Browser can never be
        # Qt's current_window(), so stock Table.op_executed's `if focused:
        # self.redraw_cells()` never fires for us and the table can paint
        # stale/blank after a reset (e.g. every sync attempt, success or
        # failure — on_collection_sync_finished calls mw.reset()
        # unconditionally). Force the repaint ourselves while open.
        try:
            gui_hooks.operation_did_execute.remove(_on_op_executed_embedded)
        except Exception:
            pass
        gui_hooks.operation_did_execute.append(_on_op_executed_embedded)

        # See _on_add_note_embedded: pulls a note added via the in-Browse
        # "+ Add" button (_open_add_in_browse) into the visible results.
        try:
            gui_hooks.add_cards_did_add_note.remove(_on_add_note_embedded)
        except Exception:
            pass
        gui_hooks.add_cards_did_add_note.append(_on_add_note_embedded)

        # Highlight "Browse" in the sidebar.
        try:
            w = getattr(parent_mw, "web", None)
            if w is not None:
                w.eval(
                    "window.__baSetActive && window.__baSetActive('browse');"
                )
        except Exception:
            pass
    except Exception as e:
        import traceback
        print(
            f"[anki-design.browse_embed] failed: {e}\n"
            f"{traceback.format_exc()}",
            flush=True,
        )
        try:
            close_inline()
        except Exception:
            pass
        try:
            br.show()
        except Exception:
            pass
