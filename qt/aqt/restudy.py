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
from anki.cards import CardId
from anki.collection import Collection
from anki.consts import QUEUE_TYPE_REV
from anki.decks import DeckId, FilteredDeckConfig
from anki.scheduler import FilteredDeckForUpdate
from anki.scheduler.base import ScheduleCardsAsNew
from anki.utils import strip_html
from aqt.operations import QueryOp
from aqt.operations.scheduling import add_or_update_filtered_deck, forget_cards
from aqt.qt import *
from aqt.utils import disable_help_button, showWarning, tooltip, tr

_EXCERPT_LEN = 60


@dataclass
class RestudyRow:
    card_id: CardId
    excerpt: str
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
    for cid in card_ids:
        card = col.get_card(cid)
        note = card.note()
        notetype = note.note_type()
        assert notetype is not None
        excerpt = strip_html(note.fields[notetype["sortf"]]).strip()
        if not excerpt:
            excerpt = strip_html(note.fields[0]).strip()
        if len(excerpt) > _EXCERPT_LEN:
            excerpt = excerpt[: _EXCERPT_LEN - 1] + "…"
        difficulty = card.memory_state.difficulty if card.memory_state else None
        due_in_days = card.due - today if card.queue == QUEUE_TYPE_REV else None
        rows.append(
            RestudyRow(
                card_id=cid,
                excerpt=excerpt,
                difficulty=difficulty,
                lapses=card.lapses,
                missed_recently=cid in missed,
                due_in_days=due_in_days,
            )
        )

    rows.sort(
        key=lambda r: (r.difficulty is None, -(r.difficulty or 0.0), -r.lapses)
    )
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

        headers = [
            "",
            tr.studying_restudy_card(),
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
        for i, row in enumerate(self.data.rows):
            check = QTableWidgetItem()
            check.setFlags(
                Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled
            )
            check.setCheckState(Qt.CheckState.Unchecked)
            difficulty = (
                f"{row.difficulty * 100:.0f}%" if row.difficulty is not None else "-"
            )
            due = str(row.due_in_days) if row.due_in_days is not None else "-"
            missed = "✓" if row.missed_recently else ""
            for col_idx, item in enumerate(
                [
                    check,
                    QTableWidgetItem(row.excerpt),
                    QTableWidgetItem(difficulty),
                    QTableWidgetItem(str(row.lapses)),
                    QTableWidgetItem(missed),
                    QTableWidgetItem(due),
                ]
            ):
                self.table.setItem(i, col_idx, item)
        header = self.table.horizontalHeader()
        assert header is not None
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        qconnect(self.table.itemChanged, self._on_item_changed)
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
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        qconnect(self.button_box.accepted, self._on_accept)
        qconnect(self.button_box.rejected, self.reject)
        layout.addWidget(self.button_box)

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
        for i, row in enumerate(self.data.rows):
            item = self.table.item(i, 0)
            assert item is not None
            item.setCheckState(
                Qt.CheckState.Checked if predicate(row) else Qt.CheckState.Unchecked
            )

    def _selected_ids(self) -> list[CardId]:
        ids = []
        for i, row in enumerate(self.data.rows):
            item = self.table.item(i, 0)
            assert item is not None
            if item.checkState() == Qt.CheckState.Checked:
                ids.append(row.card_id)
        return ids

    def _on_item_changed(self, _item: QTableWidgetItem) -> None:
        count = len(self._selected_ids())
        self.selected_label.setText(
            tr.studying_restudy_selected(
                selected=count, total=len(self.data.rows)
            )
        )
        ok = self.button_box.button(QDialogButtonBox.StandardButton.Ok)
        assert ok is not None
        ok.setEnabled(count > 0)

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
