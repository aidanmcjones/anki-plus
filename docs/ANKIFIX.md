# ankifix

`ankifix` takes a ticket written by `ankibug` (`~/AnkiTickets/<id>/ticket.json`,
see `SCHEMA.md`) and runs Claude Code headless (`claude -p`) to fix it.

```
ankifix <id>                       # fix one ticket
ankifix --dry-run <id>             # print the prompt (command on stderr); change nothing
ankifix --watch [--interval 30]    # fix status=new tickets one at a time, oldest first
ankifix --watch --once             # drain the queue once and exit
ankifix --reindex                  # regenerate ~/AnkiTickets/index.md
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
  clock did not expire. Otherwise `failed`.

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
  instead of prompting (nobody is there to answer). The allow list is narrow
  (git subcommands, `node tests/*`, `python3 tests/*`, read-only shell tools);
  the deny list is a second fence (force-push, checking out the base branch,
  `make`, `kill`, `rm -rf`, web tools).
- Wall clock: the process group is SIGTERMed then SIGKILLed after
  `--timeout-min` (default 25).

`fix.log` is JSONL: an `{"type":"ankifix","event":"start",...}` line with the
exact command, Claude's stream-json events, then an `"event":"end"` line.

## Locking

`~/AnkiTickets/.ankifix.lock` (flock). `ankifix <id>` exits 3 if another run
holds it; `--watch` waits and retries on its next poll.
