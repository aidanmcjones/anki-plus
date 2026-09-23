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

## Required procedure (output contract)

1. **Reproduce first.** Find the responsible code and write a test that
   fails on the current code because of this bug: extend
   `tests/editor_tools.cjs` or `tests/reviewer_click.cjs` (headless
   Playwright against `web/*.js`/`web/*.css`), or add a
   `tests/test_<topic>.py` for Python logic. Run it and see it fail.
2. **Fix** the bug with the smallest change that makes that test pass.
3. **Keep everything green**: run every command in the test harness list
   above after the fix. All must pass.
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
  never start, stop, or restart Anki+ or any Anki process.
- No network access other than the `git push` above.

## Final message

End your final message with a fenced block tagged `ankifix-result`
containing one JSON object, exactly this shape:

```ankifix-result
{"status": "fixed or failed", "tests_green": true, "tests": ["node tests/reviewer_click.cjs", "..."], "reproduced": true, "pushed": true, "summary": "2-4 sentences: root cause, fix, test added"}
```

Style rule: never use em dashes anywhere you write (code comments, commit messages, docs, summaries). Use a comma, a colon, or a new sentence instead.

Pull requests: never open a pull request (no `gh pr create`, no PR via the API). Push your fix branch only; the maintainer lands it on the one open PR for the base branch.
