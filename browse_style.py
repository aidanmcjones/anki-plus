"""Anki Design — Browser chrome restyle.

The Browser is pure Qt (QTableView + QTreeView + QDockWidget + QSplitter),
so nothing in `web/` reaches it. This module paints it in the same
language as the rest of Anki+: paper background, hairline rules, flat
uppercase column labels, typographic hover (a wash, never a bevel), and
the accent only where a selection genuinely is.

Design notes
------------
* **Widget-level, not app-level.** `gui_hooks.style_did_init` would let us
  append to Anki's *application* stylesheet, but that sheet is global —
  every dialog in Anki would inherit our rules and we'd have to fight
  specificity forever. Instead we set the stylesheet on the two widgets
  the Browser is actually built from (`form.centralwidget` and the
  sidebar dock's container). Both survive `browse_embed`'s reparenting
  into the overlay, so the embed and the standalone window get the same
  treatment from one code path.
* **The sidebar tree fights back.** `SidebarTreeView._setup_style` sets a
  stylesheet *on the tree itself*, which beats anything inherited from an
  ancestor, and Anki re-runs it on every theme change. We rebind the
  bound method on the instance so both the initial call and the
  `theme_did_change` re-run land on ours.
* **Everything resolves through `addcard._resolve_palette()`** so the
  user's theme + background override in Settings → Appearance follow the
  Browser too. No hardcoded hex lives in this file.
"""

from __future__ import annotations

import os
from typing import Any, Dict, Optional

from aqt import gui_hooks, mw
from aqt.qt import (
    QAbstractItemView,
    QFont,
    QHeaderView,
    QIcon,
    QStyledItemDelegate,
    Qt,
)


ADDON = __name__.split(".")[0]

# Type scale for the table. Points (not px) so it tracks the platform's
# UI scaling the way the rest of the Qt chrome does.
TABLE_PT = 10.5
HEADER_PT = 8.5
ROW_HEIGHT_PAD = 12  # added to Anki's computed max template line height


def _config() -> Dict[str, Any]:
    try:
        return mw.addonManager.getConfig(ADDON) or {}
    except Exception:
        return {}


def enabled() -> bool:
    return bool(_config().get("browse_restyle", True))


def _palette():
    from . import addcard as _addcard

    return _addcard._resolve_palette()


def _accent() -> str:
    from . import colors as _colors

    return _colors.hex_ok(_config().get("accent")) or "#6c8cff"


def _sel_colors(p: Dict[str, str], is_dark: bool, accent: str) -> tuple[str, str]:
    """(selection background, selection foreground).

    A full accent fill would shout; the deck list selects with a quiet
    wash and keeps the text in ink. We mix the accent into the paper so
    the selection reads as "this row", not "this row is a button"."""
    from . import colors as _colors

    bg = _colors.mix(p["paper"], accent, 0.30 if is_dark else 0.22)
    return bg, p["ink"]


# --------------------------------------------------------------------------- #
# Stylesheets
# --------------------------------------------------------------------------- #
def _scrollbars(p: Dict[str, str]) -> str:
    """12px, no arrows, handle only visible against a hover. Matches the
    settings page's scrollbar treatment, one size up because the table
    scrolls a lot further."""
    return f"""
QScrollBar:vertical {{
    background: transparent;
    width: 12px;
    margin: 2px 2px 2px 0;
}}
QScrollBar:horizontal {{
    background: transparent;
    height: 12px;
    margin: 0 2px 2px 2px;
}}
QScrollBar::handle:vertical {{
    background: {p['line2']};
    border-radius: 4px;
    min-height: 36px;
}}
QScrollBar::handle:horizontal {{
    background: {p['line2']};
    border-radius: 4px;
    min-width: 36px;
}}
QScrollBar::handle:vertical:hover,
QScrollBar::handle:horizontal:hover {{ background: {p['ink_faint']}; }}
QScrollBar::add-line, QScrollBar::sub-line {{
    height: 0; width: 0; border: 0; background: transparent;
}}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}
"""


def central_qss(p: Dict[str, str], is_dark: bool, accent: str) -> str:
    """QSS for the Browser's central widget: search bar, Cards/Notes row,
    card table, editor pane wrapper, splitters, scrollbars."""
    sel_bg, sel_fg = _sel_colors(p, is_dark, accent)
    from . import addcard as _addcard

    sans = _addcard.SANS
    return f"""
QWidget {{
    background: {p['paper']};
    color: {p['ink']};
    font-family: {sans};
}}

/* ---------------- Search bar ----------------
   Anki gives us an editable QComboBox. We want it to read as the one
   input on the page: a field-toned rectangle with an 8px radius and a
   hairline that goes accent on focus. */
QComboBox#searchEdit {{
    background: {p['field_bg']};
    color: {p['ink']};
    border: 1px solid {p['line']};
    border-radius: 8px;
    padding: 7px 30px 7px 12px;
    font-size: {TABLE_PT}pt;
    min-height: 18px;
    selection-background-color: {accent};
    selection-color: {p['paper']};
}}
QComboBox#searchEdit:focus {{
    border-color: {accent};
    outline: none;
}}
QComboBox#searchEdit::drop-down {{
    subcontrol-origin: padding;
    subcontrol-position: top right;
    width: 26px;
    border: 0;
    background: transparent;
}}
QComboBox#searchEdit::down-arrow {{
    image: url({_chevron()});
    width: 10px;
    height: 6px;
}}
QComboBox#searchEdit QAbstractItemView {{
    background: {p['panel']};
    color: {p['ink']};
    border: 1px solid {p['line2']};
    border-radius: 8px;
    padding: 5px 0;
    outline: 0;
    selection-background-color: {sel_bg};
    selection-color: {sel_fg};
}}

/* ---------------- Card table ----------------
   Flat: no frame, no gridlines, no alternating bands, no bevelled
   header. The rows separate themselves through leading alone, exactly
   like the deck list. */
QTableView {{
    background: {p['paper']};
    alternate-background-color: {p['paper']};
    color: {p['ink']};
    border: 0;
    border-top: 1px solid {p['line']};
    border-radius: 0;
    gridline-color: transparent;
    outline: 0;
    font-size: {TABLE_PT}pt;
    selection-background-color: {sel_bg};
    selection-color: {sel_fg};
}}
QTableView::item {{
    border: 0;
    padding: 0 10px;
}}
QTableView::item:hover {{ background: {p['hover']}; }}
QTableView::item:selected {{
    background: {sel_bg};
    color: {sel_fg};
}}

/* Column labels — small, uppercase (the model is patched to upper-case
   the strings; QSS has no text-transform), letterspaced, faint. Flat
   fill so Anki's button gradient on hover/press never appears. */
QHeaderView {{
    background: {p['paper']};
    border: 0;
}}
QHeaderView::section {{
    background: {p['paper']};
    color: {p['ink_faint']};
    border: 0;
    border-bottom: 1px solid {p['line']};
    padding: 7px 10px 6px 10px;
    margin: 0;
    font-size: {HEADER_PT}pt;
    font-weight: 600;
    letter-spacing: 0.09em;
}}
QHeaderView::section:hover {{
    background: {p['paper']};
    color: {p['ink_dim']};
}}
QHeaderView::section:pressed {{
    background: {p['paper']};
    color: {p['ink']};
}}
QHeaderView::section:first, QHeaderView::section:last,
QHeaderView::section:only-one, QHeaderView::section:!first {{
    border-left: 0;
    border-right: 0;
    border-top: 0;
    border-radius: 0;
}}
QHeaderView::up-arrow, QHeaderView::down-arrow {{
    width: 9px;
    height: 6px;
    margin-right: 8px;
    subcontrol-position: center right;
}}
QHeaderView::up-arrow {{ image: url({_chevron("chevron-up.svg")}); }}
QHeaderView::down-arrow {{ image: url({_chevron()}); }}

/* ---------------- Splitters ---------------- */
QSplitter::handle {{ background: {p['line']}; }}
QSplitter::handle:hover {{ background: {p['line2']}; }}
QSplitter::handle:horizontal {{ width: 1px; }}
QSplitter::handle:vertical {{ height: 1px; }}

/* The editor pane's own frame — no border, just paper. */
QWidget#verticalLayoutWidget, QWidget#fieldsArea {{
    background: {p['paper']};
    border: 0;
}}

{_scrollbars(p)}
"""


def sidebar_qss(p: Dict[str, str], is_dark: bool, accent: str) -> str:
    """QSS for the sidebar dock's container: the filter field, the little
    tool row above the tree, and the tree itself."""
    sel_bg, sel_fg = _sel_colors(p, is_dark, accent)
    from . import addcard as _addcard

    sans = _addcard.SANS
    return f"""
QWidget {{
    background: {p['paper']};
    color: {p['ink']};
    font-family: {sans};
}}
QDockWidget {{
    background: {p['paper']};
    border: 0;
    titlebar-close-icon: none;
}}

/* Sidebar filter — same input treatment as the search bar, one notch
   quieter (it filters the tree, it isn't the primary action). */
QLineEdit {{
    background: {p['field_bg']};
    color: {p['ink']};
    border: 1px solid {p['line']};
    border-radius: 8px;
    padding: 6px 10px;
    font-size: 10pt;
    selection-background-color: {accent};
    selection-color: {p['paper']};
}}
QLineEdit:focus {{ border-color: {accent}; outline: none; }}

QToolBar {{ background: transparent; border: 0; padding: 0; spacing: 2px; }}
QToolButton {{
    background: transparent;
    border: 0;
    border-radius: 6px;
    padding: 4px;
}}
QToolButton:hover {{ background: {p['hover']}; }}
QToolButton:checked {{ background: {p['hover']}; }}

{_scrollbars(p)}
"""


def tree_qss(p: Dict[str, str], is_dark: bool, accent: str) -> str:
    """The stylesheet we install *on* SidebarTreeView, replacing Anki's
    `_setup_style` output. Widget-level so it wins over the container's
    sheet, which is also why Anki put its own there."""
    sel_bg, sel_fg = _sel_colors(p, is_dark, accent)
    from . import addcard as _addcard

    return f"""
QTreeView {{
    background: {p['paper']};
    color: {p['ink']};
    border: 0;
    outline: 0;
    padding: 4px 4px 4px 0;
    font-family: {_addcard.SANS};
    font-size: 10.5pt;
    /* 0, not 1: the selection is a contained pill around the label, so it
       must stop at the item and never bleed left into the chevron gutter
       (which is where Qt's own bright highlight showed up, boxing the
       expand arrow in accent blue). */
    show-decoration-selected: 0;
}}
QTreeView::item {{
    border: 0;
    border-radius: 6px;
    padding: 4px 2px;
    margin: 1px 4px 1px 0;
    color: {p['ink_dim']};
}}
QTreeView::item:hover {{
    background: {p['hover']};
    color: {p['ink']};
}}
QTreeView::item:selected, QTreeView::item:selected:active,
QTreeView::item:selected:!active {{
    background: {sel_bg};
    color: {sel_fg};
}}
{_branch_qss(p)}
{_scrollbars(p)}
"""


def _chevron(name: str = "chevron-down.svg") -> str:
    return os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "web", name
    ).replace(os.sep, "/")


# The stroke colour baked into the shipped web/chevron-*.svg files. We
# swap it for a palette colour when tinting; if the assets are ever
# re-drawn, this is the one string that has to keep matching.
_CHEVRON_STROKE = "#7a7468"
_icon_cache: Dict[tuple, str] = {}


def _tinted_chevron(name: str, color: str) -> str:
    """`web/<name>` re-stroked in `color`, written to a temp file, path
    returned for QSS `url()`.

    Qt's stylesheet engine can't recolour an SVG — no `currentColor`, no
    filters, no palette binding — so a themed chevron means one file per
    (direction × theme × hover) combination. Rather than check eight
    near-identical assets into `web/`, we keep the single shipped shape as
    the source of truth and tint it at runtime. That also means the
    chevrons follow a custom accent or background override, not just
    light/dark.

    Falls back to the untinted asset if anything goes wrong — a slightly
    off-tone chevron beats no chevron, which is the bug this fixes."""
    key = (name, color)
    cached = _icon_cache.get(key)
    if cached and os.path.exists(cached):
        return cached
    src = _chevron(name)
    try:
        import tempfile

        with open(src, "r", encoding="utf-8") as fh:
            svg = fh.read()
        svg = svg.replace(_CHEVRON_STROKE, color)
        out_dir = os.path.join(tempfile.gettempdir(), "anki-design-icons")
        os.makedirs(out_dir, exist_ok=True)
        stem = name.rsplit(".", 1)[0]
        out = os.path.join(
            out_dir, f"{stem}-{color.lstrip('#').replace(',', '').replace('(', '')}.svg"
        )
        with open(out, "w", encoding="utf-8") as fh:
            fh.write(svg)
        _icon_cache[key] = out.replace(os.sep, "/")
        return _icon_cache[key]
    except Exception:
        return src


def _branch_qss(p: Dict[str, str]) -> str:
    """Expand/collapse chevrons for the sidebar tree.

    Styling *anything* on `QTreeView::branch` makes Qt hand branch
    painting over to the stylesheet, which then draws nothing unless an
    `image` is supplied — so the innocuous-looking
    `QTreeView::branch { background: transparent }` that killed the box
    hover also silently removed every expand arrow. The rules below put
    them back in the deck list's idiom: a hairline chevron, right when
    closed and down when open, ink-faint at rest and ink-dim under the
    cursor, with no box behind it.

    Qt splits "has children" across two selectors depending on whether the
    row also has siblings, and both have to be spelled out."""
    rest_r = _tinted_chevron("chevron-right.svg", p["ink_faint"])
    rest_d = _tinted_chevron("chevron-down.svg", p["ink_faint"])
    hot_r = _tinted_chevron("chevron-right.svg", p["ink_dim"])
    hot_d = _tinted_chevron("chevron-down.svg", p["ink_dim"])
    return f"""
QTreeView::branch {{
    background: transparent;
    border-image: none;
    image: none;
}}
/* Background only — never `image`, or these would out-rank nothing but
   would still need re-stating below. Keeps the gutter clear of the
   selection and hover fills. */
QTreeView::branch:selected, QTreeView::branch:hover,
QTreeView::branch:selected:active, QTreeView::branch:selected:!active {{
    background: transparent;
}}
QTreeView::branch:has-children:!has-siblings:closed,
QTreeView::branch:closed:has-children:has-siblings {{
    border-image: none;
    image: url({rest_r});
}}
QTreeView::branch:open:has-children:!has-siblings,
QTreeView::branch:open:has-children:has-siblings {{
    border-image: none;
    image: url({rest_d});
}}
QTreeView::branch:has-children:!has-siblings:closed:hover,
QTreeView::branch:closed:has-children:has-siblings:hover {{
    image: url({hot_r});
}}
QTreeView::branch:open:has-children:!has-siblings:hover,
QTreeView::branch:open:has-children:has-siblings:hover {{
    image: url({hot_d});
}}
"""


# --------------------------------------------------------------------------- #
# Application
# --------------------------------------------------------------------------- #
def _upper_header(model: Any) -> None:
    """Uppercase the column labels. QSS has no `text-transform`, and the
    labels come from `column_label()` deep in the backend, so the cheapest
    honest place to do it is the model's headerData — patched on the
    instance, so nothing else in Anki sees it."""
    if getattr(model, "_ba_header_patched", False):
        return
    orig = model.headerData

    def patched(section: int, orientation: Any, role: int = 0) -> Any:
        out = orig(section, orientation, role)
        if (
            isinstance(out, str)
            and orientation == Qt.Orientation.Horizontal
            and role == Qt.ItemDataRole.DisplayRole
        ):
            return out.upper()
        return out

    model.headerData = patched  # type: ignore[assignment]
    model._ba_header_patched = True


def _tabular(font: QFont) -> QFont:
    """Ask for tabular figures so Due / interval columns line up. Qt 6.7+
    only; silently skipped on older builds (the columns just read a hair
    less tidily)."""
    try:
        font.setFeature("tnum", 1)  # type: ignore[attr-defined]
    except Exception:
        pass
    return font


def _style_table(browser: Any, p: Dict[str, str]) -> None:
    view = browser.form.tableView
    view.setShowGrid(False)
    view.setAlternatingRowColors(False)
    view.setFrameShape(view.Shape.NoFrame)
    view.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)

    font = QFont(view.font())
    font.setPointSizeF(TABLE_PT)
    view.setFont(_tabular(font))

    vh = view.verticalHeader()
    if vh is not None:
        # Anki sizes rows off the tallest note-type line height; we add a
        # little more air so the rows breathe like the deck list. Remember
        # Anki's own figure — `apply()` re-runs on every theme change and
        # the padding would otherwise compound each time.
        base = getattr(view, "_ba_row_base", None)
        if base is None:
            base = vh.defaultSectionSize()
            view._ba_row_base = base  # type: ignore[attr-defined]
        vh.setDefaultSectionSize(base + ROW_HEIGHT_PAD)

    hh = view.horizontalHeader()
    if hh is not None:
        hf = QFont(hh.font())
        hf.setPointSizeF(HEADER_PT)
        hf.setWeight(QFont.Weight.DemiBold)
        hh.setFont(hf)
        hh.setHighlightSections(False)
        hh.setDefaultAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
        )
        hh.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        hh.setStretchLastSection(True)

    model = view.model()
    if model is not None:
        _upper_header(model)


class _SectionDelegate(QStyledItemDelegate):
    """Renders the sidebar's section roots (Decks, Tags, …) as *labels*
    rather than as rows you could click.

    "Extremely functional, but still able to separate into groups" — the
    groups need to read as groups. Everywhere else in Anki+ a group label
    is small, uppercase, letterspaced and faint (DUE / NEW / LEARNING in
    the rail; TEXT / EXTRA in the editor). The sidebar gets the same
    treatment, and loses its folder/tag icon while it's at it: a heading
    doesn't need a picture of itself."""

    def __init__(self, tree: Any, faint: str) -> None:
        super().__init__(tree)
        self._tree = tree
        self._faint = faint
        try:
            from aqt.browser.sidebar.item import SidebarItemType

            self._roots = set(SidebarItemType.section_roots())
        except Exception:
            self._roots = set()

    def _is_section(self, index: Any) -> bool:
        if not self._roots:
            return False
        try:
            item = self._tree.model().item_for_index(index)
        except Exception:
            return False
        return bool(item is not None and item.item_type in self._roots)

    def initStyleOption(self, option: Any, index: Any) -> None:
        super().initStyleOption(option, index)
        if not self._is_section(index):
            return
        try:
            option.text = (option.text or "").upper()
            font = QFont(option.font)
            font.setPointSizeF(max(7.5, font.pointSizeF() - 2.0))
            font.setWeight(QFont.Weight.DemiBold)
            font.setLetterSpacing(QFont.SpacingType.PercentageSpacing, 112)
            option.font = font
            option.icon = QIcon()
            option.features &= ~option.ViewItemFeature.HasDecoration
            from aqt.qt import QColor, QPalette

            col = QColor(self._faint)
            for group in (
                QPalette.ColorGroup.Active,
                QPalette.ColorGroup.Inactive,
                QPalette.ColorGroup.Disabled,
            ):
                option.palette.setColor(group, QPalette.ColorRole.Text, col)
                option.palette.setColor(
                    group, QPalette.ColorRole.HighlightedText, col
                )
        except Exception:
            pass


def _patch_sidebar_style(browser: Any, p: Dict[str, str], is_dark: bool,
                         accent: str) -> None:
    sidebar = getattr(browser, "sidebar", None)
    if sidebar is None:
        return

    def _setup_style(_self=sidebar) -> None:
        try:
            pal, dark = _palette()
            _self.setStyleSheet(tree_qss(pal, dark, _accent()))
        except Exception:
            pass

    # Anki registered its own bound `_setup_style` with theme_did_change in
    # SidebarTreeView.__init__; rebinding the attribute doesn't unregister
    # that. Swapping the hook entry keeps `cleanup()`'s `remove()` working
    # (it removes by the attribute's *current* value) while making the
    # theme-change path call ours.
    #
    # `apply()` re-runs on every theme change, so this must be idempotent —
    # otherwise the hook list grows a closure per flip.
    if not getattr(sidebar, "_ba_style_patched", False):
        try:
            old = sidebar._setup_style
            hook = gui_hooks.theme_did_change
            if old in hook._hooks:  # type: ignore[attr-defined]
                hook.remove(old)
            sidebar._setup_style = _setup_style  # type: ignore[assignment]
            hook.append(_setup_style)
        except Exception:
            sidebar._setup_style = _setup_style  # type: ignore[assignment]
        sidebar._ba_style_patched = True  # type: ignore[attr-defined]

    _setup_style()
    try:
        # 18px leaves a 3px gutter either side of the 12px chevron; at
        # Anki's 15 the arrow crowds the label it belongs to.
        sidebar.setIndentation(18)
        sidebar.setAnimated(True)
        sidebar.setRootIsDecorated(True)
    except Exception:
        pass
    try:
        delegate = _SectionDelegate(sidebar, p["ink_faint"])
        sidebar.setItemDelegate(delegate)
        # Qt does not own the delegate (setItemDelegate takes no ownership);
        # park it on the tree so it isn't garbage-collected out from under
        # the view.
        sidebar._ba_delegate = delegate  # type: ignore[attr-defined]
    except Exception:
        pass


def apply(browser: Any) -> None:
    """Paint one Browser instance. Safe to call more than once."""
    if not enabled():
        return
    p, is_dark = _palette()
    accent = _accent()

    try:
        browser.form.centralwidget.setStyleSheet(central_qss(p, is_dark, accent))
    except Exception:
        pass
    try:
        # `setupSidebar` wraps the tree + filter + toolbar in a plain
        # QWidget and hands it to the dock; that wrapper is what gets
        # reparented into the embed, so it's where the sheet belongs.
        #
        # Once `browse_embed` has lifted it out, `dock.widget()` is None —
        # so remember the wrapper the first time we see it, or a later
        # theme flip would repaint the table and leave the sidebar's
        # filter field wearing the old palette.
        dock = getattr(browser, "sidebarDockWidget", None)
        inner = getattr(browser, "_ba_sidebar_container", None)
        if inner is None and dock is not None:
            inner = dock.widget()
            if inner is not None:
                browser._ba_sidebar_container = inner
        qss = sidebar_qss(p, is_dark, accent)
        if dock is not None:
            dock.setStyleSheet(qss)
        if inner is not None:
            inner.setStyleSheet(qss)
    except Exception:
        pass
    try:
        _style_table(browser, p)
    except Exception:
        pass
    try:
        _patch_sidebar_style(browser, p, is_dark, accent)
    except Exception:
        pass
    try:
        # Standalone (non-embedded) window: paint the shell too, so
        # `embed_browse: false` still lands in the design language.
        browser.setStyleSheet(
            f"QMainWindow {{ background: {p['paper']}; }}"
        )
    except Exception:
        pass


def current_browser() -> Optional[Any]:
    """The live Browser, embedded or standalone."""
    try:
        from . import browse_embed

        br = browse_embed._state.get("browser")
        if br is not None:
            return br
    except Exception:
        pass
    try:
        import aqt

        return aqt.dialogs._dialogs["Browser"][1]  # type: ignore[index]
    except Exception:
        return None


def on_theme_did_change() -> None:
    """Anki rebuilds its application stylesheet on a theme flip; our
    widget-level sheets survive but hold the old palette. Repaint."""
    try:
        from . import browse_embed

        browse_embed.refresh_palette()
    except Exception:
        pass
    br = current_browser()
    if br is None:
        return
    try:
        apply(br)
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# Sidebar trim
# --------------------------------------------------------------------------- #
# Stock Anki opens the sidebar with seven top-level sections and ~20 rows
# before you've touched anything: Saved Searches, Today (7 canned
# searches), Flags, Card State, Decks, Note Types, Tags. Only two of those
# are *groupings* of your collection — Decks and Tags. The rest are
# saved queries wearing a tree's clothes, and they push the two real axes
# below the fold.
#
# So: keep Decks + Tags, drop the rest. Nothing is lost functionally —
# every suppressed row is a search you can still type (`flag:1`,
# `is:due`, `added:1`, `note:Basic`), and the menu actions that create
# saved searches keep working; they just don't render a row.
_TRIMMED_STAGES = ("SAVED_SEARCHES", "TODAY", "FLAGS", "CARD_STATE", "NOTETYPES")


def sidebar_minimal() -> bool:
    return bool(_config().get("browse_sidebar_minimal", True))


def on_browser_will_build_tree(
    handled: bool, tree: Any, stage: Any, browser: Any
) -> bool:
    """Return True to tell Anki we've "handled" (i.e. skipped) a stage."""
    if handled or not sidebar_minimal():
        return handled
    try:
        if getattr(stage, "name", "") in _TRIMMED_STAGES:
            return True
    except Exception:
        pass
    return handled
