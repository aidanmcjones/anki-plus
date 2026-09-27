"""Anki Design: a deck is named by its title, not by its path.

Anki's canonical deck name is the whole path, `Parent::Child::Leaf`, and
the search that finds one deck is `"deck:Parent::Child::Leaf"`. Both leak
into the Browse tab as-is: click a deck in the sidebar and the search box
fills with the quoted query, while the Deck column repeats the full path
on every row. The user reads a deck by its title, the way the sidebar tree
and the deck list already show it. So:

  - `leaf(name)` is the last path segment: the title.
  - `title(name)` is the same, gated on the config key.
  - `deck_label(text)` is the Deck column's cell: the title, and for a
    card in a filtered deck the home deck's title in parentheses, which is
    how the backend lays that cell out.
  - `pretty_search(search)` rewrites every plain `deck:` term of a search
    string to that deck's title. Display only.

`install(browser)` puts them to work on one Browser, on
`browser_will_show`:

  - `search_for` keeps the real query where stock Anki keeps it
    (`_lastSearchTxt`) and shows the pretty form in the box. Stock Anki
    already separates the two: `search_for(search, prompt)` lets the box
    show something other than the search, which `show_single_card` uses
    to blank it. `current_search` hands the real query back while the box
    still shows the pretty form, so everything that reads the box (Enter,
    ⌘-click AND, ⇧-click OR, Create Filtered Deck, Save Current Search)
    sees the query and not the title. Typing anything into the box drops
    the pairing: from then on the text is the search, as it always was.
  - `update_history` puts the pretty form back afterwards. Qt makes the
    first item current when items land in an empty combobox, and stock
    Anki leans on that to keep the raw query in the box after the history
    list is rebuilt.
  - the table model's `data()` answers the Deck column's DisplayRole with
    the label. The tooltip keeps the full path, so hovering a row still
    says where the deck lives.
"""

from __future__ import annotations

import re
from typing import Any, Optional

from aqt import mw
from aqt.qt import Qt

ADDON = __name__.split(".")[0]
CONFIG_KEY = "clean_deck_names"


def _config() -> dict:
    try:
        return mw.addonManager.getConfig(ADDON) or {}
    except Exception:
        return {}


def enabled() -> bool:
    return bool(_config().get(CONFIG_KEY, True))


# --------------------------------------------------------------------------- #
# Pure helpers
# --------------------------------------------------------------------------- #

def leaf(name: str) -> str:
    """The title of a deck: the last segment of its `::` path."""
    name = name or ""
    return name.split("::")[-1] or name


def title(name: str) -> str:
    """`leaf`, unless the user turned the feature off."""
    return leaf(name) if enabled() else (name or "")


# The backend renders a filtered deck's cell as "filtered (home)". Greedy on
# the left so a title that itself ends in parentheses, "07 Enzymes (Classes
# 7&8)", splits into path + "(Classes 7&8)" and reassembles unchanged.
_FILTERED_RE = re.compile(r"^(.*) \((.*)\)$", re.S)


def deck_label(text: str) -> str:
    """The Deck column's cell, by title."""
    if "::" not in (text or ""):
        return text
    m = _FILTERED_RE.match(text)
    if m:
        return f"{leaf(m.group(1))} ({leaf(m.group(2))})"
    return leaf(text)


# One search token: `"quoted"` or `key:"quoted"` (optionally negated), a
# parenthesis, or a bare word. Anki's normalised output quotes whole terms,
# `"deck:A B"`; a hand-typed `deck:"A B"` is accepted too.
_TOKEN_RE = re.compile(
    r'-?(?:[A-Za-z_-]+:)?"(?:[^"\\]|\\.)*"'
    r"|[()]"
    r'|[^\s()"]+'
)
_UNESCAPE_RE = re.compile(r"\\(.)")
# `deck:` values that are keywords, not decks.
_SPECIAL = {"*", "current", "filtered"}


def _deck_term(token: str) -> Optional[str]:
    """The deck path a token names, or None when it is not a plain `deck:`
    term: negated, a keyword, another key, or free text."""
    if token.startswith("-"):
        return None
    body = token
    if len(body) >= 2 and body[0] == '"' and body[-1] == '"':
        body = body[1:-1]
    if body[:5].lower() != "deck:":
        return None
    value = body[5:]
    if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
        value = value[1:-1]
    if not value or value.lower() in _SPECIAL:
        return None
    return _UNESCAPE_RE.sub(r"\1", value)


def pretty_search(search: str) -> str:
    """`search` with every plain `deck:` term replaced by the deck's title.
    Anything that is not such a term comes through untouched, so a search
    with nothing to retitle is returned as it was."""

    def swap(m: "re.Match[str]") -> str:
        path = _deck_term(m.group(0))
        if path is None:
            return m.group(0)
        return leaf(path) or m.group(0)

    return _TOKEN_RE.sub(swap, search or "")


# --------------------------------------------------------------------------- #
# Browser
# --------------------------------------------------------------------------- #

def _column_key(model: Any, index: Any) -> str:
    section = index.column()
    try:
        return str(model.column_key_at(section))
    except Exception:
        pass
    try:
        return str(model.active_columns[section])
    except Exception:
        pass
    try:
        return str(model.column_at(index).key)
    except Exception:
        return ""


def _patch_model(browser: Any) -> None:
    """Deck column by title. Patched on the model instance, like
    browse_style's header patch, so nothing else in Anki sees it."""
    model = getattr(getattr(browser, "table", None), "_model", None)
    if model is None:
        try:
            model = browser.form.tableView.model()
        except Exception:
            model = None
    if model is None or getattr(model, "_ba_deck_label_patched", False):
        return
    orig = model.data

    def data(index: Any, role: int = 0) -> Any:
        out = orig(index, role)
        if (
            role == Qt.ItemDataRole.DisplayRole
            and isinstance(out, str)
            and "::" in out
            and _column_key(model, index) == "deck"
        ):
            return deck_label(out)
        return out

    model.data = data  # type: ignore[assignment]
    model._ba_deck_label_patched = True


def _line_edit(browser: Any) -> Any:
    return browser.form.searchEdit.lineEdit()


def _show(browser: Any, search: str, pretty: str) -> None:
    """Remember that the box shows `pretty` for `search`, and say so in
    the tooltip."""
    browser._ba_pretty_search = (search, pretty)
    try:
        _line_edit(browser).setToolTip(search if pretty != search else "")
    except Exception:
        pass


def _patch_search(browser: Any) -> None:
    orig_search_for = browser.search_for
    orig_current = browser.current_search
    orig_history = browser.update_history

    def search_for(search: str, prompt: Optional[str] = None) -> None:
        if prompt is None:
            pretty = pretty_search(search)
            if pretty != search:
                prompt = pretty
            _show(browser, search, pretty)
        else:
            _show(browser, search, search)
        _patch_model(browser)
        orig_search_for(search, prompt)

    def current_search() -> str:
        text = orig_current()
        pair = getattr(browser, "_ba_pretty_search", None)
        if pair and text == pair[1]:
            return pair[0]
        return text

    def update_history() -> None:
        orig_history()
        pair = getattr(browser, "_ba_pretty_search", None)
        if not pair or pair[0] == pair[1]:
            return
        try:
            if browser._lastSearchTxt == pair[0]:
                line = _line_edit(browser)
                if line.text() != pair[1]:
                    line.setText(pair[1])
        except Exception:
            pass

    def typed(_text: str = "") -> None:
        # The user is writing their own search now; the box is the truth.
        browser._ba_pretty_search = None
        try:
            _line_edit(browser).setToolTip("")
        except Exception:
            pass

    browser.search_for = search_for  # type: ignore[assignment]
    browser.current_search = current_search  # type: ignore[assignment]
    browser.update_history = update_history  # type: ignore[assignment]
    try:
        _line_edit(browser).textEdited.connect(typed)
    except Exception:
        pass

    # Browser.setupSearch ran its first search before browser_will_show:
    # if the box is still showing that raw query, retitle it now.
    try:
        last = getattr(browser, "_lastSearchTxt", "") or ""
        line = _line_edit(browser)
        if last and line.text() == last:
            pretty = pretty_search(last)
            if pretty != last:
                line.setText(pretty)
            _show(browser, last, pretty)
    except Exception:
        pass


def install(browser: Any) -> Optional[bool]:
    """`gui_hooks.browser_will_show`: one Browser, once."""
    if not enabled():
        return None
    if getattr(browser, "_ba_deck_names_installed", False):
        return True
    try:
        _patch_search(browser)
    except Exception as e:
        print(f"[anki-design.deck_names] search patch failed: {e}", flush=True)
        return None
    try:
        _patch_model(browser)
    except Exception as e:
        print(f"[anki-design.deck_names] model patch failed: {e}", flush=True)
    browser._ba_deck_names_installed = True
    return True
