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

from typing import Any, Dict

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
    QSize,
    QSplitter,
    Qt,
    QTimer,
    QToolButton,
    QVBoxLayout,
    QWidget,
)


# Per-module config, same pattern as browse_preview.py / deadlines.py /
# settings.py etc.
ADDON = __name__.split(".")[0]


def _pane_config() -> Dict[str, Any]:
    try:
        return mw.addonManager.getConfig(ADDON) or {}
    except Exception:
        return {}


def _save_pane_collapse(*, tree: bool | None = None, table: bool | None = None) -> None:
    """Persist the Browse pane collapse toggles to the add-on config —
    survives restart, same storage as every other add-on setting."""
    try:
        cfg = mw.addonManager.getConfig(ADDON) or {}
        if tree is not None:
            cfg["browse_tree_collapsed"] = bool(tree)
        if table is not None:
            cfg["browse_table_collapsed"] = bool(table)
        mw.addonManager.writeConfig(ADDON, cfg)
    except Exception:
        pass


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
            if not w.isVisible():
                # Collapsed via the Sidebar toggle (see _set_tree_collapsed)
                # — leave it at 0 rather than clamping back up to MIN.
                sizes[i] = 0
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
            table_side = inner.widget(0)
            if table_side is not None and not table_side.isVisible():
                # Collapsed via the Table toggle — give the editor/preview
                # side the room.
                inner.setSizes([0, avail])
                return
            editor_w = max(EDITOR_MIN, min(EDITOR_PREF, avail // 2))
            table_w = max(TABLE_MIN, avail - editor_w)
            inner.setSizes([table_w, editor_w])
        except Exception:
            pass

    QTimer.singleShot(0, _set)


def _reflow_outer_splitter(splitter: "QSplitter", central: Any) -> None:
    """Recompute [docks | central] sizes, giving 0 width to any collapsed
    (hidden) dock and handing the freed space to central. Shared by the
    initial layout pass (_apply_initial_splitter_sizes) and by the Sidebar
    collapse toggle (_set_tree_collapsed) — same math, so toggling never
    leaves a dead gap the way a plain clamp would."""
    try:
        count = splitter.count()
        handles = max(0, count - 1) * splitter.handleWidth()
        avail = max(0, splitter.width() - handles)
        visible = [
            i for i in range(count)
            if splitter.widget(i) is not central and splitter.widget(i).isVisible()
        ]
        n = len(visible)
        per = (
            min(SIDEBAR_DOCK_PREF, max(SIDEBAR_DOCK_MIN, (avail - CENTRAL_MIN) // n))
            if n else 0
        )
        central_w = max(CENTRAL_MIN, avail - per * n)
        sizes = []
        for i in range(count):
            if splitter.widget(i) is central:
                sizes.append(central_w)
            elif i in visible:
                sizes.append(per)
            else:
                sizes.append(0)
        splitter.setSizes(sizes)
    except Exception:
        pass


def _apply_initial_splitter_sizes(splitter: "QSplitter", central: Any) -> None:
    """Set initial splitter sizes from the overlay's actual width so a
    narrow window doesn't end up with a 100px sidebar after QSplitter
    proportionally scales setSizes(). Deferred via QTimer.singleShot(0)
    so the overlay has been laid out and width() is meaningful."""
    QTimer.singleShot(0, lambda: _reflow_outer_splitter(splitter, central))


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
# Back arrow — the CARD panel's "leave Add, return to the card" affordance.
_BACK_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" '
    'width="16" height="16" fill="none" stroke="{c}" stroke-width="1.9" '
    'stroke-linecap="round" stroke-linejoin="round">'
    '<path d="M19 12H5"/><path d="M11 18l-6-6 6-6"/></svg>'
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
    # Third mode — Add — hijacks the same header/stack pattern. Only one
    # of edit_btn / back_btn is visible at a time (see _paint_header).
    back_btn = QToolButton(header)
    back_btn.setObjectName("ba-browse-pane-back")
    back_btn.setCursor(Qt.CursorShape.PointingHandCursor)
    back_btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
    back_btn.setIconSize(QSize(16, 16))
    back_btn.setVisible(False)
    back_btn.clicked.connect(lambda: _close_add_panel())
    hrow.addWidget(back_btn)

    def _paint_header() -> None:
        """Icon + label for whichever mode we're in, in palette colours."""
        try:
            from . import addcard as _addcard

            p, _dark = _addcard._resolve_palette()
            if _state.get("pane_add_ac") is not None:
                edit_btn.setVisible(False)
                back_btn.setVisible(True)
                back_btn.setIcon(_svg_icon(_BACK_SVG, p["ink_dim"]))
                back_btn.setText("Back")
                back_btn.setToolTip("Back to the card (Esc)")
                label.setText("Add")
                return
            back_btn.setVisible(False)
            edit_btn.setVisible(True)
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
        if _state.get("pane_add_ac") is not None:
            # Add is showing — edit_btn is hidden and shouldn't be able to
            # drive the stack, but guard anyway (e.g. a queued signal).
            return
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
        if _state.get("pane_add_ac") is not None:
            # Add's page is being shown/hidden — _paint_header already
            # knows to read pane_add_ac, not the raw index.
            _paint_header()
            return
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
    _state["pane_back_btn"] = back_btn
    _state["pane_paint"] = _paint_header
    _paint_header()
    # First paint: the webview needs a beat to finish loading reviewer.js
    # before `_showAnswer` means anything.
    QTimer.singleShot(350, pane.render)


def _teardown_preview() -> None:
    # If the Add panel is open, close it (and its AddCards instance) first
    # — otherwise closing Browse would leave a live AddCards with no
    # parent stack, and note-save hooks would go on firing at nothing.
    if _state.get("pane_add_ac") is not None:
        try:
            _close_add_panel()
        except Exception:
            pass
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
    for key in ("pane_stack", "pane_head", "pane_edit_btn", "pane_back_btn", "pane_paint"):
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
    toolbar = _state.get("toolbar")
    if toolbar is not None:
        try:
            toolbar.setStyleSheet(
                "QWidget#ba-browse-toolbar { background: " + palette["paper"]
                + "; border-bottom: 1px solid " + palette["line"] + "; }"
                "QWidget#ba-browse-toolbar QToolButton { color: "
                + palette["ink_dim"] + "; background: transparent; border: 1px "
                "solid " + palette["line2"] + "; border-radius: 6px; padding: "
                "3px 10px; font-size: 9.5pt; }"
                "QWidget#ba-browse-toolbar QToolButton:checked { color: "
                + palette["ink"] + "; border-color: " + palette["ink_faint"] + "; }"
                "QWidget#ba-browse-toolbar QToolButton:hover { background: "
                + palette["hover"] + "; }"
                "QWidget#ba-browse-toolbar QToolButton:disabled { opacity: 0.4; }"
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


def reflow() -> None:
    """Reposition the open overlay/curtain to the current sidebar width.

    Called from __init__.py's `ba:sidebar-collapse` pycmd handler right
    after the config flip, so Browse (if open) doesn't sit at the old
    offset until the next window resize."""
    try:
        cw = mw.form.centralwidget
        w = _sidebar_w()
        for key in ("overlay", "curtain"):
            widget = _state.get(key)
            if widget is not None:
                widget.setGeometry(w, 0, cw.width() - w, cw.height())
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
                w = _sidebar_w()
                self._overlay.setGeometry(
                    w,
                    0,
                    cw.width() - w,
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


def _open_add_in_panel(parent_mw: Any) -> None:
    """Turn the CARD panel (the right-hand pane that normally shows the
    selected row's rendered question/answer, or its fields in Edit mode)
    into the add-a-note form, in place — no overlay stacked on top of the
    table or the sidebar.

    This is the "+ Add" button's normal path now. It reuses `addcard_-
    embed.create_inline` — the exact same AddCards construction that
    backs the standalone Add screen, so the inline notetype/deck pickers
    and the save button (`ac._ba_safe_add`) are identical, not a second
    implementation. Only the mounting differs: instead of an overlay
    QFrame positioned over the table, `ac.centralWidget()` becomes a new
    page on the CARD panel's existing QStackedWidget (`_state["pane_-
    stack"]`, built by `_install_preview` for the Card/Editing modes) and
    the header swaps its pencil/Done toggle for a single "Back" button.

    Falls back to the old stacked-overlay Add (`_open_add_in_browse`,
    kept for the dev screenshot harness and for the case where the
    preview pane isn't installed at all) if the panel isn't available or
    construction fails."""
    stack = _state.get("pane_stack")
    if stack is None:
        # browse_preview disabled, or _install_preview hasn't run for some
        # other reason — nowhere to mount the panel. Old behaviour.
        _open_add_in_browse(parent_mw)
        return
    if _state.get("pane_add_ac") is not None:
        # Already open — just bring its page forward.
        idx = _state.get("pane_add_index")
        if idx is not None:
            try:
                stack.setCurrentIndex(idx)
            except Exception:
                pass
        return

    from . import addcard_embed
    ac = addcard_embed.create_inline(parent_mw)
    if ac is None:
        _open_add_in_browse(parent_mw)
        return

    try:
        central = ac.centralWidget()
        central.setParent(stack)
        central.setWindowFlags(Qt.WindowType.Widget)
        idx = stack.addWidget(central)

        # Chain onto AddCards' own _close so any internal close path also
        # clears our bookkeeping (mirrors addcard_embed.open_inline's own
        # wrap-and-chain of the same method).
        try:
            orig_close = ac._close  # type: ignore[attr-defined]

            def _wrapped_close(orig=orig_close) -> None:
                try:
                    orig()
                finally:
                    if _state.get("pane_add_ac") is ac:
                        _state["pane_add_ac"] = None
            ac._close = _wrapped_close  # type: ignore[assignment]
        except Exception:
            pass

        _state["pane_add_ac"] = ac
        _state["pane_add_widget"] = central
        _state["pane_add_index"] = idx
        stack.setCurrentIndex(idx)

        # Cmd/Ctrl+Return and Cmd/Ctrl+Enter submit the note. AddCards'
        # own shortcuts targeted its now-orphaned buttonBox, so rebuild
        # them here exactly like addcard_embed.open_inline does for the
        # full-overlay case.
        try:
            safe_add = getattr(ac, "_ba_safe_add", None) or ac.add_current_note
            shortcuts = []
            for keys in ("Ctrl+Return", "Ctrl+Enter"):
                sc = QShortcut(QKeySequence(keys), central)
                sc.setAutoRepeat(False)
                sc.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
                sc.activated.connect(safe_add)
                shortcuts.append(sc)
            _state["pane_add_shortcuts"] = shortcuts
        except Exception:
            pass

        try:
            ac.editor.web.setFocus()
        except Exception:
            pass
    except Exception as e:
        import traceback
        print(
            f"[anki-design.browse_embed] add-in-panel failed: {e}\n"
            f"{traceback.format_exc()}",
            flush=True,
        )
        _state.pop("pane_add_ac", None)
        _state.pop("pane_add_widget", None)
        _state.pop("pane_add_index", None)
        try:
            addcard_embed.teardown_inline(ac)
        except Exception:
            pass
        _open_add_in_browse(parent_mw)
        return

    _paint_header_safe()


def _paint_header_safe() -> None:
    paint = _state.get("pane_paint")
    if paint is not None:
        try:
            paint()
        except Exception:
            pass


def _close_add_panel() -> None:
    """Leave Add mode and return the CARD panel to showing the selected
    row. Called by the header's Back button, by Escape (see open_inline's
    shortcut wiring below), and from _teardown_preview when Browse itself
    is closing while Add is still open."""
    ac = _state.pop("pane_add_ac", None)
    widget = _state.pop("pane_add_widget", None)
    idx = _state.pop("pane_add_index", None)
    for sc in _state.pop("pane_add_shortcuts", None) or []:
        try:
            sc.setParent(None)
            sc.deleteLater()
        except Exception:
            pass
    stack = _state.get("pane_stack")
    if stack is not None:
        try:
            stack.setCurrentIndex(0)
        except Exception:
            pass
        if widget is not None:
            try:
                stack.removeWidget(widget)
            except Exception:
                pass
    if ac is not None:
        try:
            from . import addcard_embed
            addcard_embed.teardown_inline(ac)
        except Exception:
            pass
    _paint_header_safe()
    # Card may have changed underneath (e.g. the note just added) — make
    # sure the read pane reflects the current row rather than a stale one.
    pane = _state.get("preview")
    if pane is not None:
        try:
            pane.render(force=True)
        except Exception:
            pass


def _open_add_in_browse(parent_mw: Any) -> None:
    """Fallback / dev-tool path: stack the full-overlay Add screen on top
    of Browse (the original implementation, before Add moved into the
    CARD panel). Still used when the CARD panel isn't available, and by
    `__init__.py`'s `_dev_screenshot` smoke-test harness which looks for
    `addcard_embed._state["overlay"]`.

    Reuses `addcard_embed.open_inline` wholesale — no parallel Add
    implementation. This opens the Add panel stacked on top of Browse's
    own overlay: both are QFrames positioned at the same geometry on
    `parent_mw.form.centralwidget`, so the Add panel visually covers the
    table without destroying Browse underneath. Closing Add (Esc /
    save-and-close) reveals Browse exactly as it was — same search, same
    selection — and `addcard_embed.close_inline`'s sidebar-tab restore is
    taught (see below) to land back on "browse" instead of "decks" when
    this embed is still open underneath.

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
    for key in (
        "left_docks", "table_side", "inner_splitter", "toolbar",
        "tree_toggle_btn", "table_toggle_btn",
    ):
        _state.pop(key, None)
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


def _set_tree_collapsed(collapsed: bool, *, persist: bool = True) -> None:
    """Show/hide the Browser's left DECKS/TAGS dock(s) (Item 3).

    The dock widgets are hidden, not removed: QSplitter treats a hidden
    child as zero-width and _reflow_outer_splitter hands its space to
    central, so this is fully reversible via the toolbar's Sidebar button
    and disturbs nothing live inside the dock (its tree state, scroll
    position, selection all survive untouched)."""
    docks = _state.get("left_docks") or []
    for w in docks:
        try:
            w.setVisible(not collapsed)
        except Exception:
            pass
    sp = _state.get("splitter")
    c = _state.get("central")
    if sp is not None and c is not None:
        _reflow_outer_splitter(sp, c)
    btn = _state.get("tree_toggle_btn")
    if btn is not None:
        try:
            btn.blockSignals(True)
            btn.setChecked(not collapsed)
            btn.blockSignals(False)
        except Exception:
            pass
    if persist:
        _save_pane_collapse(tree=collapsed)


def _set_table_collapsed(collapsed: bool, *, persist: bool = True) -> None:
    """Show/hide the card-list table (Item 3), handing its space to the
    editor/CARD pane. Only visibility changes — the QTableView's model
    and selection are untouched by hiding the pane, so the CARD panel
    keeps showing the same row (and the preview stays correct) whether
    the table is visible or not; nothing about "collapsing the table"
    here can break selection."""
    table_side = _state.get("table_side")
    if table_side is not None:
        try:
            table_side.setVisible(not collapsed)
        except Exception:
            pass
    inner = _state.get("inner_splitter")
    if inner is not None:
        try:
            avail = inner.width() - inner.handleWidth()
            if avail > 0:
                if collapsed:
                    inner.setSizes([0, avail])
                else:
                    editor_w = max(EDITOR_MIN, min(EDITOR_PREF, avail // 2))
                    table_w = max(TABLE_MIN, avail - editor_w)
                    inner.setSizes([table_w, editor_w])
        except Exception:
            pass
    btn = _state.get("table_toggle_btn")
    if btn is not None:
        try:
            btn.blockSignals(True)
            btn.setChecked(not collapsed)
            btn.blockSignals(False)
        except Exception:
            pass
    if persist:
        _save_pane_collapse(table=collapsed)


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
    curtain.setGeometry(_sidebar_w(), 0, cw.width() - _sidebar_w(), cw.height())
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
                lambda _checked=False, pm=parent_mw: _open_add_in_panel(pm)
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
                _state["inner_splitter"] = inner
                _state["table_side"] = table_side
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
        _state["left_docks"] = left_docks

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

        # Slim, always-visible toolbar above the panes it controls —
        # deliberately OUTSIDE the splitter/central pane, so collapsing
        # either the tree or the table can never make its own re-expand
        # control disappear along with it (the "+ Add" button and search
        # box, by contrast, live inside table_side — collapsing the table
        # hides them too, which is fine, they're not how you get the
        # table back).
        toolbar = QWidget(overlay)
        toolbar.setObjectName("ba-browse-toolbar")
        trow = QHBoxLayout(toolbar)
        trow.setContentsMargins(10, 5, 10, 5)
        trow.setSpacing(6)

        def _mk_pane_toggle(text: str, tip: str) -> "QToolButton":
            b = QToolButton(toolbar)
            b.setCheckable(True)
            b.setChecked(True)
            b.setText(text)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
            b.setToolTip(tip)
            return b

        tree_btn = _mk_pane_toggle(
            "Decks", "Show/hide the Decks & Tags panel"
        )
        tree_btn.setEnabled(bool(left_docks))
        table_btn = _mk_pane_toggle(
            "Cards", "Show/hide the card list — frees space for the editor"
        )
        trow.addWidget(tree_btn)
        trow.addWidget(table_btn)
        trow.addStretch(1)
        try:
            toolbar.setStyleSheet(
                "QWidget#ba-browse-toolbar { background: " + palette["paper"]
                + "; border-bottom: 1px solid " + palette["line"] + "; }"
                "QWidget#ba-browse-toolbar QToolButton { color: "
                + palette["ink_dim"] + "; background: transparent; border: 1px "
                "solid " + palette["line2"] + "; border-radius: 6px; padding: "
                "3px 10px; font-size: 9.5pt; }"
                "QWidget#ba-browse-toolbar QToolButton:checked { color: "
                + palette["ink"] + "; border-color: " + palette["ink_faint"] + "; }"
                "QWidget#ba-browse-toolbar QToolButton:hover { background: "
                + palette["hover"] + "; }"
                "QWidget#ba-browse-toolbar QToolButton:disabled { opacity: 0.4; }"
            )
        except Exception:
            pass
        v.addWidget(toolbar)
        _state["toolbar"] = toolbar
        _state["tree_toggle_btn"] = tree_btn
        _state["table_toggle_btn"] = table_btn

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

        tree_btn.toggled.connect(lambda checked: _set_tree_collapsed(not checked))
        table_btn.toggled.connect(lambda checked: _set_table_collapsed(not checked))

        # Restore persisted collapse state (Item 3). Deferred like the
        # sizing calls above (_apply_inner_split / _apply_initial_splitter_-
        # sizes) so real widths are available, and scheduled after both of
        # them so this has the final say over how space is distributed.
        def _restore_pane_collapse() -> None:
            try:
                pcfg = _pane_config()
                if left_docks and bool(pcfg.get("browse_tree_collapsed", False)):
                    _set_tree_collapsed(True, persist=False)
                if bool(pcfg.get("browse_table_collapsed", False)):
                    _set_table_collapsed(True, persist=False)
            except Exception:
                pass

        QTimer.singleShot(0, _restore_pane_collapse)

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

        # Esc closes the embed — or, if the CARD panel is showing the Add
        # form, leaves Add first and reveals the card again (mirrors the
        # old stacked-overlay Add's Esc behaviour, without needing the
        # ambiguous-shortcut workaround that path required: there's only
        # ever one WindowShortcut Escape now). Browser binds its own Esc
        # inside keyPressEvent on the QMainWindow, but our QMainWindow is
        # hidden so it never gets focus events.
        def _on_escape() -> None:
            if _state.get("pane_add_ac") is not None:
                _close_add_panel()
            else:
                close_inline()

        try:
            esc = QShortcut(QKeySequence("Escape"), overlay)
            esc.setAutoRepeat(False)
            esc.setContext(Qt.ShortcutContext.WindowShortcut)
            esc.activated.connect(_on_escape)
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
