"""ankifix configuration.

Defaults live here. Overrides come from (later wins):
  1. JSON file at $ANKIFIX_CONFIG or ~/.config/ankifix/config.json
  2. environment variables ANKIFIX_TICKETS_DIR, ANKIFIX_APP_REPO,
     ANKIFIX_CLAUDE, ANKIFIX_DECK_BUILD_DIR (mostly for tests)
  3. CLI flags (applied by ankifix.cli)
"""
from __future__ import annotations

import dataclasses
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

HOME = Path.home()
PKG_DIR = Path(__file__).resolve().parent
REPO_DIR = PKG_DIR.parent

FB_BUILD = (
    HOME
    / "Library/CloudStorage/OneDrive"
    / "Course B/Anki/build"
)


@dataclass
class Config:
    tickets_dir: Path = HOME / "AnkiTickets"

    # --- app bugs -------------------------------------------------------
    app_repo: Path = HOME / "dev/anki-design"
    app_base_branch: str = "video-backdrop"
    app_remote: str = "origin"
    worktrees_subdir: str = ".claude/worktrees"
    node_path: str = str(HOME / ".npm/_npx/e41f203b7505f1fb/node_modules")
    app_test_commands: List[str] = field(
        default_factory=lambda: [
            "node tests/editor_tools.cjs",
            "node tests/reviewer_click.cjs",
            "python3 tests/test_<name>.py   (run EVERY tests/test_*.py file, one command per file)",
        ]
    )
    # test commands (matched as a substring of the command actually run) that
    # are known to be broken independently of any fix: `tests/editor_tools.cjs`
    # times out under `claude -p` (Playwright + node in that harness), and
    # `tests/test_editor_crop.py` needs Anki's `aqt`, which only the Anki venv
    # (anki_python below) provides. Both are excluded from tests_green /
    # tests_passed everywhere they're computed (runner.py, apply.py) and
    # recorded separately as fix.tests_skipped / fix.applied.tests_skipped so
    # the exclusion is visible, not silent.
    known_failing_tests: List[str] = field(
        default_factory=lambda: ["tests/editor_tools.cjs", "tests/test_editor_crop.py"]
    )

    # --- deck bugs ------------------------------------------------------
    deck_build_dir: Path = FB_BUILD
    deck_python: str = str(HOME / ".venvs/fb-anki/bin/python")

    # --- anki venv (running app tests that need `aqt`) -------------------
    anki_python: str = str(HOME / "Library/Application Support/AnkiProgramFiles/.venv/bin/python")
    # notetype name prefixes that belong to course decks (kind=deck)
    course_notetype_prefixes: List[str] = field(
        default_factory=lambda: ["CourseB-", "Micro"]
    )

    # --- classification keywords ---------------------------------------
    deck_keywords: List[str] = field(
        default_factory=lambda: [
            "image on card", "on the card", "on this card", "on card",
            "card", "cards", "deck", "answer", "answers", "blank", "blanks",
            "cloze", "typo", "slide", "field", "question text", "wording",
            "wrong image", "missing image", "learning goal", "takeaway",
        ]
    )
    # phrases that contain deck words but are really app surfaces; they are
    # stripped from the note before deck keywords are counted
    app_phrases: List[str] = field(
        default_factory=lambda: [
            "deck browser", "deck list", "deck picker", "deck options",
            "deck deadline", "card browser", "add card", "add-card",
            "card info", "show answer button", "answer button",
            "answer buttons", "card count", "card counts",
            "another deck", "other deck", "different deck", "into a deck",
            "to a deck", "place in the deck", "position in the deck",
            "list of decks", "order of decks", "deck order",
            "deck name", "deck names", "names of decks", "name of the deck",
            "names of the decks", "deck column", "deck title", "deck titles",
        ]
    )
    app_keywords: List[str] = field(
        default_factory=lambda: [
            "editor", "reviewer ui", "deadline", "hotkey", "shortcut",
            "crash", "crashes", "crashed", "freeze", "freezes", "hang",
            "traceback", "exception", "error", "button", "sidebar",
            "toolbar", "menu", "settings", "preferences", "stats",
            "heatmap", "backdrop", "video", "window", "add-on", "addon",
            "click", "scroll", "theme", "dark mode", "browser", "popup",
            "deck browser", "card browser", "add card",
            # UI interaction: "select these cards and drag them to another
            # deck" is a feature request against the app, not a card fix
            "select", "selecting", "selected", "selection", "multi-select",
            "drag", "dragging", "drop", "drag and drop", "drag-and-drop",
            "dropdown", "drop-down", "deck list", "full screen", "fullscreen",
            "resort", "re-sort", "reorder", "re-order", "reordering",
            "shortcuts", "scrolling", "scrolls",
            "search bar", "searchbar", "column", "columns", "anki plus",
            "anki+", "layout", "formatting", "punctuation",
        ]
    )
    # a ticket with no reviewer.notetype whose reviewer.state is not
    # "question"/"answer", or that has no reviewer.card_id (ankibug records
    # state "question" on the deck list too), i.e. was not captured on a
    # card, gets this many extra
    # app points; below 1 so a single clear deck word still wins
    no_card_app_bias: float = 0.5

    # --- claude ---------------------------------------------------------
    claude_bin: str = "claude"
    max_turns: int = 60
    wall_clock_minutes: float = 25.0
    max_budget_usd: Optional[float] = None
    model: Optional[str] = None
    permission_mode: str = "dontAsk"
    app_allowed_tools: List[str] = field(
        default_factory=lambda: [
            "Read", "Edit", "Write", "Glob", "Grep", "TodoWrite",
            "Bash(git status*)", "Bash(git diff*)", "Bash(git log*)",
            "Bash(git show*)", "Bash(git add*)", "Bash(git commit*)",
            "Bash(git rev-parse*)", "Bash(git branch --show-current*)",
            "Bash(git push {remote} {branch}*)",
            "Bash(git push -u {remote} {branch}*)",
            "Bash(node tests/*)", "Bash(python3 tests/*)",
            "Bash(python3 -m pytest*)",
            # broader than the tests/*-scoped rules above: needed because a
            # compound command's non-test segments (a `git show ... | node
            # ...` pipe stage, a stray `pytest -k ...`) must each match a
            # rule on their own - see docs/ANKIFIX.md "Permission matching
            # for compound commands".
            "Bash(node *)", "Bash(python3 *)", "Bash(pytest*)",
            # the Anki venv python (has `aqt`; system/worktree python does
            # not), for `tests/test_*.py` files that import it. Claude has
            # been seen writing this both as the literal absolute path and
            # as a quoted "$HOME/..." expansion, so both are allow-listed.
            "Bash({anki_python} tests/*)",
            'Bash("{anki_python}" tests/*)',
            'Bash("$HOME/Library/Application Support/AnkiProgramFiles/.venv/bin/python" tests/*)',
            "Bash(ls*)", "Bash(cat*)", "Bash(head*)", "Bash(tail*)",
            "Bash(grep*)", "Bash(rg*)", "Bash(wc*)", "Bash(mkdir -p tests*)",
        ]
    )
    app_disallowed_tools: List[str] = field(
        default_factory=lambda: [
            "Bash(git push --force*)", "Bash(git push -f*)",
            "Bash(gh pr create*)", "Bash(gh pr *)", "Bash(gh api*)",
            "Bash(git checkout {base}*)", "Bash(git switch {base}*)",
            "Bash(git worktree*)", "Bash(git reset --hard*)",
            "Bash(make*)", "Bash(open*)", "Bash(pkill*)", "Bash(killall*)",
            "Bash(kill*)", "Bash(rm -rf*)", "WebFetch", "WebSearch",
        ]
    )
    deck_allowed_tools: List[str] = field(
        default_factory=lambda: [
            "Read", "Edit", "Write", "Glob", "Grep", "TodoWrite",
            "Bash({deck_python} build_deck.py*)",
            "Bash({deck_python} verify.py*)",
            "Bash({deck_python} load_cards.py*)",
            "Bash(ls*)", "Bash(cat*)", "Bash(head*)", "Bash(tail*)",
            "Bash(grep*)", "Bash(rg*)", "Bash(wc*)", "Bash(diff*)",
        ]
    )
    deck_disallowed_tools: List[str] = field(
        default_factory=lambda: [
            "Bash(*apply_*)", "Bash(*fix_collection*)", "Bash(open*)",
            "Bash(pkill*)", "Bash(killall*)", "Bash(kill*)", "Bash(rm*)",
            "WebFetch", "WebSearch",
        ]
    )

    # --- prompts --------------------------------------------------------
    prompt_app: Path = REPO_DIR / "docs/prompt_app.md"
    prompt_deck: Path = REPO_DIR / "docs/prompt_deck.md"
    co_author: str = "Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"

    # --- watch mode -------------------------------------------------------
    # post a macOS notification after each run_ticket (and after each
    # apply_ticket) in --watch mode, if terminal-notifier or osascript is
    # available. Set false to disable.
    notify: bool = True
    # after a `fixed`/`needs-review` app ticket in --watch mode, automatically
    # run the apply step (fix branch -> live add-on checkout, uncommitted).
    auto_apply: bool = True
    # NODE_PATH exported while running ticket.fix.tests during `ankifix apply`
    # (the live checkout's own playwright/npx cache, not the fixer worktree's).
    apply_node_path: str = str(HOME / ".npm/_npx/6bcb61ec6d5aea22/node_modules")
    # per-command timeout (seconds) for each ticket.fix.tests re-run during
    # `ankifix apply`, so a hung node/Playwright test cannot block the watcher
    apply_test_timeout_s: float = 120.0

    # --- subprocess PATH --------------------------------------------------
    # directories prepended (if missing) to PATH for the `claude -p` child and
    # the apply step's test runs. launchd starts the watcher with a minimal
    # PATH; node lives in ~/.local/node/bin, which is on no default PATH.
    extra_path: List[str] = field(
        default_factory=lambda: [
            str(HOME / ".local/node/bin"), str(HOME / ".local/bin"),
            "/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin",
        ]
    )

    # --- autonomy ---------------------------------------------------------
    # a ticket whose run raises (not a normal failed fix) is retried by the
    # watcher on its next poll, up to this many attempts in total; then it is
    # marked failed "gave up after N attempts" and never retried automatically.
    # A ticket left "fixing" by a watcher that died (sleep, kill) counts too.
    max_attempts: int = 2
    # deck tickets the launchd watcher cannot read (macOS TCC, EPERM on
    # ~/Library/CloudStorage) are handed to Terminal.app, which has Files
    # access: osascript `do script "<ankifix_bin> <id> --once-from-terminal"`
    terminal_fallback: bool = True
    ankifix_bin: str = str(HOME / ".venvs/ankibug/bin/ankifix")
    osascript_bin: str = "osascript"
    # osascript itself (Automation permission prompt can hang unattended)
    osascript_timeout_s: float = 150.0
    # how long the watcher waits for the Terminal run to finish ticket.json
    terminal_fallback_timeout_min: float = 40.0
    terminal_poll_s: float = 5.0
    # light `ankifix doctor` inside --watch; a check flipping to FAIL posts a
    # notification. 0 disables.
    doctor_interval_s: float = 3600.0

    def subprocess_path(self) -> str:
        """os.environ PATH with extra_path entries appended where missing."""
        parts = [p for p in os.environ.get("PATH", "").split(os.pathsep) if p]
        for d in self.extra_path:
            d = str(Path(d).expanduser())
            if d not in parts:
                parts.append(d)
        return os.pathsep.join(parts)

    def is_known_failing_test(self, cmd: str) -> bool:
        """True if cmd matches (as a substring) one of known_failing_tests."""
        return any(sub in cmd for sub in self.known_failing_tests)

    def worktree_path(self, ticket_id: str) -> Path:
        return self.app_repo / self.worktrees_subdir / f"fix-{ticket_id}"

    @staticmethod
    def branch_name(ticket_id: str) -> str:
        return f"fix/{ticket_id}"


_PATH_FIELDS = {f.name for f in dataclasses.fields(Config) if f.type in ("Path", Path)}


def load_config(path: Optional[str] = None) -> Config:
    cfg = Config()
    cfg_path = Path(
        path or os.environ.get("ANKIFIX_CONFIG") or HOME / ".config/ankifix/config.json"
    ).expanduser()
    if cfg_path.is_file():
        data = json.loads(cfg_path.read_text())
        known = {f.name for f in dataclasses.fields(Config)}
        for key, value in data.items():
            if key not in known:
                raise ValueError(f"unknown ankifix config key {key!r} in {cfg_path}")
            if key in _PATH_FIELDS:
                value = Path(value).expanduser()
            setattr(cfg, key, value)
    env_map = {
        "ANKIFIX_TICKETS_DIR": "tickets_dir",
        "ANKIFIX_APP_REPO": "app_repo",
        "ANKIFIX_DECK_BUILD_DIR": "deck_build_dir",
    }
    for env, attr in env_map.items():
        if os.environ.get(env):
            setattr(cfg, attr, Path(os.environ[env]).expanduser())
    if os.environ.get("ANKIFIX_CLAUDE"):
        cfg.claude_bin = os.environ["ANKIFIX_CLAUDE"]
    return cfg
