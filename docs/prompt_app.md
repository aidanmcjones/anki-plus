# ankifix: fix an Anki+ (anki-design add-on) bug headless

You are running non-interactively under `ankifix`. Nobody will answer
questions; make reasonable calls and record them in your final report.

## Where you are

- Working directory: `${worktree}` (a fresh git worktree of
  `${app_repo}`, branch `${branch}`, cut from `${base_branch}`).
- Work ONLY inside this worktree. The ticket directory `${ticket_dir}` is
  readable (qa.html, screenshot) but do not modify it.
- Read `CLAUDE.md` in the worktree before you start. Its sections:
${claude_md_pointers}

## The ticket

User's note: ${note}
Expected behaviour: ${expected}

Full ticket.json:

```json
${ticket_json}
```

### Captured reviewer state

- Screenshot: ${screenshot_path}  (use the Read tool on the PNG to look at it)
- qa.html: ${qa_html_path}
- Console errors:
${console_errors}

Visible text at capture time:

```text
${visible_text}
```

qa.html (the reviewer `#qa` element at capture time, truncated to ${qa_limit} chars):

```html
${qa_html}
```

## Test harness

Run from the worktree root. `NODE_PATH` is already exported in your
environment (`${node_path}`), so run node tests plainly:

${test_commands}

The following are known, pre-existing failures unrelated to any fix (they
fail or hang for reasons outside this ticket, e.g. missing test
infrastructure). They are excluded from tests_green automatically, so do not
spend turns trying to make them pass unless your fix specifically touches
the code they cover:
${known_failing_tests}

## Live tests: proof in the real app (REQUIRED)

Unit tests here stub `aqt` (fake Qt) or run page scripts in a bare
Chromium. They have passed for "fixes" that did nothing in the real app,
because the real bug lived in the layer the stubs replace (Qt event
handling, the real webview, the real deck browser). **They are not proof.**

For any ticket about behaviour the user sees or does (clicks, drags, keys,
scrolling, rendering, layout, dialogs, menus, settings, anything on
screen), you MUST add a live test under `tests/live/` that runs inside the
REAL Anki app, and it MUST fail before your fix and pass after it.

Run a live test from the worktree root with exactly this command (it
launches a throwaway, offscreen copy of the real app with a fixture
collection and this worktree's add-on code; it never touches the user's
running Anki+ or profile):

```
${anki_pyenv} ${live_harness} tests/live/test_<name>.py
```

It prints `PASS`/`FAIL` per check and exits non-zero on any failure (each
test is capped at ${live_test_timeout} s). Read `tests/live/README.md` for
how to write one (a `run(t)` function; `t.mw`, `t.js(...)`, `t.QTest`,
`t.native_drag(...)`, `t.check(name, ok, detail)`), the fixture decks, and
what offscreen cannot cover. Model it on `tests/live/test_deck_reorder.py`:
drive the UI the way the user does, then assert on what the user would see
after the app reacts (the re-rendered DOM, widget state, the collection),
not on whether a handler was called.

If `tests/live/run_in_app.py` does not exist in this worktree, say so in
your summary and report `"live_tests": []`; the ticket will be held for
review.

## Required procedure (output contract)

1. **Reproduce first.** Find the responsible code and write a test that
   fails on the current code because of this bug. For user-visible
   behaviour that means a live test in `tests/live/test_<topic>.py` (see
   above), run with the harness command, and seen to FAIL. Unit tests
   (`tests/editor_tools.cjs`, `tests/reviewer_click.cjs`, headless
   Playwright against `web/*`, or `tests/test_<topic>.py` for Python logic)
   are welcome in addition, never instead.
2. **Fix** the bug with the smallest change that makes those tests pass.
3. **Keep everything green**: run every command in the test harness list
   above after the fix, and run your live test(s) again with the harness
   command. All must pass.
4. **Commit** on `${branch}` with a message that explains the bug and the
   fix, ending with exactly this line:
   `${co_author}`
   Use plain `-m` flags (e.g. `git commit -m "subject" -m "body" -m "${co_author}"`);
   heredocs and `$(...)` substitutions may be denied by the permission rules in this
   headless session.
5. **Push** the branch: `git push -u ${remote} ${branch}`.
6. If you cannot reproduce or cannot fix it, commit nothing and say why.

## Hard rules

- Never touch the main checkout `${app_repo}` or any other worktree.
  Never check out or modify `${base_branch}`. Never force-push.
- Never open, read-write, or import into the live Anki collection
  (`~/Library/Application Support/Anki2/`), never run `make run`/`make dev`,
  never start, stop, or restart Anki+ or any Anki process. The only Anki
  you may start is the throwaway offscreen instance the live harness
  command above launches (and stops) itself.
- No network access other than the `git push` above.

## Final message

End your final message with a fenced block tagged `ankifix-result`
containing one JSON object, exactly this shape:

```ankifix-result
{"status": "fixed or failed", "tests_green": true, "tests": ["node tests/reviewer_click.cjs", "..."], "live_tests": ["tests/live/test_<name>.py"], "reproduced": true, "pushed": true, "summary": "2-4 sentences: root cause, fix, tests added, and the live test's FAIL-before / PASS-after"}
```

`status` is only ever `fixed` or `failed` here. Never return `wrong-kind`:
this ticket may already have been re-routed to you by the deck fixer, and it
is never routed back. If you think it is really a course card content
problem, return `failed` and say so in `summary`.

`live_tests` lists the tests/live/ scripts that prove this fix in the real
app. After your fix is applied to the live checkout, ankifix re-runs each of
them through the harness; if any fails (or the list is empty), the ticket
stays needs-review and the fix does NOT land in the user's app.

Style rule: never use em dashes anywhere you write (code comments, commit messages, docs, summaries). Use a comma, a colon, or a new sentence instead.

Pull requests: never open a pull request (no `gh pr create`, no PR via the API). Push your fix branch only; the maintainer lands it on the one open PR for the base branch.
