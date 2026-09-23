"""Regression tests for bugreport.py's ticket writer (Cmd+Shift+B / the
"Report a bug" command-palette entry).

Covered here:
  (a) a full ticket (reviewer mid-card, screenshot on) writes ticket.json,
      screenshot.png and qa.html with the schema-v1 shape, and — when the
      sibling `ankibug` package is importable — validates against its own
      `ankibug.schema.validate`;
  (b) "include screenshot" off skips screenshot.png and leaves
      capture.screenshot_path null;
  (c) no reviewer card (deck list / overview) still writes a valid ticket
      with every reviewer.* field null;
  (d) the hand-built fallback shape (used when `ankibug` isn't importable)
      matches the same fields as the ankibug-backed path;
  (e) a webview that can't be evaluated (no #qa page reachable) still
      writes a ticket — capture degrades to nulls/empties, nothing raises;
  (f) index.md accumulates one row per ticket, with pipe characters in the
      note escaped.

Runs standalone (no Anki install needed):
`python3 tests/test_bug_report.py`. Stubs aqt before import, same approach
as test_deadline_modes.py, and fakes mw.reviewer/mw.col/mw.web/mw.grab()
with plain objects — everything under test here is file I/O and dict shape.
"""

import copy
import datetime
import json
import os
import shutil
import sys
import tempfile
import types


def _stub(name, **attrs):
    mod = sys.modules.get(name) or types.ModuleType(name)
    for k, v in attrs.items():
        setattr(mod, k, v)
    sys.modules[name] = mod
    return mod


class _FakeAddonManager:
    def __init__(self):
        self.cfg = {}

    def getConfig(self, *a, **k):
        return copy.deepcopy(self.cfg)

    def writeConfig(self, _addon, cfg):
        self.cfg = copy.deepcopy(cfg)


class _FakePixmap:
    def __init__(self, should_fail=False):
        self.should_fail = should_fail

    def save(self, path, fmt):
        if self.should_fail:
            return False
        with open(path, "wb") as fh:
            fh.write(b"\x89PNG\r\n\x1a\nFAKE")
        return True


class _FakeWebview:
    """Stands in for an AnkiWebView. `payload=None` simulates a page with
    no #qa (deck list) — still returns a dict, just with nulls."""

    def __init__(self, payload=None, raises=False):
        self.payload = payload if payload is not None else {
            "qa_html": None, "visible_text": "", "console_errors": [],
        }
        self.raises = raises
        self.calls = 0

    def evalWithCallback(self, js, cb):
        self.calls += 1
        if self.raises:
            raise RuntimeError("webview gone")
        cb(self.payload)


class _FakeCard:
    def __init__(self, cid=1234567890, nid=987654321, did=42, ord=0, notetype_name="Basic"):
        self.id = cid
        self.nid = nid
        self.did = did
        self.ord = ord
        self._nt = {"name": notetype_name}

    def note_type(self):
        return self._nt


class _FakeReviewer:
    def __init__(self, state="question", card=None):
        self.state = state
        self.card = card


class _FakeDecks:
    def name(self, did):
        return "Bio::Exam 1" if did == 42 else "Default"


class _FakeCol:
    def __init__(self):
        self.decks = _FakeDecks()


def _make_mw(reviewer=None, web=None, state="deckBrowser", grab_fails=False):
    return types.SimpleNamespace(
        col=_FakeCol(),
        addonManager=_FakeAddonManager(),
        reviewer=reviewer,
        web=web if web is not None else _FakeWebview(),
        state=state,
        grab=lambda: _FakePixmap(should_fail=grab_fails),
    )


_mw = _make_mw()
_stub("aqt", mw=_mw)
_stub("aqt.utils", tooltip=lambda *a, **k: None)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import importlib.util

_spec = importlib.util.spec_from_file_location(
    "ba_bugreport",
    os.path.join(os.path.dirname(__file__), "..", "bugreport.py"),
)
br = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(br)

# The ankibug package (SCHEMA.md's executable counterpart), when checked
# out at ~/dev/ankibug, so we can cross-validate our output against it.
try:
    from ankibug import schema as ankibug_schema
except Exception:
    ankibug_schema = None


def _reset_mw(**kw):
    """Swap out the module-level `mw` bugreport.py imported at load time."""
    new_mw = _make_mw(**kw)
    br.mw = new_mw
    return new_mw


def _tmp_tickets_dir():
    d = tempfile.mkdtemp(prefix="ba-bugreport-test-")
    br.TICKETS_DIR = d
    return d


def _run_create_ticket(note, expected, include_screenshot):
    result = {}

    def _on_done(ticket):
        result["ticket"] = ticket

    br.create_ticket(note, expected, include_screenshot, on_done=_on_done)
    assert "ticket" in result, "on_done was never called"
    return result["ticket"]


def test_full_ticket_writes_expected_files_and_shape():
    tdir = _tmp_tickets_dir()
    card = _FakeCard()
    rv = _FakeReviewer(state="answer", card=card)
    web = _FakeWebview(payload={
        "qa_html": "<div id=\"qa\">front &rarr; back</div>",
        "visible_text": "front -> back",
        "console_errors": [{"ts": 1, "message": "boom", "source": "reviewer.js", "line": 12}],
    })
    _reset_mw(reviewer=rv, web=web, state="review")

    ticket = _run_create_ticket("Answer buttons vanished after Undo",
                                 "The Again/Good/Easy row should stay visible", True)

    ticket_dir = os.path.join(tdir, ticket["id"])
    assert os.path.isdir(ticket_dir)
    assert os.path.isfile(os.path.join(ticket_dir, "ticket.json"))
    assert os.path.isfile(os.path.join(ticket_dir, "screenshot.png"))
    assert os.path.isfile(os.path.join(ticket_dir, "qa.html"))

    with open(os.path.join(ticket_dir, "ticket.json")) as fh:
        on_disk = json.load(fh)
    assert on_disk == ticket

    assert ticket["schema"] == 1
    assert ticket["id"].startswith(datetime.datetime.now().strftime("%Y%m%d-"))
    assert ticket["source"] == "hotkey"
    assert ticket["note"] == "Answer buttons vanished after Undo"
    assert ticket["status"] == "new"
    assert ticket["kind"] == "unknown"
    assert ticket["expected"] == "The Again/Good/Easy row should stay visible"

    app = ticket["app"]
    assert app["addon_repo"] == br.ADDON_SRC
    for field in ("anki_version", "branch", "commit", "dirty"):
        assert field in app

    reviewer = ticket["reviewer"]
    assert reviewer["state"] == "answer"
    assert reviewer["card_id"] == 1234567890
    assert reviewer["note_id"] == 987654321
    assert reviewer["notetype"] == "Basic"
    assert reviewer["deck"] == "Bio::Exam 1"
    assert reviewer["template_ord"] == 0

    assert ticket["deck_build"] is None
    assert ticket["fix"] is None

    capture = ticket["capture"]
    assert capture["pages"] == []
    assert capture["qa_html_path"] == "qa.html"
    assert capture["visible_text"] == "front -> back"
    assert capture["console_errors"] == [
        {"ts": 1, "message": "boom", "source": "reviewer.js", "line": 12}
    ]
    assert capture["screenshot_path"] == "screenshot.png"
    assert capture["captured_at"]

    with open(os.path.join(ticket_dir, "qa.html")) as fh:
        assert fh.read() == "<div id=\"qa\">front &rarr; back</div>"

    if ankibug_schema is not None:
        # Cross-validate against the concurrently-written schema module —
        # `expected` is an extra field on top, which validate() allows.
        ankibug_schema.validate(ticket)

    shutil.rmtree(tdir, ignore_errors=True)


def test_screenshot_off_skips_the_file():
    tdir = _tmp_tickets_dir()
    _reset_mw()
    ticket = _run_create_ticket("Minor visual glitch", "", False)
    ticket_dir = os.path.join(tdir, ticket["id"])
    assert not os.path.exists(os.path.join(ticket_dir, "screenshot.png"))
    assert ticket["capture"]["screenshot_path"] is None
    shutil.rmtree(tdir, ignore_errors=True)


def test_no_reviewer_card_still_writes_a_valid_ticket():
    tdir = _tmp_tickets_dir()
    _reset_mw(reviewer=None, state="deckBrowser")
    ticket = _run_create_ticket("Deck list flashed white on theme switch", "", True)
    reviewer = ticket["reviewer"]
    for field in ("state", "card_id", "note_id", "notetype", "deck", "template_ord"):
        assert reviewer[field] is None, field
    if ankibug_schema is not None:
        ankibug_schema.validate(ticket)
    shutil.rmtree(tdir, ignore_errors=True)


def test_hand_built_fallback_matches_ankibug_backed_shape():
    tdir = _tmp_tickets_dir()
    _reset_mw()
    real_schema = br._schema
    try:
        br._schema = None  # force the hand-built path
        ticket = _run_create_ticket("Fallback path", "expected text", False)
    finally:
        br._schema = real_schema
    assert ticket["schema"] == 1
    for top in ("id", "created", "source", "note", "status", "kind",
                "app", "reviewer", "deck_build", "capture", "fix", "expected"):
        assert top in ticket
    if ankibug_schema is not None:
        ankibug_schema.validate(ticket)
    shutil.rmtree(tdir, ignore_errors=True)


def test_unreachable_webview_still_writes_a_ticket():
    tdir = _tmp_tickets_dir()
    _reset_mw(web=_FakeWebview(raises=True))
    ticket = _run_create_ticket("Can't reach any webview right now", "", False)
    capture = ticket["capture"]
    assert capture["qa_html_path"] is None
    assert capture["visible_text"] == ""
    assert capture["console_errors"] == []
    if ankibug_schema is not None:
        ankibug_schema.validate(ticket)
    shutil.rmtree(tdir, ignore_errors=True)


def test_index_md_accumulates_rows_and_escapes_pipes():
    tdir = _tmp_tickets_dir()
    _reset_mw()
    t1 = _run_create_ticket("First bug", "", False)
    t2 = _run_create_ticket("Second bug | with a pipe", "", False)

    index_path = os.path.join(tdir, "index.md")
    with open(index_path) as fh:
        lines = [ln for ln in fh.read().splitlines() if ln.startswith("|")]
    assert lines[0] == "| id | created | source | kind | status | note |"
    assert lines[1] == "| --- | --- | --- | --- | --- | --- |"
    assert len(lines) == 4, f"expected header + separator + 2 ticket rows, got {lines}"
    assert t1["id"] in lines[2]
    assert t2["id"] in lines[3]
    assert "Second bug \\| with a pipe" in lines[3]

    shutil.rmtree(tdir, ignore_errors=True)


def test_slugify_and_make_id_shape():
    slug = br._slugify("Answer buttons vanished after Undo!!")
    assert slug == "answer-buttons-vanished-after-undo"
    assert br._slugify("") == "ticket"
    when = datetime.datetime(2026, 9, 22, 21, 45, 3)
    tid = br._make_id("Weird crash", when)
    assert tid.startswith("20260922-214503-")


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in tests:
        fn()
    print(f"PASS test_bug_report ({len(tests)} tests)")
