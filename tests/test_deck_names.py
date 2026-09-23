"""Regression tests for deck_names.py: a deck is named by its title, not by
its `::` path or by the search that finds it.

Reported as: "The names of decks, as seen in the top search bar and in the
deck column, shouldn't have :: or quotations, it should just be the clean
title of the deck." Clicking a deck in the Browse sidebar put
`"deck:Fundamentals of Biochemistry::Amino Acids"` in the search box, and
the Deck column repeated `Fundamentals of Biochemistry::Amino Acids` on
every row.

Runs standalone (no Anki or PyQt install needed):
`python3 tests/test_deck_names.py`. `aqt` is stubbed with a small fake of
the pieces the module touches: the Browser's search box (an editable
combobox), its `search_for` / `current_search` / `update_history` trio
with stock Anki's semantics, and a table model.
"""

import json
import os
import sys
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


# --------------------------------------------------------------------------- #
# Fake Qt
# --------------------------------------------------------------------------- #

class _ItemDataRole:
    DisplayRole = 0
    ToolTipRole = 3
    FontRole = 6


class Qt:
    ItemDataRole = _ItemDataRole


class _Signal:
    def __init__(self):
        self._slots = []

    def connect(self, fn):
        self._slots.append(fn)

    def emit(self, *a):
        for fn in list(self._slots):
            fn(*a)


class FakeLineEdit:
    def __init__(self):
        self._text = ""
        self._tip = ""
        self.textEdited = _Signal()

    def text(self):
        return self._text

    def setText(self, t):
        self._text = t

    def setToolTip(self, t):
        self._tip = t

    def toolTip(self):
        return self._tip

    # test driver: the user types
    def type(self, t):
        self._text = t
        self.textEdited.emit(t)


class FakeSearchEdit:
    """Editable QComboBox. `addItems` on an empty box makes the first item
    current, which writes it into the line edit: that is how stock Anki's
    box still shows the search after `update_history` rebuilds the list."""

    def __init__(self):
        self._line = FakeLineEdit()
        self.items = []

    def lineEdit(self):
        return self._line

    def setCurrentIndex(self, i):
        if i < 0:
            self._line.setText("")

    def clear(self):
        self.items = []

    def addItems(self, items):
        was_empty = not self.items
        self.items.extend(items)
        if was_empty and self.items:
            self._line.setText(self.items[0])

    def setEditText(self, t):
        self._line.setText(t)


class _Index:
    def __init__(self, row, col):
        self._r, self._c = row, col

    def row(self):
        return self._r

    def column(self):
        return self._c

    def isValid(self):
        return True


class FakeModel:
    """Only what `DataModel` offers to a cell painter: `data()` for the
    display and tooltip roles, and the column key for a section."""

    def __init__(self, columns, rows):
        self.active_columns = list(columns)
        self.rows = rows

    def column_key_at(self, section):
        return self.active_columns[section]

    def index(self, r, c):
        return _Index(r, c)

    def data(self, index, role=0):
        if role in (Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.ToolTipRole):
            return self.rows[index.row()][index.column()]
        return None


class FakeTable:
    def __init__(self, model):
        self._model = model
        self.searches = []

    def search(self, txt):
        self.searches.append(txt)


class FakeCol:
    def build_search_string(self, text):
        # Stock normalisation of an already-normalised deck search, or of
        # plain words, is the identity; that is all these tests need.
        return text


class FakeBrowser:
    """`search_for` / `current_search` / `search` / `onSearchActivated` /
    `update_history` as aqt.browser.browser.Browser implements them."""

    def __init__(self, model):
        self.form = types.SimpleNamespace(searchEdit=FakeSearchEdit())
        self.table = FakeTable(model)
        self.col = FakeCol()
        self._lastSearchTxt = ""
        self.history = []

    def search_for(self, search, prompt=None):
        self._lastSearchTxt = search
        prompt = search if prompt is None else prompt
        self.form.searchEdit.setCurrentIndex(-1)
        self.form.searchEdit.lineEdit().setText(prompt)
        self.search()

    def current_search(self):
        return self.form.searchEdit.lineEdit().text()

    def search(self):
        self.table.search(self._lastSearchTxt)

    def onSearchActivated(self):
        normed = self.col.build_search_string(self.current_search())
        self.search_for(normed)
        self.update_history()

    def update_history(self):
        sh = self.history
        if self._lastSearchTxt in sh:
            sh.remove(self._lastSearchTxt)
        sh.insert(0, self._lastSearchTxt)
        self.form.searchEdit.clear()
        self.form.searchEdit.addItems(sh)


def _stub(name, **attrs):
    mod = sys.modules.get(name) or types.ModuleType(name)
    for k, v in attrs.items():
        setattr(mod, k, v)
    sys.modules[name] = mod
    return mod


class _AddonManager:
    cfg = {}

    def getConfig(self, *a, **k):
        return dict(self.cfg)


_mw = types.SimpleNamespace(addonManager=_AddonManager())
_stub("aqt", mw=_mw)
_stub("aqt.qt", Qt=Qt)

import deck_names  # noqa: E402


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

PATH = "Fundamentals of Biochemistry::Amino Acids"
QUERY = '"deck:Fundamentals of Biochemistry::Amino Acids"'
DISPLAY, TOOLTIP = Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.ToolTipRole


def _boot(rows=None):
    model = FakeModel(
        ["noteFld", "deck"],
        rows or [
            ["alanine", PATH],
            ["arginine", "MCAT"],
            ["peptide bond", "Cram (Fundamentals of Biochemistry::Exam 1::02 Peptides)"],
            ["catalysis", "Fundamentals of Biochemistry::Exam 1::07 Enzymes (Classes 7&8)"],
        ],
    )
    br = FakeBrowser(model)
    assert deck_names.install(br), "install() returned nothing"
    return br


def _box(br):
    return br.form.searchEdit.lineEdit()


# --------------------------------------------------------------------------- #
# Wiring
# --------------------------------------------------------------------------- #

def test_addon_wires_install_on_browser_will_show():
    with open(os.path.join(ROOT, "__init__.py"), encoding="utf-8") as f:
        src = f.read()
    assert "browser_will_show.append(_deck_names.install)" in src, (
        "deck_names.install is not registered on browser_will_show"
    )
    with open(os.path.join(ROOT, "config.json"), encoding="utf-8") as f:
        cfg = json.load(f)
    assert cfg.get("clean_deck_names") is True, "config.json lacks clean_deck_names"


# --------------------------------------------------------------------------- #
# The reported bug: the search box after a click in the sidebar
# --------------------------------------------------------------------------- #

def test_sidebar_click_shows_the_title_and_searches_the_deck():
    br = _boot()
    br.search_for(QUERY)          # what SidebarTreeView.update_search calls
    assert _box(br).text() == "Amino Acids"
    assert br.table.searches[-1] == QUERY, "the real search must still run"
    # Anything that reads the box back gets the query, not the title:
    # Enter, ⌘-click AND / ⇧-click OR, Create Filtered Deck, saved searches.
    assert br.current_search() == QUERY
    assert _box(br).toolTip() == QUERY


def test_enter_on_the_untouched_box_reruns_the_deck_search():
    br = _boot()
    br.search_for(QUERY)
    br.onSearchActivated()
    assert br.table.searches[-1] == QUERY, "Enter turned the title into a text search"
    assert br.history[0] == QUERY, "history keeps the real query"
    # update_history rebuilt the combobox and Qt wrote the raw query back
    # into the line edit; the title has to win again.
    assert _box(br).text() == "Amino Acids"


def test_typing_makes_the_box_the_search_again():
    br = _boot()
    br.search_for(QUERY)
    _box(br).type("Amino Acids")   # the user typed the same words by hand
    assert br.current_search() == "Amino Acids"
    br.onSearchActivated()
    assert br.table.searches[-1] == "Amino Acids"
    _box(br).type("tag:leech")
    br.onSearchActivated()
    assert br.table.searches[-1] == "tag:leech"
    assert _box(br).text() == "tag:leech"


def test_a_prompt_anki_chose_itself_is_left_alone():
    # show_single_card passes an empty prompt on purpose.
    br = _boot()
    br.search_for("nid:123", "")
    assert _box(br).text() == ""
    assert br.current_search() == ""


def test_a_search_already_in_the_box_at_install_is_retitled():
    # Browser.setupSearch runs its first search before browser_will_show.
    model = FakeModel(["deck"], [[PATH]])
    br = FakeBrowser(model)
    br.search_for(QUERY)
    assert _box(br).text() == QUERY
    deck_names.install(br)
    assert _box(br).text() == "Amino Acids"
    assert br.current_search() == QUERY


def test_install_is_idempotent():
    br = _boot()
    assert deck_names.install(br)
    br.search_for(QUERY)
    br.onSearchActivated()
    assert br.table.searches[-2:] == [QUERY, QUERY]
    assert _box(br).text() == "Amino Acids"


# --------------------------------------------------------------------------- #
# The reported bug: the Deck column
# --------------------------------------------------------------------------- #

def test_deck_column_shows_the_title_and_keeps_the_path_as_tooltip():
    br = _boot()
    m = br.table._model
    assert m.data(m.index(0, 1), DISPLAY) == "Amino Acids"
    assert m.data(m.index(0, 1), TOOLTIP) == PATH
    assert m.data(m.index(1, 1), DISPLAY) == "MCAT"
    # A card in a filtered deck: "filtered (home)", both by title.
    assert m.data(m.index(2, 1), DISPLAY) == "Cram (02 Peptides)"
    # Parentheses that belong to the title stay.
    assert m.data(m.index(3, 1), DISPLAY) == "07 Enzymes (Classes 7&8)"
    # Other columns are not touched, even when they contain "::".
    m.rows[0][0] = "a::b"
    assert m.data(m.index(0, 0), DISPLAY) == "a::b"


def test_model_patch_survives_a_new_model_on_the_next_search():
    br = _boot()
    br.table._model = FakeModel(["deck"], [["A::B"]])
    br.search_for(QUERY)
    m = br.table._model
    assert m.data(m.index(0, 0), DISPLAY) == "B"


# --------------------------------------------------------------------------- #
# Pure helpers
# --------------------------------------------------------------------------- #

def test_title_is_the_last_path_segment():
    assert deck_names.leaf("A::B::C") == "C"
    assert deck_names.leaf("MCAT") == "MCAT"
    assert deck_names.leaf("") == ""


def test_pretty_search_rewrites_only_plain_deck_terms():
    ps = deck_names.pretty_search
    assert ps(QUERY) == "Amino Acids"
    assert ps("deck:MCAT::MileDown") == "MileDown"
    assert ps('deck:"A B::C D"') == "C D"
    assert ps(f'{QUERY} tag:leech') == "Amino Acids tag:leech"
    assert ps('("deck:A::B" OR deck:C::D)') == "(B OR D)"
    # Escapes come off for display: the deck is called Say "hi".
    assert ps(r'"deck:Top::Say \"hi\""') == 'Say "hi"'
    # Left alone: negations, Anki's own deck keywords, other keys, text.
    assert ps(f"-{QUERY}") == f"-{QUERY}"
    assert ps("-deck:A::B") == "-deck:A::B"
    assert ps("deck:current") == "deck:current"
    assert ps("deck:filtered") == "deck:filtered"
    assert ps("deck:*") == "deck:*"
    assert ps("tag:a::b") == "tag:a::b"
    assert ps('"amino acids"') == '"amino acids"'
    assert ps("") == ""


def test_deck_label_for_the_column():
    dl = deck_names.deck_label
    assert dl(PATH) == "Amino Acids"
    assert dl("Cram (A::B)") == "Cram (B)"
    assert dl("X::Cram (A::B)") == "Cram (B)"
    assert dl("Plain (note)") == "Plain (note)"
    assert dl("(*)") == "(*)"


def test_title_helper_follows_the_config():
    assert deck_names.title(PATH) == "Amino Acids"
    _AddonManager.cfg = {"clean_deck_names": False}
    try:
        assert deck_names.title(PATH) == PATH
    finally:
        _AddonManager.cfg = {}


def test_config_off_leaves_anki_alone():
    _AddonManager.cfg = {"clean_deck_names": False}
    try:
        model = FakeModel(["deck"], [[PATH]])
        br = FakeBrowser(model)
        assert deck_names.install(br) is None
        br.search_for(QUERY)
        assert _box(br).text() == QUERY
        assert model.data(model.index(0, 0), DISPLAY) == PATH
    finally:
        _AddonManager.cfg = {}


if __name__ == "__main__":
    names = [n for n in sorted(globals()) if n.startswith("test_")]
    for n in names:
        globals()[n]()
        print("ok", n)
    print("PASS test_deck_names")
