# ankifix

`ankifix` takes a ticket written by `ankibug` (`~/AnkiTickets/<id>/ticket.json`,
see `SCHEMA.md`) and runs Claude Code headless (`claude -p`) to fix it.

```
ankifix <id>                       # fix one ticket
ankifix --dry-run <id>             # print the prompt (command on stderr); change nothing
ankifix --watch [--interval 30]    # fix status=new tickets one at a time, oldest first
ankifix --watch --once             # drain the queue once and exit
ankifix --watch --notify|--no-notify  # override the notify config key for this run
ankifix --reindex                  # regenerate ~/AnkiTickets/index.md
ankifix apply <id>                 # apply an app ticket's fix branch to the live checkout
ankifix install-agent [--interval 20]  # write+load the launchd LaunchAgent for --watch
ankifix uninstall-agent            # unload+remove the LaunchAgent
  --kind app|deck                  # override classification
  --max-turns 60  --timeout-min 25 --max-budget-usd X  --model M  --force
```

Install: `pip install -e ~/dev/ankibug` (editable: the prompt templates are read
from `docs/prompt_app.md` and `docs/prompt_deck.md` in this repo).

## Classification

1. `--kind` wins. 2. A ticket whose `kind` is already `app`/`deck` keeps it.
3. `deck` if `reviewer.notetype` starts with a course prefix (`CourseB-`, `Micro`).
4. Otherwise keyword scoring on `note` + `expected`: deck words (card, deck,
   answer, blank, cloze, image on card, ...) against app words (editor, reviewer
   UI, deadline, hotkey, crash, button, ...). App surfaces that contain deck words
   ("deck browser", "answer button", "add card") are removed before deck words
   are counted. Console errors add one point to app. Ties go to app.

All lists live in `ankifix/config.py` and can be overridden in
`~/.config/ankifix/config.json` (any `Config` field, e.g.
`{"course_notetype_prefixes": ["CourseB-", "Micro", "Genetics-"]}`).

`ankifix.classify.classify()` is also called by `ankibug` itself at capture
time (`ankibug/cli.py:_classify_kind`), not just by `ankifix` at fix time: a
hotkey/chat ticket that arrives with no explicit `--kind` is classified
immediately with the same keyword rules and stored as `app`/`deck`, not left
at `kind: unknown` until someone runs `ankifix` on it. `ankibug list` and
`index.md` only ever showed whatever `kind` was on disk; this doesn't change
their rendering, it changes what gets written. Tickets already on disk with
`kind: unknown` are left alone; `unknown` is still a valid, accepted `kind`.

## App tickets

- Worktree `~/dev/anki-design/.claude/worktrees/fix-<id>` on new branch
  `fix/<id>` cut from local `video-backdrop` (reused if it already exists).
- Claude runs in the worktree with `NODE_PATH` exported to the npx Playwright
  install, `--add-dir <ticket dir>` so it can Read qa.html and the screenshot.
- Contract (docs/prompt_app.md): failing test first, fix, all tests green,
  commit ending `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`,
  `git push -u origin fix/<id>`, never touch the main checkout, the live
  collection, or Anki+ itself.
- `fixed` when the branch has commits past `video-backdrop` AND Claude's
  `ankifix-result` block says `status: fixed, tests_green: true` AND the wall
  clock did not expire.
- `needs-review` when everything above holds except `tests_green` is `false` -
  i.e. Claude produced a real fix branch and says it fixed the ticket, but the
  harness (its own tests, or pre-existing unrelated failures it couldn't
  clear) isn't fully green. This happened on the first real run: the fixer
  patched `web/reviewer.js`, added a passing regression test, and pushed, but
  two pre-existing failures on the branch (`editor_tools.cjs` timing out,
  `test_editor_crop.py` needing the Anki venv - see "Permission matching for
  compound commands" below) made `tests_green: false`. That is a fix branch
  worth a human look, not a silent `failed`.
- `failed` otherwise: no commits, no `ankifix-result` block, `status` in the
  block isn't `fixed`, or the wall clock expired.
- Re-running a `fixed` or `needs-review` ticket needs `--force`, same as
  `wontfix`/`fixing`.

## Deck tickets

- Claude runs in `deck_build.course_dir` (or its `Anki/build` subdir), default
  the Course B build dir, with the course root added so it
  can read `COURSE_DECK.md` §7-8. The prompt includes the best-matching
  `build_out.json` entries (matched on card ID or question text in the capture).
- Before the run, `cards/*.yaml` and `build_deck.py` are copied to
  `<ticket>/deck_before/`.
- Claude may only run `build_deck.py`, `verify.py`, `load_cards.py` with the
  fb-anki venv; `apply_*` and `fix_collection` are denied.
- `fixed` when a card file changed, `preflight_report.txt` was rewritten during
  the run and ends `RESULT: PASS`, and Claude reported green. `fix.branch` is
  null and `fix.commits` is `[]` (the build dir is not a git repo). The summary
  carries `APPLY WITH ANKI+ CLOSED: <command>` for you to run by hand.

## Claude invocation

```
claude -p --output-format stream-json --verbose --max-turns 60 \
  --permission-mode dontAsk --allowedTools <list> --disallowedTools <list> \
  --name "ankifix <id>" --add-dir <ticket dir> [--max-budget-usd X] [--model M]  < prompt
```

- `-p` with the prompt on stdin: headless, no argv length limit.
- `stream-json` + `--verbose` (required together in print mode): every event is
  streamed to `<ticket>/fix.log` as it happens, so a killed run still leaves a
  transcript; tool calls are parsed out of it to fill `fix.tests`.
- `--max-turns` is a hidden flag in 2.1.x (not in `--help`, present in the binary).
- `--permission-mode dontAsk`: anything not in `--allowedTools` is denied
  instead of prompting (nobody is there to answer). The allow list is git
  subcommands, `node *`, `python3 *`, `pytest*`, the Anki venv python for
  `tests/*` (see `anki_python` below), and read-only shell tools; the deny
  list is a second fence (force-push, checking out the base branch, `make`,
  `kill`, `rm -rf`, web tools, `git worktree`). See "Permission matching for
  compound commands" below for why a narrow `tests/*`-scoped allow rule
  isn't enough on its own once the fixer starts writing multi-step Bash
  calls.
- Wall clock: the process group is SIGTERMed then SIGKILLed after
  `--timeout-min` (default 25).

`fix.log` is JSONL: an `{"type":"ankifix","event":"start",...}` line with the
exact command, Claude's stream-json events, then an `"event":"end"` line.

## Permission matching for compound commands

The first real end-to-end run denied three commands from the fixer, even
though pieces of each looked covered by `app_allowed_tools`:

1. `git show 2a9016e:tests/reviewer_click.cjs > tests/reviewer_click.cjs && node tests/reviewer_click.cjs; echo "exit=$?"`
2. `grep -n "ANKI_VENV" Makefile | head -3; git status --short; ls "$HOME/dev/anki/.venv/bin/python" 2>/dev/null; grep -rn "playwright\|reviewer.js" tests/editor_tools.cjs | grep -c reviewer`
3. `"$HOME/Library/Application Support/AnkiProgramFiles/.venv/bin/python" tests/test_editor_crop.py`

**What I could verify** (fetched `https://code.claude.com/docs/en/permissions`
directly, quoted verbatim below - I do have live web access in this
environment, so this isn't guesswork):

> Claude Code is aware of shell operators, so a rule like `Bash(safe-cmd *)`
> won't give it permission to run the command `safe-cmd && other-cmd`. The
> recognized command separators are `&&`, `||`, `;`, `|`, `|&`, `&`, and
> newlines. A rule must match each subcommand independently.

and, on redirection:

> Output redirects: for `> file`, `>> file`, or `2> file`, the check covers
> your `Edit` allow and deny rules, protected paths, and the working
> directories.

So denial #1 is fully explained: the command splits into `git show ... >
tests/reviewer_click.cjs` (matches `Bash(git show*)`, and the redirect
target is inside the worktree so it clears the Edit/working-directory check
too), `node tests/reviewer_click.cjs` (matched `Bash(node tests/*)` even
before this hardening), and `echo "exit=$?"` - and `echo` is in Claude
Code's built-in read-only command set, which the same docs page says runs
"without a permission prompt in every mode". That reads as should-have-been-
allowed under the documented model.

Denial #3 is fully explained: at the time of that run, `app_allowed_tools`
had no rule at all for `.venv/bin/python`-style interpreters, quoted or not -
only `python3 tests/*`. That's a plain coverage gap, now closed by the
`anki_python` config field below.

**What I could not fully verify**: denial #2. Every individual subcommand in
it (`grep`, `head`, `git status --short`, `ls "$HOME/..."`, another `grep`)
already matched an existing `app_allowed_tools` rule or is itself a built-in
read-only command, yet the whole compound command was denied. I found (but
could not reproduce against this exact ticket's transcript) a live GitHub
issue describing the same shape of bug - `anthropics/claude-code#94314`,
"Bash permission allow-rule doesn't split on ;/&& before matching (contrary
to docs) - compound-command smuggling bypasses scoped allowlist" - which
says the documented per-subcommand splitting doesn't always happen in
practice. I'm treating denial #2 as most likely that bug (or a `$HOME`-
expansion / quoting edge case in the parser I couldn't pin down further
without a live `claude -p --permission-mode dontAsk` harness to binary-
search it), not a gap in `app_allowed_tools` - there is no single rule that
would plausibly need adding to cover it that isn't already present.

**What this means for `app_allowed_tools`**: since a compound command is
only as permissive as its least-covered subcommand (when the splitting
behavior works as documented), narrow `tests/*`-scoped rules are fragile the
moment the fixer writes a multi-step Bash call. This hardening widens the
allow list with `Bash(node *)`, `Bash(python3 *)`, and `Bash(pytest*)` (kept
narrower than a wildcard on the whole shell: no `Bash(cd {worktree} && *)`,
which would let a chained command run *anything* after a `cd`, defeating
`app_disallowed_tools` entirely for that segment) plus explicit rules for
the Anki venv interpreter, both as the literal absolute path and as the
quoted `"$HOME/..."` form Claude was observed writing:

- `Bash({anki_python} tests/*)` / `Bash("{anki_python}" tests/*)`, where
  `anki_python` is a new `Config` field (default
  `~/Library/Application Support/AnkiProgramFiles/.venv/bin/python`),
  formatted into the allow list the same way `deck_python` already is for
  deck tickets.
- `Bash("$HOME/Library/Application Support/AnkiProgramFiles/.venv/bin/python" tests/*)`
  as a fixed literal, since that's the exact form observed in the denial and
  `$HOME` is matched as literal text, not expanded, by the permission
  engine.

`Bash(git show*)` needed no change; it was already present and (per the
docs above) correctly covered its own subcommand in denial #1.

## Applying a fix to the live checkout

`ankifix apply <id>` (app tickets only - deck fixes go through the course's
own apply scripts by hand) turns a ticket's `fix.branch` into changes in the
live add-on checkout (`app_repo`, e.g. `~/dev/anki-design`), *without*
committing there: that checkout normally carries your own uncommitted edits.

1. `git format-patch <base>..<fix-branch> --stdout` in `app_repo` (`base` is
   the ticket's `app.branch`, falling back to `app_base_branch`). This reads
   the branch by ref; it does not need to run inside the fixer's worktree.
2. `git apply --check -` against `app_repo`'s working tree. If this fails,
   `fix.applied` is set to `{"at": ..., "error": "..."}`, a "needs manual
   apply" notification fires, and the command exits non-zero. Nothing is
   written to any file.
3. If the check passes, `git apply -` for real - plain `git apply`, not
   `--index`/`--cached` and never `--3way` (a real checkout with unstaged
   edits makes `--3way` fail with "does not match index" as a matter of
   course). This only ever rewrites file contents in the working tree; it
   never touches the index and never commits.
4. The patch is saved to `<ticket>/apply.patch`. Each command in
   `ticket.fix.tests` is then re-run from `app_repo`, with `NODE_PATH` set to
   `apply_node_path` (the live checkout's own npx cache, not the fixer
   worktree's - a separate `Config` field from `node_path`).
5. `fix.applied: {"at", "patch", "tests_passed"}` is recorded, and an "applied,
   restart Anki+" notification fires.

**You must restart Anki+ to pick up an applied fix**, and the patch sits
uncommitted in `app_repo` until you commit it yourself.

`ankifix apply` never runs `git checkout`, `git reset`, or `git commit` in
`app_repo`, and never touches anything outside it.

### auto_apply

In `--watch` mode, `Config.auto_apply` (default `true`) runs the apply step
automatically right after `run_ticket` leaves an app ticket `fixed` or
`needs-review`. Set `{"auto_apply": false}` in the config JSON to turn this
off and apply by hand instead.

The nohup watcher (started before this change landed) is running code from
before `apply.py` existed, so it will not auto-apply any ticket it
processes; those need `ankifix apply <id>` run by hand until it's replaced
by the LaunchAgent below.

## Watch-mode notifications

After each `run_ticket` in `--watch` mode (and after each `apply_ticket`),
if `terminal-notifier` is on `PATH` it's used; otherwise, if `osascript` is,
`osascript -e 'display notification ...'` posts `ankifix: <id> <status>`
(or, from apply, `ankifix: <id> applied, restart Anki+` /
`ankifix: <id> needs manual apply`). Controlled by `Config.notify` (default
`true`) and overridable per-invocation with `--notify`/`--no-notify`.

## Persistence: the launchd LaunchAgent

`ankifix install-agent` writes
`~/Library/LaunchAgents/com.aidanjones.ankifix-watch.plist` (`ProgramArguments`
`[~/.venvs/ankibug/bin/ankifix, --watch, --interval, 20]`, `RunAtLoad` and
`KeepAlive` both true, stdout/stderr to `~/AnkiTickets/ankifix-watch.log`,
`PATH` including `~/.local/bin` - where the `claude` binary lives - plus
Homebrew/usr paths for `git`/`node`/`python3`) and loads it with
`launchctl bootstrap gui/$(id -u) <plist>`. `ankifix uninstall-agent` runs
`launchctl bootout gui/$(id -u)/com.aidanjones.ankifix-watch` and removes the
file.

`install-agent` refuses (non-zero, clear message) if `pgrep -f
"ankifix.*--watch"` finds a running watcher, so a hand-started
(`nohup ankifix --watch ...`) and a launchd-started watcher can never run
against the same tickets dir at once.

**Migration from the nohup watcher**: the orchestrator runs this once the
current `nohup ankifix --watch --interval 20` (pid 73347) has been stopped:

```
ankifix install-agent --interval 20
```

## Locking

`~/AnkiTickets/.ankifix.lock` (flock). `ankifix <id>` exits 3 if another run
holds it; `--watch` waits and retries on its next poll.
