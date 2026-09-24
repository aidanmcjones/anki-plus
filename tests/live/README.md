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
| `QT_QPA_PLATFORM=offscreen` | harness | no window on screen (`cocoa` for opt-in display tests) |
| `ANKI_SRC` | you (optional) | fork location if not `~/dev/anki` |
| `ANKI_LIVE_DISPLAY=1` | you (opt-in) | run `NEEDS_DISPLAY` scripts on the real display (`QT_QPA_PLATFORM=cocoa`) |

Without `ANKI_DESIGN_LIVE_TEST` the add-on's live-test hook does nothing.

## Real-display tests (opt-in)

A script with `NEEDS_DISPLAY = True` at module level needs the real screen
(for example macOS full screen, which offscreen cannot do). The harness
prints `SKIP <script>` and exits 0 for it unless `ANKI_LIVE_DISPLAY=1` is
set; with it, the throwaway app runs with `QT_QPA_PLATFORM=cocoa`, so a
real window appears and the test may go full screen and move the cursor.
Everything else is unchanged (temp base, private instance key, fixture
profile; the user's Anki+ and profile are never touched). Run one only
with the user's consent and while they are away from the Mac:

```sh
cd ~/dev/anki && ANKI_LIVE_DISPLAY=1 out/pyenv/bin/python \
    ~/dev/anki-design/tests/live/run_in_app.py \
    ~/dev/anki-design/tests/live/test_fullscreen_display.py
```

Posted mouse events need Accessibility permission and reading screen pixels
needs Screen Recording permission for the process that launches the
harness (System Settings > Privacy & Security). If the bar never reveals the
test says Accessibility is the likely cause; if it reveals and the content
does not move, it says that instead. A locked screen fails fast. Set
`ANKI_FS_DIAG_OUT=<file.json>` to keep every sample.

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

- `test_fullscreen_display.py` (opt-in, `ANKI_LIVE_DISPLAY=1`, arm64): measures
  the windowed title bar (frame minus content view, 28 pt) and the top of
  the content as pixels, enters macOS full screen, then for 1.5 s with the
  cursor away asserts no NSToolbar is attached and the bar is closed (AppKit's
  bar window off screen, content not offset, the content's top pixels equal
  the windowed content's top). Moves the cursor to the top edge with Quartz
  (CGWarpMouseCursorPosition plus a posted kCGEventMouseMoved, through
  ctypes), then back to mid-screen, sampling about every 40 ms: the visible
  bottom of AppKit's bar (its title bar container) and where the app's
  pixels are, measured on screen by matching a strip over the sidebar
  against the baseline. Asserts the content moved down exactly once,
  animated, stayed, slid back to 0 when the bar hid, matched the bar's
  reveal in every sample (lockstep), and that the revealed bar is the plain
  title bar height (within 2 pt). Restores the cursor and leaves full screen
  with the content view back in place. `ANKI_FS_DEACTIVATE=1` runs only the
  at-rest part with Finder in front (macOS then shows another Space, so
  only the AppKit state is compared, not pixels).

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
- `test_browse_card_reposition.py`: with the real Browse table sorted by
  position, drags cards between rows with the real mouse and, between the
  drag move and the drop, reads the marker widget the add-on shows over
  the table. Checks what the user sees: a 2px insertion line on the
  hovered row's top edge (upper half) or bottom edge (lower half) and no
  box around the row; after the drop, the re-searched rows show the whole
  selection at the line in queue order, before or after that row, a
  hovered row that is itself selected included; the tooltip counts every
  card; a drop on the card's own row changes nothing. On 93f1d3a nine of
  its checks fail (34px box marker, a lower-half drop lands above the
  row, the hovered card is left out of the group).
- `test_browse_reposition_ties.py`: with the real Browse table sorted by
  position, gives cards tied positions through the collection (two at
  #25 after the dragged pair, then all six at #25, as the user's deck
  was) and drags with the real mouse onto the line between two tied
  rows, above the second, and below the fifth of six. Checks what the
  user sees: the re-searched rows show the cards between the two rows
  they were dropped between, every card in the list has a position of
  its own, the tooltip counts the dragged cards only, and cards ahead of
  the run outside the table keep their positions. On 5d22099 six of its
  checks fail: the pair lands after both tied rows, and a single card
  dropped above the second tied row leaves the list exactly as it was.
- `test_browse_sweep_select.py`: press on an unselected row of the real
  Browse table and sweep down the list with the real mouse; the rows
  crossed must end up selected and no card drag may start, then the
  swept group dragged onto a sidebar deck moves there, and a sideways
  pull on an unselected row still drags that one card. Its drag stand-in
  owns the pointer from the moment a drag starts (the rest of the gesture
  becomes drag events and the release is eaten, as Qt's drag loop does),
  which is what makes the bug visible: on dfaefaa the sweep started a
  drag and selected nothing.
- `test_sidebar_tag_restudy.py`: tags the fixture notes (alpha, beta,
  beta::kid, gamma), selects alpha and beta in the real Browse sidebar with
  the real mouse (click, Cmd-click) and right-clicks through
  `onContextMenu`. Checks "Restudy 2 tags" is the first item with a
  separator under it, the Restudy filtered deck holds every card with
  either tag including the child tag, the reviewer shows one of them and
  the tooltip counts them; then one tag with a suspended card says "1 left
  out". On 9b5aa17 the menu has no Restudy item.
- `test_sidebar_tag_drag.py`: with seven tags in the real sidebar, sweeps
  down from an unselected tag with the real mouse (the run is selected,
  no drag starts), drags the selected group onto the top edge of a tag,
  one tag onto a bottom edge, a Cmd-click pair onto the line above a
  child of another parent (reparented, full names change) and one tag
  into the middle of another (nests). Between move and drop it reads the
  add-on's marker over the viewport and the viewport's pixels: a 2px
  accent line on the hovered edge, a box for the middle. After each drop
  it reads the refreshed sidebar and `tag_order`. Deck rows: a pull from a
  deck still starts the tree's own drag, and a deck drop passes the
  add-on's filters to Anki's `dropEvent` and reparents (delivered straight
  to the tree, since a synthetic drag has no QDrag source and an
  InternalMove view refuses it otherwise). On 9b5aa17 the sweep drags
  `delta` into `gamma` and 11 of its checks fail before it stops.
- `test_sidebar_resize.py`: hover the left rail's right border with the
  real mouse (a resize handle with a col-resize cursor must be there), then
  drag it 80px right. Checks what the user sees: the rail widens with every
  pointer move, the page content shifts by the same amount, the width is in
  the add-on config (`sidebar_width`) and in `addcard.sidebar_w()`, it
  survives a deck-list re-render and a collapse/expand round trip, and with
  Browse open inline a drag on the edge moves the Qt overlay too. On
  c7b48c1 (no handle, fixed 264px rail) every check fails.
- Both Browse tests wait for the embed's opening curtain (a QFrame that
  covers the table for ~0.9 s) to drop before pressing; a press before
  that lands on the curtain and reaches nothing.
- `test_render_memory.py`: opens the real reviewer on a 40-card deck with
  the FunBiochem-E1 note type CSS (read from the Exam 1 .apkg when it is
  on disk, else an embedded copy: px sizes, sub/sup, a table, a
  slide-sized answer image) and answers 300 card views (show answer,
  Good). After 10 views and every 50 it reads the page's total `<style>`
  text, CSSOM rule count, DOM element count, JS heap and the renderer's
  RSS; style text, rules and elements must stay within 5 percent of the
  10-view values and the heap within 20 MB. `RF_MEM_VIEWS` changes the
  count. It passes on 81ed6d5 too: the page was not growing (ticket
  20260923-153047).
- `test_renderer_recovery.py`: SIGKILLs the real renderer process under the
  reviewer and checks the page is live again, still in the reviewer, on
  the same unanswered card, and the answer can be revealed; the same for
  the deck list; repeated deaths stop being reloaded; and with the page
  dead for good, the bug-report capture files a ticket within 5 s with
  "webview unresponsive". On 81ed6d5 six of its checks fail: the window
  stays a dead page and the bug report waits on it.
