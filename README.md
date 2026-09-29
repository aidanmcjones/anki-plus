# ankibug

Bug-ticket capture library and CLI for Anki+ (the `~/dev/anki` fork run
with the `anki-design` add-on). Files a ticket under
`~/AnkiTickets/<id>/` with whatever context it can grab
from a running Anki+ over its Chromium remote-debugging port
(`http://127.0.0.1:8080/json`), and degrades gracefully to a bare
ticket when Anki+ isn't running. Never touches the Anki collection.

See `SCHEMA.md` for the ticket JSON schema (v1).

## Install

```sh
python3 -m venv ~/.venvs/ankibug
~/.venvs/ankibug/bin/pip install -e .
```

This installs the `ankibug` console script into `~/.venvs/ankibug/bin/`.
Put that on your `PATH`, or call it by full path, or symlink it:

```sh
ln -sf ~/.venvs/ankibug/bin/ankibug /usr/local/bin/ankibug
```

## Usage

File a ticket, capturing live context from Anki+ if it's running:

```sh
ankibug "editor loses focus after Ctrl+Z on a cloze note"
ankibug "scroll position resets in reviewer" --kind app
ankibug "week 4 deck build produced 0 cards" --kind deck
```

Attach a card/note id from the reviewer (not auto-detectable from the page):

```sh
ankibug "answer side renders blank" --card 1758577200123 --note 1758500000456
```

Attach an existing screenshot instead of (or alongside) a live capture:

```sh
ankibug "layout breaks on narrow window" --screenshot ~/Desktop/shot.png
```

File from a chat hand-off (sets `source=chat`, attaches the given image
as the ticket's screenshot):

```sh
ankibug --from-screenshot ~/Desktop/shot.png "this is what I'm seeing"
```

List, inspect, and update tickets:

```sh
ankibug list
ankibug show 20260922-214503-editor-loses-focus
ankibug set 20260922-214503-editor-loses-focus status=fixing
```

`ankibug set` accepts dotted keys for nested fields, e.g.
`ankibug set <id> fix.summary="switched to blur handler"`.

## What gets captured

When Anki+'s CDP port answers, ankibug connects to the "main webview"
page over a raw CDP websocket (Playwright's `connectOverCDP` does not
work against QtWebEngine, so this is hand-rolled JSON-RPC over the
websocket, using the `websockets` package's sync client) and grabs:

- `document.getElementById('qa')?.outerHTML` -> `qa.html`
- `document.body.innerText` -> `capture.visible_text`
- `window.__baReviewerState` -> `reviewer.state` (`question`/`answer`)
- `window.__baErrors` (an in-app ring buffer, if the anki-design hotkey
  worker maintains one) plus ~1s of `Runtime`/`Log` console events ->
  `capture.console_errors`
- a full-page PNG via `Page.captureScreenshot` -> `screenshot.png`

It also records anki-design's git branch/commit/dirty state (from
`~/dev/anki-design`) and the Anki version (from `~/dev/anki/.version`)
into `ticket.json`'s `app` block.

If the CDP port isn't reachable, or a page/global isn't there, the
corresponding field is just `null`/empty and the ticket is still
written — capture failures are never fatal.

## Layout on disk

```
~/AnkiTickets/
  index.md                  <- regenerated in full on every write
  <id>/
    ticket.json
    screenshot.png          <- if a screenshot was captured/attached
    qa.html                 <- if a #qa element was found
    capture.log             <- human-readable capture/CLI log
```

## Development

```sh
~/.venvs/ankibug/bin/pip install -e . pytest
~/.venvs/ankibug/bin/pytest
```

Tests cover schema validation, the on-disk store/index, and CDP
capture against a fake `/json` + websocket server (no running Anki+
required).

## ankiship: grouped commits and standing PRs

`ankiship` turns uncommitted work into clean, grouped history. Each changed
file is classified to an **area** (browse, sidebar, reviewer, editor,
backdrop, fullscreen, scheduling, stats, search, settings, stability, theme,
tags, bug-report, build, docs, test-harness; the engine profile has its own
set) and each area group to a conventional **type** (feat, fix, perf,
refactor, test, docs, build, chore). A test file joins the area it tests.

For every `(type, area)` group it:

1. commits locally on the current branch as `type(area): summary`, so the
   live checkout ends clean;
2. cherry-picks that commit onto the standing branch `auto/<type>/<area>`
   (fast-forward only, never forced) and pushes it;
3. keeps exactly one open PR per branch into the base branch, labelled
   `type:<type>` and `area:<area>`, with a running commit list.

It never pushes main, master or the base branch, and never merges. A commit
that does not cherry-pick cleanly stays local and is reported as `conflict`.

```bash
ankiship plan ~/dev/anki-design                  # dry run: show the groups
ankiship run  ~/dev/anki-design                  # commit, push, open/update PRs
ankiship run  ~/dev/anki-design --type perf -m "cache deck tree"
ankiship install-agent ~/dev/anki-design         # every 15 min, files quiet 10 min
```

Per-repo settings live in git config: `ship.base`, `ship.prefix`,
`ship.profile` (`addon` or `engine`), `ship.labels false`,
`ship.pushCurrent false`. For the engine checkout:
`git config ship.base engine/main && git config ship.prefix engine/auto/`.

ankifix calls it automatically (`auto_ship`, on by default): when the watcher
auto-applies a ticket and it lands `fixed`, exactly the files in the ticket's
patch ship as `fix(<area>): <ticket note>` with a `Ticket:` trailer.
