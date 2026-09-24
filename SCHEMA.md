# ankibug ticket schema (v1)

Every ticket is a single JSON object stored at
`~/AnkiTickets/<id>/ticket.json`.

`<id>` = `YYYYMMDD-HHMMSS-<slug of note>` where `<slug of note>` is the
first ~40 characters of the user's note, lowercased, non-alphanumerics
collapsed to single hyphens, leading/trailing hyphens stripped. If the
note is empty the slug falls back to `ticket`.

## Top-level fields

| field | type | notes |
|---|---|---|
| `schema` | int | always `1` for this version |
| `id` | str | see above |
| `created` | str | ISO 8601 local time, e.g. `2026-09-22T21:45:03-05:00` |
| `source` | str | one of `hotkey`, `terminal`, `chat` |
| `note` | str | the user's free-text description of the bug |
| `status` | str | one of `new`, `fixing`, `fixed`, `needs-review`, `failed`, `wontfix` |
| `kind` | str | one of `app`, `deck`, `unknown` |
| `app` | object | see below |
| `reviewer` | object | see below |
| `deck_build` | object or `null` | see below |
| `capture` | object or `null` | see below |
| `fix` | object or `null` | see below |
| `manual_set` | object, optional | written by `ankibug set`: `{"at": ISO time, "fields": [keys]}`. ankifix never overwrites a change stamped after its run started (the run's result goes to `fix.result_ignored`) |

ankifix adds optional `fix` keys beyond the required ones: `owner`
(`watcher`, `cli`, `terminal`), `pid`, `attempts`, `delegated`,
`delegated_at`, `kind_flip`, `result_ignored`, `blocked`, `error_log`.
`fix.log_path` names the latest per-attempt transcript, `fix.<n>.log`.

### `app`

Environment info about the Anki+ / anki-design checkout at ticket time.

```json
{
  "anki_version": "26.08.1",
  "addon_repo": "~/dev/anki-design",
  "branch": "video-backdrop",
  "commit": "de48ca29fdcbb7f77c761be1adb66319a0cd2c72",
  "dirty": true
}
```

All fields may be `null` if they could not be determined (e.g. git not
available, `.version` file missing).

`status: needs-review` (set only by `ankifix`, for `kind: app` tickets) means
a fix branch exists, has commits, and Claude's own result block says
`status: fixed` - but `tests_green` in that block is `false`, so the harness
(the fixer's own new test, or pre-existing unrelated failures it could not
clear) isn't fully green. It is a completed-ish state like `fixed`:
re-running the ticket needs `--force`.

### `reviewer`

Reviewer/browser context. Card/note ids are not reachable from the
captured page, so they are only populated when passed explicitly via
`--card` / `--note` on the CLI.

```json
{
  "state": "question",
  "card_id": 1234567890,
  "note_id": 987654321,
  "notetype": null,
  "deck": null,
  "template_ord": null
}
```

`state` is one of `"question"`, `"answer"`, or `null` when unknown
(read from `window.__baReviewerState` in the main webview when present).

### `deck_build`

Only relevant for `kind: "deck"` tickets (e.g. class-anki-deck runs).
`null` when not applicable. Currently populated only when the caller
supplies course/build info; ankibug itself does not infer this yet.

```json
{
  "course_dir": "/path/to/course",
  "build_marker": "unit-3.apkg"
}
```

### `capture`

Populated when a live capture against Anki+'s CDP port succeeded;
`null` when Anki+ was not reachable at capture time.

```json
{
  "pages": [{"title": "main webview", "url": "http://127.0.0.1:40000/congrats#night"}],
  "qa_html_path": "qa.html",
  "visible_text": "...",
  "console_errors": ["TypeError: ..."],
  "screenshot_path": "screenshot.png",
  "captured_at": "2026-09-22T21:45:04-05:00"
}
```

`qa_html_path` and `screenshot_path` are relative to the ticket
directory. `pages` lists every page ankibug saw on the CDP `/json`
endpoint at capture time (title + url only).

### `fix`

Populated by whatever downstream worker fixes the bug; ankibug itself
only ever writes `null` here (or leaves an existing value alone via
`ankibug set`). `null` until a fix is started.

```json
{
  "branch": "fix/ticket-slug",
  "commits": ["abc1234"],
  "tests": ["pytest tests/test_foo.py"],
  "log_path": "capture.log",
  "started": "2026-09-22T21:50:00-05:00",
  "finished": null,
  "summary": null,
  "applied": null
}
```

`applied` (object or `null`, `kind: app` only) is written by
`ankifix apply` / `ankibug`'s `auto_apply` watch step, after `fix.branch` has
been patched into the live add-on checkout:

```json
{"at": "2026-09-22T22:05:00-05:00", "patch": "apply.patch", "tests_passed": true}
```

plus, when the fix lists live tests (`fix.live_tests`, tests/live/test_*.py
run in the real app by the add-on's harness), `live_tests_passed` (bool) and
`live_tests` (one `{"test", "ok", "rc", "command", "output"?}` per test),
and `landed`: `{"state": "restarted" | "loaded" | "pending" | "not-running"
| "failed" | "gave-up" | "not-landed", "at", "reason"}`.

or, if `git apply --check` failed against the checkout's current working
tree:

```json
{"at": "2026-09-22T22:05:00-05:00", "error": "patch does not apply: ..."}
```

## On-disk layout

```
~/AnkiTickets/
  index.md                        <- table of all tickets
  <id>/
    ticket.json                   <- the schema above
    screenshot.png                <- present iff capture succeeded and a screenshot was taken
    qa.html                       <- present iff capture succeeded and a #qa element was found
    capture.log                   <- human-readable capture/CLI log for this ticket
```

## `index.md`

A single markdown table, regenerated in full on every write, columns:
`id | created | source | kind | status | note` (note truncated to
~60 chars, pipe characters escaped).
