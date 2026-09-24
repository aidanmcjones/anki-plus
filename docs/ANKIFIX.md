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
ankifix install-agent [--interval 20]  # doctor, then write+load the launchd LaunchAgent
ankifix uninstall-agent            # unload+remove the LaunchAgent
ankifix doctor                     # PASS/FAIL health checks; exit 1 on any FAIL
  --kind app|deck                  # override classification
  --max-turns 60  --timeout-min 25 --max-budget-usd X  --model M  --force
```

Install: `pip install -e ~/dev/ankibug` (editable: the prompt templates are read
from `docs/prompt_app.md` and `docs/prompt_deck.md` in this repo).

## Classification

1. `--kind` wins. 2. A ticket whose `kind` is already `app`/`deck` keeps it.
3. `reviewer.notetype` starting with a course prefix (`CourseB-`, `Micro`)
   is *evidence* for `deck`, not a verdict on its own - see "Routing" below.
4. Otherwise (no course prefix) keyword scoring on `note` + `expected`: deck words (card, deck,
   answer, blank, cloze, image on card, ...) against app words (editor, reviewer
   UI, deadline, hotkey, crash, button, ...). App surfaces that contain deck words
   ("deck browser", "answer button", "add card") are removed before deck words
   are counted. Console errors add one point to app. Ties go to app.
   UI-interaction words (select, drag, drop, drag and drop, dropdown, deck
   list, full screen, resort, reorder, shortcut, scroll, ...) count for app,
   and phrases like "another deck" / "place in the deck" / "list of decks" are
   stripped before deck words are counted, so "select cards and drag them to
   another deck" is an app feature request, not a card fix. A ticket with no
   `reviewer.notetype` whose `reviewer.state` is neither `question` nor
   `answer` (not captured on a card) gets `no_card_app_bias` (0.5) extra app
   points: enough to break a tie, never enough to beat one clear deck word
   ("the answer on card C2-Q05 is wrong" is still deck).
5. **Off-card rule** (ticket `20260924-131713`). A ticket with no course
   notetype that was not captured on a card (no `reviewer.card_id`, or a
   state other than `question`/`answer`) is `app` outright, unless the note
   itself names card content: a `card_content_signals` phrase ("card says",
   "this card", "on the card", "wrong answer", "the answer", "typo",
   "cloze", "wording", ...; matched after the app phrases above are
   stripped, so "answer button" does not count), a card id
   (`card_id_pattern`, e.g. `C2-Q05`), or a course name (`course_names`:
   courseb, course a, ...) with no app hint next to it. Only then
   does rule 4's scoring decide. Why: a deck fix edits a course card's
   content in the build dir, and with no card on screen and no card named
   there is nothing to point it at. "the restudy deck should have a title
   denoting what the restudy deck contains, for example: restudy: courseb
   hi-yield" (filed from the deck list, `state: answer`, `card_id: null`)
   went to deck on the bare word "deck" and failed; it is now app (app hints
   `restudy`, `title`; the course name is a deck-list label here, since app
   hints are present). New app hints: restudy, filtered deck, title,
   rename (plus the existing button, menu, sidebar).
6. **Safety net: `wrong-kind`.** The deck prompt tells the deck fixer to
   check first and, when the bug is in the add-on/app rather than course
   card content, change nothing and return `"status": "wrong-kind"` with a
   `reason`. The runner then flips the ticket to `kind: app`, `status: new`
   (not `failed`), records `fix.kind_flip` `{from, to, reason, at}`, resets
   `fix.attempts` so the app fixer gets its own retry budget, and logs it;
   the watcher runs the app fixer on its next poll. At most one flip:
   `fix.kind_flip` is carried across reruns and a second `wrong-kind` is
   `failed` ("not flipping again"); the app prompt never offers
   `wrong-kind`, and an app run that returns it anyway is `failed`.

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

### Routing: the course notetype prefix is evidence, not a verdict

Ticket `20260923-102640` ("I should be able to right-click on an image and
copy it or cut it. Add these two options to the pop-up menu. From there I
should be able to paste a copied image from my clipboard into any field I
want") was captured while a CourseB-E1 card happened to be showing. The
old classifier returned `deck` as soon as it saw the course-prefixed
notetype, before it ever looked at the words - so this app feature request
was sent down the deck path, hit the OneDrive Files TCC block, and only
reached a human via the Terminal fallback.

Now, when `reviewer.notetype` matches a course prefix, `classify()` still
looks at the note before deciding:

1. **Content/rendering words win first.** If the note mentions the card's
   own content or rendering (`course_content_keywords`: answer, wrong, typo,
   missing, "image on the card", cloze, front, back, shows, hidden, blank,
   definition, slide, and the "on the/this card" phrasings) - `deck`. This
   check comes first on purpose: "the image on this card is cut off" stays
   `deck` even though it also contains the app-ish words "image" and "cut".
2. **Otherwise, UI verbs/nouns route it to app.** If the note has
   `course_app_signal_keywords` (right-click, menu, pop-up, clipboard, copy,
   cut, paste, drag, drop, select, dropdown, sidebar, search bar, settings,
   toolbar, full screen, window, shortcut, hotkey, scroll, editor, field,
   button, resize, image) - `app`, even on a course card. Ticket 102640
   (right-click/copy/cut/pop-up menu/paste/clipboard/field) is exactly this
   case.
3. **No signal either way** - `deck` (the prefix's original default stands).

Both lists are `Config` fields (`course_app_signal_keywords`,
`course_content_keywords`), overridable the same way as everything else in
`~/.config/ankifix/config.json`.

**Manual override:** when the classifier still gets a ticket wrong, fix the
routing by hand instead of re-running it and hoping - `ankibug set <id>
kind=app` (or `kind=deck`) sets `ticket.kind` directly, which both `ankibug`
and `ankifix` (rule 2 above) treat as authoritative from then on, no
classifier involved. `ankifix <id> --kind app|deck` does the same for one run
without persisting it to the ticket.

### Held tickets

`status: needs-review` is the only status the watcher's `--watch` loop never
picks back up on its own - it is the manual hold. Use it (or just leave a
ticket that landed there) when you want to look at something before it runs
again.

`status: fixing` set by hand (`ankibug set <id> status=fixing`, e.g. while
you work on it yourself) is left alone. `recover_stale()` only requeues a run
the watcher itself claimed that never finished: `fix.owner` `"watcher"` (or
no owner but a `fix.pid`, a run recorded before owners existed),
`fix.finished` null, and `fix.pid` dead. A hand-set `fixing` sits on a
finished run's `fix` (or no `fix`, or `manual_set.at` newer than
`fix.started`), so it is never requeued. Before 20260924-131713 it was: the
watcher re-ran that ticket concurrently with the human and then overwrote
the human's `status=fixed` with its own result. A dead manual `ankifix <id>`
run (`fix.owner: "cli"`) is marked `failed`, not requeued.

**A human change always wins over a run that finishes later.** `ankibug
set` stamps `ticket.manual_set` `{at, fields}`. Before a run writes its
result it re-reads `ticket.json`; if the status left `fixing`,
`fix.started`/`fix.pid` are no longer that run's, or `manual_set` changed,
the on-disk ticket is kept and the run's result goes to
`fix.result_ignored` `{status, kind, summary, commits, log_path, ...}`, and
the watcher does not auto-apply it.

**`ankibug set` is atomic.** Every `key=value` pair is applied to an
in-memory copy and validated before anything is written; one bad pair
(`kind=app status=in_progress`) writes nothing and prints `...; nothing
written`. It used to write `kind=app` and then fail on the status.

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
- **`known_failing_tests`** (default `["tests/editor_tools.cjs",
  "tests/test_editor_crop.py"]`, a `Config` field, substring-matched against
  each test command): those two commands are broken for reasons unrelated to
  any given ticket (`editor_tools.cjs` times out under `claude -p`,
  `test_editor_crop.py` needs Anki's `aqt`, which only `anki_python`
  provides), so every fix that reached the end of its run legitimately was
  landing `needs-review` even when the fix's own tests passed - the user had
  to relabel those by hand. Now: any test command matching a
  `known_failing_tests` substring is excluded everywhere tests_green/
  tests_passed is computed, and recorded separately (never silently dropped)
  as `fix.tests_skipped` (set in `runner.py`, from the commands the fixer's
  transcript/result block listed) and `fix.applied.tests_skipped` (set in
  `apply.py`, from `fix.tests` re-run during `ankifix apply`). The fixer's
  prompt (`docs/prompt_app.md`, `${known_failing_tests}`) also lists them, so
  it doesn't waste turns chasing a fix for a test that isn't really broken by
  its change. **The apply step is the authority for the final status**: when
  a ticket the fixer reported fixed (`status` `fixed` or `needs-review`) is
  applied, its non-excluded `fix.tests` are re-run against the live checkout,
  and the ticket's `status` is set from that re-run, not from the fixer's own
  `tests_green` self-report - `fixed` if all non-excluded tests pass, else
  `needs-review`. The transition is logged: `<id>: apply re-ran N tests, M
  skipped (known failing), all passed -> fixed` (or `failures remain ->
  needs-review`). Since `auto_apply` (default on) runs the apply step right
  after every app-ticket run in `--watch` mode, this means a ticket whose own
  tests are green no longer needs a human to relabel it away from
  `needs-review`.
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

**Every attempt keeps its own transcript**: `<ticket>/fix.<n>.log` (n = 1,
2, ...), with `fix.log_path` naming the latest and `fix.log` a symlink to
it. A pre-existing regular `fix.log` from older runs is kept as
`fix.0.log`. (`fix.log` used to be reopened with `"w"`, so attempt 2 erased
attempt 1's evidence.) The Terminal child's stdout/stderr go to
`<ticket>/terminal.log` (below). `ankifix --watch` prefixes every log line
with an ISO timestamp.

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
5. Each live test in `ticket.fix.live_tests` (tests/live/test_*.py, from the
   fixer's result block) is run through the add-on's live harness:
   `<anki_pyenv> <app_repo>/<live_harness> --timeout <live_test_timeout_s>
   <test>` from `app_repo`. The harness launches a throwaway, offscreen copy
   of the REAL Anki app (fixture collection, the checkout's add-on code) and
   runs the test inside it; see the add-on's `tests/live/README.md`.
6. `fix.applied: {"at", "patch", "tests_passed", "live_tests_passed",
   "live_tests": [{"test", "ok", "rc", "command", "output"?}], "landed"?}` is
   recorded.
7. Status: `fixed` only when the tests pass AND every live test passes AND
   there is at least one live test (`require_live_tests`, default true).
   Otherwise `needs-review`, `fix.applied.landed = {"state": "not-landed",
   "reason": "live test failed: ..." | "no live test" | "tests failed"}`, the
   summary starts with that reason, and the notification says
   `ankifix: <id> live test failed..., not landed`. The patch stays in the
   working tree (it is still uncommitted there); the app is NOT restarted.
8. Fully green: the fix is landed in the running app (below).

### Landing: restarting Anki+ (auto_restart_app)

The running app only loads add-on code at startup. After a fully green apply
`ankifix/restart.py` restarts Anki+ when it is safe:

- `cdp_url` (`http://127.0.0.1:8080/json`) answers, and its page list has
  no Anki webview besides `main webview` / `top toolbar` / `bottom toolbar`
  (an `editor`, add-card, browser, deck options, previewer... window means
  unsafe);
- the main webview shows the deck list or the congrats page (congrats by
  URL; otherwise one read-only `Runtime.evaluate` classifies the DOM:
  `#qa` = reviewer, `.ad-list-row` = deck list).

Safe: notification `ankifix: restarting Anki+ to load <id>`, then
`osascript -e 'tell application "Anki+" to quit'` (its -128 error is
benign), wait up to `restart_quit_timeout_s` (30) for `pgrep -f anki-dev` to
come back empty, then `open -na "Anki+"`. Never force-kills: an app that
does not quit is left alone and the fix loads on the next restart.

Not safe: notification `ankifix: <id> fix applied, will load on next
restart`, and the id is queued in `<tickets_dir>/.pending_restart.json`.
The watcher retries every `restart_retry_interval_s` (300) for up to
`restart_retry_window_s` (7200). If Anki+ was restarted by the user after
the apply, the queue is cleared as `loaded`. The outcome is recorded as
`fix.applied.landed.state`: `restarted`, `loaded`, `pending`,
`not-running`, `failed`, `gave-up`.

Set `{"auto_restart_app": false}` to go back to restarting by hand. The
watcher only reloads its own code on restart:
`launchctl kickstart -k gui/$(id -u)/com.aidanjones.ankifix-watch`, then
look for a fresh `ankifix: watcher started pid <pid>` line in its log.

The patch sits uncommitted in `app_repo` until you commit it yourself.

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
`KeepAlive` both true, `ThrottleInterval` 30, stdout/stderr to
`~/AnkiTickets/ankifix-watch.log`, and `EnvironmentVariables` `PATH` + `HOME`)
and loads it with
`launchctl bootstrap gui/$(id -u) <plist>`. `ankifix uninstall-agent` runs
`launchctl bootout gui/$(id -u)/com.aidanjones.ankifix-watch` and removes the
file.

`PATH` is resolved at install time: the directory of each of `node`,
`claude`, `git`, `python3` as `shutil.which` finds it on the installing
shell's PATH (so `~/.local/node/bin` for node), first, then
`~/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin`. `HOME` is the
installer's. Before this, the plist PATH had no node directory, so the fixer
reported "no node binary exists on this machine" and the apply step's node
tests failed with `node: command not found` (tickets 232122, 085108).
Independently of the plist, `claude -p` and the apply step's test runs get
an explicit `PATH` (`env=`): the process PATH plus any missing `extra_path`
entries (default `~/.local/node/bin`, `~/.local/bin`, Homebrew, `/usr/bin`,
`/bin`). Apply test runs also get `NODE_PATH=apply_node_path`, run in their
own process group, and are killed after `apply_test_timeout_s` (120 s)
each, recorded in `fix.applied.tests_timed_out`.

`install-agent` refuses (non-zero, clear message) if `pgrep -f
"ankifix.*--watch"` finds a running watcher, so a hand-started
(`nohup ankifix --watch ...`) and a launchd-started watcher can never run
against the same tickets dir at once.

New config keys (all optional in `~/.config/ankifix/config.json`):
`no_card_app_bias` (0.5), `apply_test_timeout_s` (120), `extra_path`,
`max_attempts` (2), `terminal_fallback` (true), `ankifix_bin`,
`osascript_bin`, `osascript_timeout_s` (150), `terminal_fallback_timeout_min`
(40), `terminal_poll_s` (5), `doctor_interval_s` (3600),
`known_failing_tests` (`["tests/editor_tools.cjs",
"tests/test_editor_crop.py"]`, see above).

## Autonomy: the watcher never dies on one ticket

Overnight on 2026-09-23 the LaunchAgent crash-looped 84 times on ticket
`20260923-085141`: it was classified deck, the deck prompt opened
`build_out.json` under `~/Library/CloudStorage/...`, macOS refused
(`PermissionError: [Errno 1] Operation not permitted`, a TCC denial, since a
launchd agent has no Files and Folders access), the exception escaped
`run_ticket` (the prompt was rendered before its `try`), the process exited
1, `KeepAlive` respawned it, and it hit the same ticket again. What now
prevents each part of that:

- **Every failure is recorded on the ticket.** `run_ticket` classifies,
  plans, renders and runs inside one `try`; any exception sets `status`
  and `fix.summary` (the error) and appends the traceback to
  `<ticket>/error.log` (`fix.error_log`). `watch()` additionally wraps each
  `run_ticket`, each auto-apply and the whole poll pass in `except
  Exception`, notifies, and moves on. A ticket whose result cannot even be
  saved is skipped for the life of the process.
- **Retry cap.** `fix.attempts` counts runs. In `--watch`, a run that raised
  goes back to `new` and is retried on the next poll; the second exception
  marks it `failed` with `gave up after 2 attempts` and it is never retried
  automatically (`max_attempts`, default 2). A ticket left `fixing` by a
  watcher that died mid-run (`fix.pid` no longer alive: Mac slept, killed)
  is requeued the same way on the next poll, and given up at the cap.
- **ThrottleInterval 30** in the plist: launchd waits 30 s before respawning
  a watcher that exited, so even a crash that escapes all of the above
  cannot spin. Each start logs `ankifix: watcher started pid N`, so restarts
  are visible in `~/AnkiTickets/ankifix-watch.log`.
- **Hourly doctor.** Every `doctor_interval_s` (3600) the watcher runs the
  light checks below against the PATH it hands its children and posts a
  notification when one flips to FAIL.

## Deck tickets and macOS Files access (TCC)

A launchd agent cannot read `~/Library/CloudStorage` (OneDrive) until it is
granted access. Before a deck run the watcher probes the build dir (list it,
open `build_out.json`). On `EPERM`:

1. **Terminal fallback** (`terminal_fallback`, default on). The ticket is
   handed to Terminal.app, which has Files access:
   `osascript -e 'tell application "Terminal" to do script "~/.venvs/ankibug/bin/ankifix <id> --once-from-terminal; exit"'`.
   The child's output is tee'd (unbuffered) into `<ticket>/terminal.log`,
   which also gets a header line per delegation; the watcher's `(Terminal
   run)` log line carries the child's summary (and the tail of
   terminal.log when it left none), so a failed Terminal run says why.

   **`--watch` does not wait** (`run_ticket(wait_terminal=False)`): it marks
   the ticket `fixing` with `fix.delegated: "terminal"`, `fix.delegated_at`,
   `fix.owner: "watcher"`, records the id in
   `~/AnkiTickets/.ankifix-delegations.json`, and moves on. The global lock
   is released at the end of that poll, so other tickets are no longer
   blocked for up to 40 minutes. Each later poll (under the lock) runs
   `settle_delegations()`: a ticket that left `fixing` (the child's result, a
   `wrong-kind` flip, or a human's `ankibug set`) is logged and dropped from
   the file; a child whose pid died with the ticket still `fixing` is marked
   `failed` ("terminal run ended without a result" + terminal.log tail); a
   child that never claimed the ticket within `osascript_timeout_s +
   terminal_start_grace_s` becomes `needs-review` (files-access). While a
   delegation is in flight the watcher skips *deck* tickets (one deck fixer
   in the build dir at a time); app tickets keep flowing. `recover_stale()`
   never touches a delegation. This is safe because the child already ran
   without the lock, the child and the watcher never write the same ticket
   at the same time (the watcher only writes a delegated ticket once its
   child is dead or never started), and deck work is serialized by the
   deck-skip rule.

   A manual `ankifix <id>` (not `--watch`) still waits, as described next.
   `--once-from-terminal` runs that one ticket without taking the lock and
   without a fallback of its own; it records its own pid in `fix.pid` and
   `fix.owner: "terminal"` the moment it starts. The waiting run polls
   `ticket.json` every `terminal_poll_s` (5) until
   the same `settle_delegation()` check the watcher uses says it settled:
   the status left `fixing` (a real result, or someone changed it by hand,
   which is kept as is: a hand-set `new` is no longer overwritten with
   `failed`), or `fix.pid` is no longer alive while the ticket is still
   `fixing`, which is an abnormal end caught within one poll interval: the
   ticket is marked `failed`, `"terminal run ended without a result"`. Either way
   the wait is capped at `terminal_fallback_timeout_min` (40). The first
   time, macOS asks whether Python may control Terminal (Automation); allow
   it once.

   This closes a real incident: an orchestrator killed both the `ankifix
   <id> --once-from-terminal` process and its `claude` child for a ticket,
   then set its status back to `new` by hand. The old poll only asked "has
   status left `{fixing, new}`?", which a `new` reset (still in that exempt
   set) never satisfies, so the watcher sat there - doing nothing else
   either, since `--watch` processes one ticket at a time - for the rest of
   the timeout window and needed a manual restart. Now a status change away
   from `fixing` that isn't the delegation's own doing (or a dead pid) ends
   the wait immediately instead of being waited on.
2. If osascript is refused (Automation denied, error -1743) or times out, the
   ticket becomes `needs-review` with `fix.blocked: "files-access"` and the
   summary: *deck fixes need Files access for the watcher; run `ankifix <id>`
   from a terminal, or grant Full Disk Access to <python> in System Settings
   > Privacy & Security*. A `files-access` ticket can be re-run with
   `ankifix <id>` without `--force`.

**One-time step that removes the fallback entirely:** System Settings >
Privacy & Security > Full Disk Access > `+`, press Cmd+Shift+G and add

```
/Library/Developer/CommandLineTools/Library/Frameworks/Python3.framework/Versions/3.9/Resources/Python.app
```

Why that path: `~/.venvs/ankibug/bin/python` is a symlink to
`/Library/Developer/CommandLineTools/usr/bin/python3` (the Command Line Tools
python 3.9.6, `com.apple.python3`), a framework build that re-execs
`Python3.framework/Versions/3.9/Resources/Python.app/Contents/MacOS/Python`.
`ps` on the running watcher shows that binary, so it is what TCC attributes
the file access to (`ankifix doctor` prints it). The grant covers every
script run by the Command Line Tools python, not just ankifix. After
granting, `launchctl kickstart -k gui/$(id -u)/com.aidanjones.ankifix-watch`.

## ankifix doctor

Prints `PASS`/`FAIL` per check and exits 1 on any FAIL:

- `claude`, `git`, `node`, `python3` resolvable on the agent PATH (the
  installed plist's PATH, or what install-agent would write);
- `require('playwright')` works with `node_path` and with `apply_node_path`;
- the addon repo is a git checkout, `git status` works, the base branch exists;
- the tickets dir is writable;
- the course build dir is readable (the TCC check: opens `build_out.json`);
- the launchd agent is loaded and has a PID;
- an osascript notification posts.

`install-agent` runs it (minus the agent check) before loading the plist and
refuses when any of the four tools is missing. The watcher runs the light
subset (no agent, no notification) hourly.

## Locking

`~/AnkiTickets/.ankifix.lock` (flock). `ankifix <id>` exits 3 if another run
holds it; `--watch` waits and retries on its next poll. The watcher holds
it for one poll pass (recover_stale, settle_delegations, the queue); it is
no longer held while a Terminal child runs (see the TCC section).
