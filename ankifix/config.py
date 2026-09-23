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

    # --- deck bugs ------------------------------------------------------
    deck_build_dir: Path = FB_BUILD
    deck_python: str = str(HOME / ".venvs/fb-anki/bin/python")
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
        ]
    )

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
            "Bash(ls*)", "Bash(cat*)", "Bash(head*)", "Bash(tail*)",
            "Bash(grep*)", "Bash(rg*)", "Bash(wc*)", "Bash(mkdir -p tests*)",
        ]
    )
    app_disallowed_tools: List[str] = field(
        default_factory=lambda: [
            "Bash(git push --force*)", "Bash(git push -f*)",
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
