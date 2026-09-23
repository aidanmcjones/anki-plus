# Live tests: the add-on inside the real Anki app

Unit tests under `tests/` stub `aqt`. They prove the Python logic, not that a
user can click, drag, or see anything. A live test runs inside the REAL Anki
app (the fork at `~/dev/anki`, same `tools/run.py` entry the Anki+ launcher
uses), with this add-on loaded, real widgets, and real webviews. If a live
test passes, the behaviour works in the app; if it fails, the user would see
the failure too.

## Run one

```sh
cd ~/dev/anki && out/pyenv/bin/python ~/dev/anki-design/tests/live/run_in_app.py \
    ~/dev/anki-design/tests/live/test_smoke.py
```

From a worktree, call that worktree's copy of `run_in_app.py`; it loads the
add-on code from the checkout it lives in (override with `--addon-dir`).
Several scripts may be given; each gets its own app launch.

Options: `--timeout S` (default 120 per test), `--keep` (keep the temp base
folder for inspection), `-v` (print the app log tail on failure),
`--anki-src` (default `~/dev/anki` or `$ANKI_SRC`).

Output: one `PASS`/`FAIL <script>: <check> -- <detail>` line per check,
`NOTE` lines, then `PASSED`/`FAILED <script>: N checks, M failed, app runtime
Xs`. Exit 0 only when every check passed; 1 when a check failed or the test
raised; 2 on timeout or no result; 3 for a missing script.

Runtime: about 3 s for the smoke test and 7 s for the deck reorder test on
this Mac (app launch included).

## What the harness does

1. `mkdtemp` base folder with `prefs21.db` (first-run language answered,
   update checks off) and one profile, `User 1`, whose collection holds the
   fixture: decks `Parent`, `Parent::A`, `Parent::B`, `Parent::C`, `Solo`,
   two new Basic cards in each leaf.
2. `<base>/addons21/anki-design/` is a folder of symlinks to this add-on's
   files. `meta.json`, dotfiles, `tests/`, `out/`, `docs/`, `user_files/` are
   not linked, so config writes go to the temp folder and never to the
   checkout or the user's live settings. No other add-ons are loaded.
3. The app starts with `QT_QPA_PLATFORM=offscreen`, `-b <base>`, a private
   `ANKI_SINGLE_INSTANCE_KEY` (so it never contacts the user's running
   Anki+), and without `QTWEBENGINE_REMOTE_DEBUGGING` / `ANKI_API_PORT`
   (no port clash with the live app). The user's profile is never opened.
4. The add-on's `live_test.py` sees `ANKI_DESIGN_LIVE_TEST`, waits for the
   deck browser to finish loading, runs the script's `run(t)`, writes
   `ANKI_DESIGN_LIVE_RESULT`, and closes the app.
5. The harness prints the result and deletes the base folder (always, even
   on timeout, unless `--keep`). On timeout it stops only its own child
   process group.

## Env vars

| var | set by | meaning |
| --- | --- | --- |
| `ANKI_DESIGN_LIVE_TEST` | harness | absolute path of the script to run in the app |
| `ANKI_DESIGN_LIVE_RESULT` | harness | where the app writes the result JSON |
| `ANKI_SINGLE_INSTANCE_KEY` | harness | private key so the test app is its own instance |
| `QT_QPA_PLATFORM=offscreen` | harness | no window on screen |
| `ANKI_SRC` | you (optional) | fork location if not `~/dev/anki` |

Without `ANKI_DESIGN_LIVE_TEST` the add-on's live-test hook does nothing.

## Writing a live test

A script under `tests/live/` named `test_*.py` with `run(t)`:

```python
def run(t):
    names = t.deck_list_names()
    t.check("Solo is listed", "Solo" in names, names)
```

`t` (see `live_test.py`):

- `t.mw` the real main window; `t.app` the QApplication; `t.QTest`.
- `t.check(name, ok, detail)` records one PASS/FAIL line. Record a check for
  every observable outcome; a script with no checks fails.
- `t.note(msg)` adds context to the output.
- `t.js(code, web=None)` evaluates JS in a real webview (default `mw.web`)
  and returns the JSON-able result; `t.wait_js(expr, timeout)` polls until
  truthy.
- `t.pump(ms)` runs the real event loop; `t.wait_until(pred, timeout)`.
- `t.element_center(css, frac_y)` widget coordinates of a point inside an
  element; `t.native_drag(web, p0, p1)` a real mouse press/move/release
  through `QWindowSystemInterface` (QTest's QWindow overloads).
- `t.deck_list_names()` deck names in on-screen order on the home list.

Rules for a good live test:

- Drive the UI the way the user does (clicks, keys, pointer events), then
  assert on what the user would SEE after the app reacts: the re-rendered
  DOM, widget state, collection contents. Asserting that a handler was
  called is not enough.
- Write it first and watch it fail on the unfixed code; that failure is
  the proof the test sees the bug.
- Open other screens through the app (`t.mw.onBrowse()`, `t.mw.onAddCard()`,
  `t.mw.moveToState("review")`) and find their widgets/webviews from `t.mw`
  or `aqt.dialogs`.

## Limits (what offscreen cannot cover)

- The OS drag session. Under `offscreen`, Qt's `QOffscreenDrag` ends every
  `QDrag` immediately, so an HTML5 drag-and-drop started by Chromium gets
  `dragstart` then `dragend` with no `dragover`/`drop`. A pointer-event
  (mousedown/move/up) drag IS fully exercised. For HTML5 drags, test the
  page logic with dispatched drag events and the Qt hand-off by calling the
  webview's `dragEnterEvent`/`dropEvent` (see `test_deck_reorder.py`).
- Pixels: offscreen renders, but screenshots are not compared here.
- macOS menu bar, native dialogs' look, and focus between real windows.

## Current tests

- `test_smoke.py`: the app starts on the deck list, the add-on is loaded,
  and the home webview lists the fixture decks in tree order.
- `test_deck_reorder.py`: dragging a deck onto a sibling's top edge
  reorders it in the re-rendered list (page events), and, since the list
  uses HTML5 drag-and-drop, that the home webview hands drag enter / drop to
  the web engine while a file dragged from Finder still goes to Anki's
  import. Before `webview_drops.py` wrapped `MainWebView.dragEnterEvent` /
  `dragMoveEvent` / `dropEvent` (fix/drag-regressions), the Qt checks
  FAILED: Anki treats every drag on the deck browser as a file import and
  swallows a deck drag, so a real drag never delivered `dragover`/`drop`.
- `test_browse_card_drag.py`: opens Browse inline, drags cards with the
  real mouse from the real table (the add-on starts a real QDrag); the
  OS drag loop is stood in for by delivering DragEnter/Move/Drop to the
  widget under the pointer, as QWidgetWindow does. Checks what the user
  sees: a group dropped on a sidebar deck moves and leaves the list, an
  unselected row can be pulled, a review-row drop explains itself, and a
  new-on-new drop repositions. On 06d7d9f three of its checks fail.
