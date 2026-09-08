# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""One-click re-study of a deck's cards, ranked hardest-first.

Offers two modes on a bulk-selectable card list:
- Cram now: builds a reschedule=off filtered deck ("Restudy: <deck>") so the
  cards can be studied immediately without touching their real due dates.
- Start over: routes the selection through the existing Forget dialog.
"""

from __future__ import annotations

from dataclasses import dataclass

import aqt
from anki.cards import Card, CardId
from anki.collection import Collection
from anki.consts import QUEUE_TYPE_REV
from anki.decks import DeckId, FilteredDeckConfig
from anki.scheduler import FilteredDeckForUpdate
from anki.scheduler.base import ScheduleCardsAsNew
from anki.utils import strip_html
from aqt.browser.previewer import Previewer
from aqt.operations import QueryOp
from aqt.operations.scheduling import add_or_update_filtered_deck, forget_cards
from aqt.qt import *
from aqt.utils import disable_help_button, showWarning, tooltip, tr

_EXCERPT_LEN = 60

_COL_CHECK = 0
_COL_CARD = 1
_COL_TYPE = 2
_COL_DIFFICULTY = 3
_COL_LAPSES = 4
_COL_MISSED = 5
_COL_DUE = 6


@dataclass
class RestudyRow:
    card_id: CardId
    excerpt: str
    type_name: str
    difficulty: float | None
    lapses: int
    missed_recently: bool
    due_in_days: int | None


@dataclass
class RestudyData:
    deck_id: DeckId
    deck_name: str
    rows: list[RestudyRow]


def _gather_rows(col: Collection, deck_id: DeckId) -> RestudyData:
    deck = col.decks.get(deck_id)
    assert deck is not None
    name = deck["name"]
    escaped = name.replace("\\", "\\\\").replace('"', '\\"')
    card_ids = col.find_cards(f'deck:"{escaped}" -is:suspended')
    missed = set(col.find_cards(f'deck:"{escaped}" rated:7:1'))
    today = col.sched.today

    rows = []
    notetypes: dict[int, dict] = {}
    for cid in card_ids:
        card = col.get_card(cid)
        note = card.note()
        notetype = notetypes.get(note.mid)
        if notetype is None:
            notetype = note.note_type()
            assert notetype is not None
            notetypes[note.mid] = notetype
        excerpt = strip_html(note.fields[notetype["sortf"]]).strip()
        if not excerpt:
            excerpt = strip_html(note.fields[0]).strip()
        if len(excerpt) > _EXCERPT_LEN:
            excerpt = excerpt[: _EXCERPT_LEN - 1] + "…"
        templates = notetype["tmpls"]
        if card.ord < len(templates):
            type_name = templates[card.ord]["name"]
        else:
            # cloze: one template serves every ordinal
            type_name = templates[0]["name"] if templates else ""
        if card.memory_state:
            # stored difficulty is raw 1-10; normalize to 0-1 like the
            # browser's Difficulty column (FsrsMemoryState::difficulty())
            difficulty = (card.memory_state.difficulty - 1.0) / 9.0
        else:
            difficulty = None
        due_in_days = card.due - today if card.queue == QUEUE_TYPE_REV else None
        rows.append(
            RestudyRow(
                card_id=cid,
                excerpt=excerpt,
                type_name=type_name,
                difficulty=difficulty,
                lapses=card.lapses,
                missed_recently=cid in missed,
                due_in_days=due_in_days,
            )
        )

    rows.sort(key=lambda r: (r.difficulty is None, -(r.difficulty or 0.0), -r.lapses))
    return RestudyData(deck_id=deck_id, deck_name=name, rows=rows)


class RestudyDialog(QDialog):
    @staticmethod
    def fetch_and_show(mw: aqt.AnkiQt, deck_id: DeckId) -> None:
        QueryOp(
            parent=mw,
            op=lambda col: _gather_rows(col, deck_id),
            success=lambda data: RestudyDialog(mw, data),
        ).with_progress().run_in_background()

    def __init__(self, mw: aqt.AnkiQt, data: RestudyData) -> None:
        "Don't call this directly; use RestudyDialog.fetch_and_show()."
        QDialog.__init__(self, mw)
        self.mw = mw
        self.data = data
        disable_help_button(self)
        self.setWindowTitle(tr.studying_restudy_title(deck=data.deck_name))
        self.setMinimumSize(640, 480)
        self._build_ui()
        self._select(lambda row: True)
        self.open()

    # UI
    ##########################################################################

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)

        quick = QHBoxLayout()
        for label, predicate in (
            (tr.studying_restudy_select_all(), lambda r: True),
            (
                tr.studying_restudy_select_missed(),
                lambda r: r.missed_recently,
            ),
            (
                tr.studying_restudy_select_hardest(),
                self._hardest_quartile_predicate(),
            ),
            (
                tr.studying_restudy_select_due_soon(),
                lambda r: r.due_in_days is not None and r.due_in_days <= 3,
            ),
        ):
            button = QPushButton(label)
            qconnect(button.clicked, lambda _=False, p=predicate: self._select(p))
            quick.addWidget(button)
        quick.addStretch()
        layout.addLayout(quick)

        # Master select-all/deselect-all, top-left of the table. The square
        # mirrors the table (unchecked/partial/checked) and toggles all/none;
        # the word next to it is a menu button offering per-card-type
        # selection. Driven from `clicked` (user gestures only), so
        # programmatic setCheckState() can't recurse into it.
        master_row = QHBoxLayout()
        master_row.setSpacing(4)
        self.master_check = QCheckBox()
        self.master_check.setTristate(False)
        self.master_check.setToolTip(tr.studying_restudy_master_select())
        qconnect(self.master_check.clicked, self._on_master_clicked)
        master_row.addWidget(self.master_check)
        self.type_button = QToolButton()
        self.type_button.setText(tr.studying_restudy_master_select() + " \u25be")
        self.type_button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        self.type_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        # the text carries its own arrow; hide the style's duplicate
        self.type_button.setStyleSheet(
            "QToolButton { border: none; } QToolButton::menu-indicator { image: none; }"
        )
        self._type_menu = QMenu(self)
        qconnect(self._type_menu.aboutToShow, self._rebuild_type_menu)
        self.type_button.setMenu(self._type_menu)
        master_row.addWidget(self.type_button)
        master_row.addStretch()
        layout.addLayout(master_row)

        headers = [
            "",
            tr.studying_restudy_card(),
            tr.card_stats_card_template(),
            tr.studying_restudy_difficulty(),
            tr.studying_restudy_lapses(),
            tr.studying_restudy_missed(),
            tr.studying_restudy_due_in(),
        ]
        self.table = QTableWidget(len(self.data.rows), len(headers), self)
        self.table.setHorizontalHeaderLabels(headers)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self._view_rows: list[RestudyRow] = list(self.data.rows)
        header = self.table.horizontalHeader()
        assert header is not None
        header.setSectionResizeMode(_COL_CARD, QHeaderView.ResizeMode.Stretch)
        # Sorting is done manually (header.sectionClicked -> _apply_sort)
        # rather than via setSortingEnabled(): a plain __lt__ can't keep
        # value-less rows ("-") at the bottom for both directions, and
        # repopulating by card id keeps check states with their cards.
        header.setSectionsClickable(True)
        header.setSortIndicatorShown(True)
        header.setSortIndicator(_COL_DIFFICULTY, Qt.SortOrder.DescendingOrder)
        self._sort_column = _COL_DIFFICULTY
        self._sort_order = Qt.SortOrder.DescendingOrder
        qconnect(header.sectionClicked, self._on_header_clicked)
        self._populate(self._view_rows, checked=set())
        qconnect(self.table.itemChanged, self._on_item_changed)
        qconnect(self.table.cellClicked, self._on_cell_clicked)
        qconnect(self.table.cellDoubleClicked, self._on_cell_double_clicked)
        # Drag-select: a press on the checkbox column starts painting that
        # row's toggled state across every row the cursor passes, with
        # timer-driven auto-scroll past the viewport edges.
        self._drag_target: Qt.CheckState | None = None
        self._drag_last_row: int | None = None
        self._autoscroll_timer = QTimer(self)
        self._autoscroll_timer.setInterval(60)
        qconnect(self._autoscroll_timer.timeout, self._drag_autoscroll_tick)
        viewport = self.table.viewport()
        assert viewport is not None
        viewport.installEventFilter(self)
        layout.addWidget(self.table)

        self.selected_label = QLabel()
        layout.addWidget(self.selected_label)

        self.cram_radio = QRadioButton(tr.studying_restudy_cram())
        self.reset_radio = QRadioButton(tr.studying_restudy_reset())
        self.cram_radio.setChecked(True)
        modes = QHBoxLayout()
        modes.addWidget(self.cram_radio)
        modes.addWidget(self.reset_radio)
        modes.addStretch()
        layout.addLayout(modes)

        self.button_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        qconnect(self.button_box.accepted, self._on_accept)
        qconnect(self.button_box.rejected, self.reject)
        layout.addWidget(self.button_box)

    def _populate(self, rows: list[RestudyRow], checked: set[CardId]) -> None:
        "Fill the table with `rows`, restoring check state by card id."
        self.table.blockSignals(True)
        self.table.setRowCount(len(rows))
        self._view_rows = rows
        for i, row in enumerate(rows):
            check = QTableWidgetItem()
            check.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled)
            check.setCheckState(
                Qt.CheckState.Checked
                if row.card_id in checked
                else Qt.CheckState.Unchecked
            )
            difficulty = (
                f"{row.difficulty * 100:.0f}%" if row.difficulty is not None else "-"
            )
            due = str(row.due_in_days) if row.due_in_days is not None else "-"
            missed = "✓" if row.missed_recently else ""
            excerpt_item = QTableWidgetItem(row.excerpt)
            link_font = excerpt_item.font()
            link_font.setUnderline(True)
            excerpt_item.setFont(link_font)
            excerpt_item.setForeground(self.palette().color(QPalette.ColorRole.Link))
            excerpt_item.setToolTip(tr.actions_preview())
            for col_idx, item in enumerate(
                [
                    check,
                    excerpt_item,
                    QTableWidgetItem(row.type_name),
                    QTableWidgetItem(difficulty),
                    QTableWidgetItem(str(row.lapses)),
                    QTableWidgetItem(missed),
                    QTableWidgetItem(due),
                ]
            ):
                self.table.setItem(i, col_idx, item)
        self.table.blockSignals(False)
        self._refresh_selected_label()

    def _sort_key(self, column: int):
        "Raw per-row sort value for `column`; None means no value."
        checked = set(self._selected_ids())
        return {
            _COL_CHECK: lambda r: r.card_id in checked,
            _COL_CARD: lambda r: r.excerpt.casefold(),
            _COL_TYPE: lambda r: r.type_name.casefold(),
            _COL_DIFFICULTY: lambda r: r.difficulty,
            _COL_LAPSES: lambda r: r.lapses,
            _COL_MISSED: lambda r: r.missed_recently,
            _COL_DUE: lambda r: r.due_in_days,
        }[column]

    def _on_header_clicked(self, column: int) -> None:
        if column == self._sort_column:
            self._sort_order = (
                Qt.SortOrder.DescendingOrder
                if self._sort_order == Qt.SortOrder.AscendingOrder
                else Qt.SortOrder.AscendingOrder
            )
        else:
            self._sort_column = column
            self._sort_order = Qt.SortOrder.AscendingOrder
        header = self.table.horizontalHeader()
        assert header is not None
        header.setSortIndicator(self._sort_column, self._sort_order)
        self._apply_sort()

    def _apply_sort(self) -> None:
        key = self._sort_key(self._sort_column)
        checked = set(self._selected_ids())
        known = [r for r in self._view_rows if key(r) is not None]
        missing = [r for r in self._view_rows if key(r) is None]
        known.sort(
            key=key,
            reverse=self._sort_order == Qt.SortOrder.DescendingOrder,
        )
        # rows with no value in this column sort last in both directions
        self._populate(known + missing, checked)

    def _hardest_quartile_predicate(self):
        known = sorted(
            (r.difficulty for r in self.data.rows if r.difficulty is not None),
            reverse=True,
        )
        if not known:
            return lambda r: False
        cutoff = known[max(0, (len(known) + 3) // 4 - 1)]
        return lambda r: r.difficulty is not None and r.difficulty >= cutoff

    def _select(self, predicate) -> None:
        for i, row in enumerate(self._view_rows):
            item = self.table.item(i, 0)
            assert item is not None
            item.setCheckState(
                Qt.CheckState.Checked if predicate(row) else Qt.CheckState.Unchecked
            )

    def _selected_ids(self) -> list[CardId]:
        ids = []
        for i, row in enumerate(self._view_rows):
            item = self.table.item(i, 0)
            assert item is not None
            if item.checkState() == Qt.CheckState.Checked:
                ids.append(row.card_id)
        return ids

    def _on_item_changed(self, _item: QTableWidgetItem) -> None:
        self._refresh_selected_label()

    def _on_master_clicked(self, _checked: bool = False) -> None:
        # Decide from the table's state, not the box's (Qt has already
        # toggled the box by the time `clicked` fires).
        select_all = len(self._selected_ids()) < len(self._view_rows)
        self._select(lambda row: select_all)

    def _refresh_selected_label(self) -> None:
        if not hasattr(self, "button_box"):
            # the initial _populate runs before the label/buttons exist;
            # __init__'s _select() refreshes once the ui is complete
            return
        count = len(self._selected_ids())
        total = len(self.data.rows)
        self.selected_label.setText(
            tr.studying_restudy_selected(selected=count, total=total)
        )
        ok = self.button_box.button(QDialogButtonBox.StandardButton.Ok)
        assert ok is not None
        ok.setEnabled(count > 0)
        if count == 0:
            state = Qt.CheckState.Unchecked
        elif count == total:
            state = Qt.CheckState.Checked
        else:
            state = Qt.CheckState.PartiallyChecked
        self.master_check.blockSignals(True)
        self.master_check.setCheckState(state)
        self.master_check.blockSignals(False)

    def _rebuild_type_menu(self) -> None:
        "One action per distinct card type; selecting replaces the selection."
        self._type_menu.clear()
        counts: dict[str, int] = {}
        for row in self.data.rows:
            counts[row.type_name] = counts.get(row.type_name, 0) + 1
        for name in sorted(counts, key=str.casefold):
            action = self._type_menu.addAction(f"{name} ({counts[name]})")
            assert action is not None
            qconnect(
                action.triggered,
                lambda _=False, t=name: self._select(lambda r: r.type_name == t),
            )
        self._type_menu.addSeparator()
        invert = self._type_menu.addAction(tr.studying_restudy_invert())
        assert invert is not None
        qconnect(invert.triggered, self._invert_selection)

    def _invert_selection(self, _checked: bool = False) -> None:
        checked = set(self._selected_ids())
        self._select(lambda r: r.card_id not in checked)

    # Drag-select
    ##########################################################################

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:
        if obj is self.table.viewport():
            etype = event.type()
            if (
                etype == QEvent.Type.MouseButtonPress
                and isinstance(event, QMouseEvent)
                and event.button() == Qt.MouseButton.LeftButton
            ):
                y = int(event.position().y())
                row = self.table.rowAt(y)
                if (
                    row >= 0
                    and self.table.columnAt(int(event.position().x())) == _COL_CHECK
                ):
                    item = self.table.item(row, _COL_CHECK)
                    assert item is not None
                    self._drag_target = (
                        Qt.CheckState.Unchecked
                        if item.checkState() == Qt.CheckState.Checked
                        else Qt.CheckState.Checked
                    )
                    self._drag_last_row = row
                    item.setCheckState(self._drag_target)
                    self._autoscroll_timer.start()
                    # consume so Qt's own indicator handling can't re-toggle
                    return True
            elif etype == QEvent.Type.MouseMove and self._drag_target is not None:
                if isinstance(event, QMouseEvent):
                    self._drag_apply_at(int(event.position().y()))
                return True
            elif (
                etype == QEvent.Type.MouseButtonRelease
                and self._drag_target is not None
            ):
                self._end_drag()
                return True
            elif etype == QEvent.Type.Wheel and self._drag_target is not None:
                # let the table scroll, then paint the row now under the cursor
                QTimer.singleShot(0, self._drag_apply_under_cursor)
                return False
        return super().eventFilter(obj, event)

    def _drag_apply_at(self, y: int) -> None:
        "Apply the drag state to every row between the last painted row and y."
        if self._drag_target is None:
            return
        viewport = self.table.viewport()
        assert viewport is not None
        y = max(0, min(y, viewport.height() - 1))
        row = self.table.rowAt(y)
        if row < 0 or self._drag_last_row is None:
            return
        step = 1 if row >= self._drag_last_row else -1
        for i in range(self._drag_last_row, row + step, step):
            item = self.table.item(i, _COL_CHECK)
            assert item is not None
            item.setCheckState(self._drag_target)
        self._drag_last_row = row

    def _drag_apply_under_cursor(self) -> None:
        if self._drag_target is None:
            return
        viewport = self.table.viewport()
        assert viewport is not None
        self._drag_apply_at(viewport.mapFromGlobal(QCursor.pos()).y())

    def _drag_autoscroll_tick(self) -> None:
        "While dragging past an edge, scroll a row and keep painting."
        if self._drag_target is None:
            return
        viewport = self.table.viewport()
        assert viewport is not None
        pos_y = viewport.mapFromGlobal(QCursor.pos()).y()
        scrollbar = self.table.verticalScrollBar()
        assert scrollbar is not None
        if pos_y < 0:
            scrollbar.setValue(scrollbar.value() - 1)
        elif pos_y > viewport.height():
            scrollbar.setValue(scrollbar.value() + 1)
        self._drag_apply_at(pos_y)

    def _end_drag(self) -> None:
        self._drag_target = None
        self._drag_last_row = None
        self._autoscroll_timer.stop()

    def hideEvent(self, event: QHideEvent) -> None:
        # safety: never leave a drag armed if the dialog goes away mid-drag
        self._end_drag()
        super().hideEvent(event)

    def _on_cell_clicked(self, row_idx: int, col_idx: int) -> None:
        if col_idx == 1:
            self._preview_row(row_idx)

    def _on_cell_double_clicked(self, row_idx: int, _col_idx: int) -> None:
        self._preview_row(row_idx)

    def _preview_row(self, row_idx: int) -> None:
        card = self.mw.col.get_card(self._view_rows[row_idx].card_id)
        _SingleCardPreviewer(card=card, mw=self.mw).open()

    # Actions
    ##########################################################################

    def _on_accept(self) -> None:
        if self.cram_radio.isChecked():
            self._build_cram()
        else:
            self._full_reset()

    def _build_cram(self) -> None:
        selected = self._selected_ids()
        if not selected:
            return
        name = f"Restudy: {self.data.deck_name}"
        existing = self.mw.col.decks.id_for_name(name)
        if existing:
            deck = self.mw.col.decks.get(existing)
            if deck and not deck["dyn"]:
                showWarning(tr.studying_restudy_must_rename(), parent=self)
                return
        target = DeckId(existing or 0)
        search = "cid:" + ",".join(str(cid) for cid in selected)

        def on_fetched(update: FilteredDeckForUpdate) -> None:
            update.name = name
            config = update.config
            config.reschedule = False
            del config.search_terms[:]
            config.search_terms.append(
                FilteredDeckConfig.SearchTerm(
                    search=search,
                    limit=99_999,
                    order=FilteredDeckConfig.SearchTerm.Order.RETRIEVABILITY_ASCENDING,
                )
            )
            config.preview_again_secs = 60
            config.preview_hard_secs = 600
            config.preview_good_secs = 0
            update.allow_empty = False

            def on_success(_changes) -> None:
                tooltip(tr.studying_restudy_created(), parent=self.mw)
                self.mw.moveToState("review")

            add_or_update_filtered_deck(parent=self.mw, deck=update).success(
                on_success
            ).run_in_background()
            self.accept()

        QueryOp(
            parent=self,
            op=lambda col: col.sched.get_or_create_filtered_deck(deck_id=target),
            success=on_fetched,
        ).run_in_background()

    def _full_reset(self) -> None:
        selected = self._selected_ids()
        if not selected:
            return
        if op := forget_cards(
            parent=self,
            card_ids=selected,
            context=ScheduleCardsAsNew.Context.BROWSER,
        ):
            op.run_in_background()
            self.accept()


class _SingleCardPreviewer(Previewer):
    """Read-only question/answer preview of one fixed card."""

    def __init__(self, card: Card, mw: aqt.AnkiQt) -> None:
        super().__init__(parent=None, mw=mw, on_close=lambda: None)
        self._card = card

    def card(self) -> Card | None:
        return self._card

    def card_changed(self) -> bool:
        return False
