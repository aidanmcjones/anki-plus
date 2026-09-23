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
| `status` | str | one of `new`, `fixing`, `fixed`, `failed`, `wontfix` |
| `kind` | str | one of `app`, `deck`, `unknown` |
| `app` | object | see below |
| `reviewer` | object | see below |
| `deck_build` | object or `null` | see below |
| `capture` | object or `null` | see below |
| `fix` | object or `null` | see below |

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
  "summary": null
}
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
