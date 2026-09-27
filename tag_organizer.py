"""Tag auto-organizer: file every tag under its course's root.

Each top-level deck is a course with a tag root (config `tag_roots`, e.g.
"Fundamentals of Biochemistry" -> "FunBiochem"; an unmapped deck gets its
name with non-alphanumerics collapsed to "_"). A tag whose root (the part
before the first "::") is not a course root and not ignored is renamed
under the course root when EVERY note carrying it (or one of its child
tags) lives in that one course: `hi_yield` -> `FunBiochem::hi_yield`,
`Kaplan::Ch1` on MCAT notes -> `MCAT::Kaplan::Ch1`. Tags spanning several
courses, or on notes with no course, are left alone; a mixed parent is
split, so its single-course children are still filed.

A card's course is its home deck: `odid` when it sits in a filtered deck
(for example "Restudy"). Filtered top-level decks and "Default" have no
course.

Runs after any op that changed tags or note text (adds, edits, Browse tag
changes, imports), debounced, on the tags that appeared since the last
pass; a full pass runs once when the profile opens and from Tools >
"Organize Tags Now" (also on the Browse sidebar's Tags header). Every pass
is one undoable op built from `col.tags.rename`.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from aqt import mw

MENU_LABEL = "Organize Tags Now"
UNDO_NAME = "Organize Tags"
DEBOUNCE_MS = 500
DEFAULT_ROOTS = {
    "Microbiology": "Microbiology",
    "Fundamentals of Biochemistry": "FunBiochem",
    "MCAT": "MCAT",
}
DEFAULT_IGNORE = ["Type", "AnkiHub_Subdeck", "marked", "leech"]

# sentinel passed as the op initiator so our own rename does not re-trigger
_INITIATOR = object()

_state: Dict[str, Any] = {
    "known": None,  # set of casefolded tag names seen at the last pass
    "timer": None,
    "running": False,
    "pending": False,
    "last": None,  # summary dict of the last pass (tests, reports)
}


def _log(msg: str) -> None:
    print(f"[anki-design] tag_organizer: {msg}", flush=True)


def _addon_name() -> str:
    return __name__.split(".")[0]


def _config() -> Dict[str, Any]:
    try:
        return mw.addonManager.getConfig(_addon_name()) or {}
    except Exception:
        return {}


def enabled() -> bool:
    return bool(_config().get("auto_organize_tags", True))


def _tooltip(msg: str) -> None:
    try:
        from aqt.utils import tooltip

        tooltip(msg, period=3500)
    except Exception:
        _log(msg)


# -- pure logic --------------------------------------------------------------


def derive_root(deck_name: str) -> str:
    """"Organic Chemistry" -> "Organic_Chemistry"."""
    return re.sub(r"[\W_]+", "_", deck_name or "").strip("_")


def tag_root(tag: str) -> str:
    return tag.split("::", 1)[0]


def is_ignored(tag: str, ignore: Iterable[str]) -> bool:
    root = tag_root(tag).casefold()
    if root.startswith("ankihub"):
        return True
    for entry in ignore:
        e = str(entry).casefold()
        if not e:
            continue
        if root == e or tag.casefold() == e:
            return True
    return False


def plan_renames(
    note_tags: Dict[int, List[str]],
    note_course: Dict[int, Optional[str]],
    candidates: Iterable[str],
    course_roots: Iterable[str],
    ignore: Iterable[str],
) -> Tuple[List[Tuple[str, str]], List[str]]:
    """Decide which tag subtrees to rename.

    note_tags: note id -> its tags. note_course: note id -> the course root
    of every card of that note, or None (no course, or cards in several
    courses). candidates: tags to consider (new since the last pass, or all).
    Returns ([(old, new)], [left-alone candidate roots/nodes that span
    courses]). Renamed subtrees never overlap.
    """
    roots_cf = {r.casefold() for r in course_roots if r}
    ignore = list(ignore)
    cand = []
    for t in candidates:
        if not t:
            continue
        if tag_root(t).casefold() in roots_cf or is_ignored(t, ignore):
            continue
        cand.append(t)
    if not cand:
        return [], []

    # node (casefolded full path) -> set of courses of notes carrying the
    # node or any descendant; display name from the first spelling seen
    wanted_roots = {tag_root(t).casefold() for t in cand}
    courses: Dict[str, Set[Optional[str]]] = {}
    spelled: Dict[str, str] = {}
    children: Dict[str, Set[str]] = {}
    for nid, tags in note_tags.items():
        course = note_course.get(nid)
        for tag in tags:
            parts = tag.split("::")
            if parts[0].casefold() not in wanted_roots:
                continue
            parent = None
            for i in range(1, len(parts) + 1):
                name = "::".join(parts[:i])
                key = name.casefold()
                spelled.setdefault(key, name)
                courses.setdefault(key, set()).add(course)
                if parent is not None:
                    children.setdefault(parent, set()).add(key)
                parent = key

    # nodes on the path of a candidate: only those may be renamed/descended
    on_path: Set[str] = set()
    for t in cand:
        parts = t.split("::")
        for i in range(1, len(parts) + 1):
            on_path.add("::".join(parts[:i]).casefold())

    renames: List[Tuple[str, str]] = []
    mixed: List[str] = []

    def visit(key: str) -> None:
        cs = courses.get(key)
        if not cs:
            return
        if len(cs) == 1:
            (course,) = tuple(cs)
            if course:
                old = spelled[key]
                renames.append((old, f"{course}::{old}"))
                return
        kids = sorted(k for k in children.get(key, ()) if k in on_path)
        if not kids:
            mixed.append(spelled[key])
        for k in kids:
            visit(k)

    for root in sorted(wanted_roots):
        if root in on_path:
            visit(root)
    return renames, mixed


# -- collection access -------------------------------------------------------


def course_roots_for(col: Any) -> Tuple[Dict[int, Optional[str]], Set[str]]:
    """(deck id -> course root or None, set of every course root)."""
    cfg = _config()
    mapping = cfg.get("tag_roots")
    if not isinstance(mapping, dict):
        mapping = DEFAULT_ROOTS
    by_top = {str(k).casefold(): str(v).strip() for k, v in mapping.items() if str(v).strip()}
    deck_root: Dict[int, Optional[str]] = {}
    top_root: Dict[str, Optional[str]] = {}
    roots: Set[str] = set(by_top.values())
    all_decks = list(col.decks.all_names_and_ids())
    dyn_names = set()
    for d in all_decks:
        try:
            if col.decks.is_filtered(d.id):
                dyn_names.add(d.name.casefold())
        except Exception:
            pass
    for d in all_decks:
        top = d.name.split("::", 1)[0]
        tk = top.casefold()
        if tk not in top_root:
            if tk == "default" or tk in dyn_names:
                top_root[tk] = None
            else:
                r = by_top.get(tk) or derive_root(top) or None
                top_root[tk] = r
                if r:
                    roots.add(r)
        deck_root[int(d.id)] = top_root[tk]
    return deck_root, roots


def collect(col: Any, roots_filter: Optional[Set[str]] = None):
    """note_tags and note_course for notes whose tags have a root in
    roots_filter (casefolded), or every tagged note when None."""
    deck_root, roots = course_roots_for(col)
    note_tags: Dict[int, List[str]] = {}
    for nid, tags in col.db.execute("select id, tags from notes where tags != ''"):
        ts = [t for t in str(tags).split() if t]
        if roots_filter is not None:
            ts = [t for t in ts if tag_root(t).casefold() in roots_filter]
        if ts:
            note_tags[int(nid)] = ts
    note_course: Dict[int, Optional[str]] = {}
    if note_tags:
        seen: Dict[int, Set[Optional[str]]] = {}
        for nid, did, odid in col.db.execute("select nid, did, odid from cards"):
            nid = int(nid)
            if nid not in note_tags:
                continue
            home = int(odid) if odid else int(did)
            seen.setdefault(nid, set()).add(deck_root.get(home))
        for nid in note_tags:
            cs = seen.get(nid, set())
            note_course[nid] = next(iter(cs)) if len(cs) == 1 else None
    return note_tags, note_course, roots


def _all_tags(col: Any) -> List[str]:
    try:
        return list(col.tags.all())
    except Exception:
        return []


def compute(col: Any, full: bool) -> Tuple[List[Tuple[str, str]], List[str]]:
    tags = _all_tags(col)
    known = _state.get("known")
    if full or known is None:
        cand = tags
    else:
        cand = [t for t in tags if t.casefold() not in known]
    if not cand:
        return [], []
    cfg = _config()
    ignore = cfg.get("tag_organize_ignore")
    if not isinstance(ignore, list):
        ignore = DEFAULT_IGNORE
    filt = {tag_root(t).casefold() for t in cand}
    note_tags, note_course, roots = collect(col, filt)
    return plan_renames(note_tags, note_course, cand, roots, ignore)


def _snapshot(col: Any) -> None:
    _state["known"] = {t.casefold() for t in _all_tags(col)}


def summary(renames: List[Tuple[str, str]]) -> str:
    by_root: Dict[str, int] = {}
    for _old, new in renames:
        r = tag_root(new)
        by_root[r] = by_root.get(r, 0) + 1
    n = len(renames)
    word = "tag" if n == 1 else "tags"
    return f"Filed {n} {word} under " + ", ".join(f"{r}::" for r in sorted(by_root))


# -- running a pass ----------------------------------------------------------


def run_pass(full: bool = False, report: bool = False) -> None:
    """Plan on the main thread (cheap), rename in one undoable op."""
    col = getattr(mw, "col", None)
    if col is None:
        return
    if _state["running"]:
        _state["pending"] = True
        return
    try:
        renames, mixed = compute(col, full)
    except Exception as exc:
        _log(f"plan failed: {exc!r}")
        return
    _state["last"] = {"full": full, "renames": renames, "mixed": mixed}
    planned = {t.casefold() for t in _all_tags(col)}
    if not renames:
        _snapshot(col)
        if report:
            msg = "Tags are already organized."
            if mixed:
                n = len(mixed)
                msg += f" {n} {'tag spans' if n == 1 else 'tags span'} several courses and stayed put."
            _tooltip(msg)
        return

    from aqt.operations import CollectionOp

    def op(c: Any):
        pos = c.add_custom_undo_entry(UNDO_NAME)
        for old, new in renames:
            c.tags.rename(old, new)
        return c.merge_undo_entries(pos)

    def done(_res: Any = None) -> None:
        _state["running"] = False
        # known = what this pass saw plus what it created; a tag that
        # appeared meanwhile from another op stays new for the next pass
        targets = [n.casefold() for _o, n in renames]
        try:
            current = {t.casefold() for t in _all_tags(mw.col)}
        except Exception:
            current = set()
        made = {
            t for t in current
            if any(t == n or t.startswith(n + "::") for n in targets)
        }
        _state["known"] = (planned & current) | made
        if current - _state["known"]:
            _state["pending"] = True
        msg = summary(renames)
        if report and mixed:
            n = len(mixed)
            msg += f"; {n} {'tag spans' if n == 1 else 'tags span'} several courses and stayed put."
        _tooltip(msg)
        if _state["pending"]:
            _state["pending"] = False
            schedule()

    def failed(exc: Exception) -> None:
        _state["running"] = False
        _log(f"rename failed: {exc!r}")

    _state["running"] = True
    CollectionOp(parent=mw, op=op).success(done).failure(failed).run_in_background(
        initiator=_INITIATOR
    )


def organize_now() -> None:
    """Tools menu / sidebar: a full pass that always reports."""
    run_pass(full=True, report=True)


def _timer():
    t = _state.get("timer")
    if t is None:
        from aqt.qt import QTimer

        t = QTimer(mw)
        t.setSingleShot(True)
        t.timeout.connect(lambda: enabled() and run_pass(full=False))
        _state["timer"] = t
    return t


def schedule() -> None:
    try:
        _timer().start(DEBOUNCE_MS)
    except Exception as exc:
        _log(f"schedule failed: {exc!r}")


# -- hooks -------------------------------------------------------------------


def _was_user_rename() -> bool:
    """True when the last undoable step is a tag rename or reparent
    (sidebar rename, sidebar drag): Anki labels both "Rename Tag"."""
    try:
        from aqt.utils import tr

        return (mw.col.undo_status().undo or "") == tr.actions_rename_tag()
    except Exception:
        return False


def on_operation_did_execute(changes: Any, handler: Any) -> None:
    try:
        if handler is _INITIATOR or not enabled():
            return
        if not (getattr(changes, "tag", False) or getattr(changes, "note_text", False)):
            return
        if _was_user_rename():
            # a tag the user renamed or dragged by hand stays where they
            # put it (until the next full pass)
            known = _state.get("known")
            if known is not None:
                known |= {t.casefold() for t in _all_tags(mw.col)}
            return
        schedule()
    except Exception as exc:
        _log(f"hook failed: {exc!r}")


def on_profile_open() -> None:
    _state["known"] = None
    _state["running"] = False
    _state["pending"] = False
    if not enabled():
        try:
            _snapshot(mw.col)
        except Exception:
            pass
        return

    def first() -> None:
        if enabled():
            run_pass(full=True)
        else:
            _snapshot(mw.col)

    try:
        mw.progress.single_shot(1500, first)
    except Exception as exc:
        _log(f"profile pass failed: {exc!r}")


def on_profile_close() -> None:
    t = _state.get("timer")
    if t is not None:
        t.stop()
    _state["known"] = None


def _add_menu() -> None:
    try:
        from aqt.qt import QAction

        act = QAction(MENU_LABEL, mw)
        act.setObjectName("anki_design_organize_tags")
        act.triggered.connect(organize_now)
        mw.form.menuTools.addAction(act)
    except Exception as exc:
        _log(f"menu failed: {exc!r}")


def on_sidebar_context_menu(sidebar: Any, menu: Any, item: Any, index: Any) -> None:
    try:
        from aqt.browser.sidebar.item import SidebarItemType

        if item is None or item.item_type is not SidebarItemType.TAG_ROOT:
            return
        menu.addSeparator()
        menu.addAction(MENU_LABEL, organize_now)
    except Exception as exc:
        _log(f"sidebar menu failed: {exc!r}")


def register() -> None:
    from aqt import gui_hooks

    gui_hooks.operation_did_execute.append(on_operation_did_execute)
    gui_hooks.profile_did_open.append(on_profile_open)
    gui_hooks.profile_will_close.append(on_profile_close)
    gui_hooks.browser_sidebar_will_show_context_menu.append(on_sidebar_context_menu)
    gui_hooks.main_window_did_init.append(_add_menu)
