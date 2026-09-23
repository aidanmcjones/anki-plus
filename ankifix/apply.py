"""Apply an app ticket's fix branch to the live add-on checkout.

`ankifix apply <ticket-id>` (and, when `Config.auto_apply` is on, `watch()`
after a `fixed`/`needs-review` app run) builds a patch of the ticket's fix
branch against its base with `git format-patch --stdout`, `git apply
--check`s it against the *main* add-on checkout (`cfg.app_repo`, not the
fixer's worktree), and - only if that check passes - applies it to the
working tree with a plain `git apply` (no `--index`, no `--cached`, no
`--3way`): the checkout carries the user's own uncommitted edits, so this
only ever touches file contents, never the index, and never commits.

It never runs `git checkout`, `git reset`, or `git commit` in `cfg.app_repo`,
and it refuses anything that is not an `app` ticket with a `fix.branch` -
deck fixes are applied through the course's own apply scripts, which is the
user's manual step.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional

from .config import Config
from .runner import AnkifixError, notify
from .tickets import load_ticket, now_iso, save_ticket, ticket_dir

PATCH_NAME = "apply.patch"


def _run(cwd: Path, args: List[str], input_text: Optional[str] = None,
         env: Optional[Dict[str, str]] = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        args, cwd=str(cwd), input=input_text, capture_output=True, text=True, env=env,
    )


def apply_ticket(ticket_id: str, cfg: Config, echo=print) -> Dict[str, Any]:
    """Apply ticket_id's fix branch to cfg.app_repo's working tree. Raises
    AnkifixError (without raising past a recorded fix.applied) on any
    precondition failure or a patch that does not apply cleanly."""
    ticket = load_ticket(cfg.tickets_dir, ticket_id)

    if ticket.get("kind") != "app":
        raise AnkifixError(
            f"ticket {ticket_id} is kind={ticket.get('kind')!r}; apply only handles app "
            "tickets (deck fixes are applied through the course's apply scripts by hand)"
        )

    fix = ticket.get("fix") or {}
    branch = fix.get("branch")
    if not branch:
        raise AnkifixError(f"ticket {ticket_id} has no fix.branch; nothing to apply")

    repo = Path(cfg.app_repo)
    base = (ticket.get("app") or {}).get("branch") or cfg.app_base_branch
    tdir = ticket_dir(cfg.tickets_dir, ticket_id)

    fp = _run(repo, ["git", "format-patch", f"{base}..{branch}", "--stdout"])
    if fp.returncode != 0:
        raise AnkifixError(
            f"git format-patch {base}..{branch} failed in {repo}: {fp.stderr.strip()}"
        )
    patch_text = fp.stdout
    if not patch_text.strip():
        raise AnkifixError(
            f"git format-patch {base}..{branch} produced an empty patch in {repo} "
            "(branch has no commits past base?)"
        )

    check = _run(repo, ["git", "apply", "--check", "-"], input_text=patch_text)
    if check.returncode != 0:
        err = check.stderr.strip() or "git apply --check failed"
        ticket.setdefault("fix", {})
        ticket["fix"]["applied"] = {"at": now_iso(), "error": err}
        save_ticket(cfg.tickets_dir, ticket)
        notify(cfg, f"ankifix: {ticket_id} needs manual apply")
        echo(f"ankifix: {ticket_id} apply --check failed in {repo}: {err}")
        raise AnkifixError(f"patch for {ticket_id} does not apply cleanly to {repo}: {err}")

    # --check passed; apply for real. Plain `git apply` (no --index/--cached)
    # only ever writes file contents in the working tree - never the index,
    # never a commit - so it coexists with whatever else is uncommitted there.
    applied = _run(repo, ["git", "apply", "-"], input_text=patch_text)
    if applied.returncode != 0:
        err = applied.stderr.strip() or "git apply failed"
        ticket.setdefault("fix", {})
        ticket["fix"]["applied"] = {"at": now_iso(), "error": err}
        save_ticket(cfg.tickets_dir, ticket)
        notify(cfg, f"ankifix: {ticket_id} needs manual apply")
        echo(f"ankifix: {ticket_id} apply failed after a passing --check in {repo}: {err}")
        raise AnkifixError(f"git apply failed for {ticket_id} in {repo}: {err}")

    (tdir / PATCH_NAME).write_text(patch_text)

    env = dict(os.environ)
    env["NODE_PATH"] = cfg.apply_node_path
    tests_passed = True
    for cmd in fix.get("tests") or []:
        r = subprocess.run(cmd, shell=True, cwd=str(repo), env=env, capture_output=True, text=True)
        if r.returncode != 0:
            tests_passed = False
            echo(f"ankifix: {ticket_id} apply test failed: {cmd}\n{r.stdout}\n{r.stderr}")

    ticket["fix"]["applied"] = {"at": now_iso(), "patch": PATCH_NAME, "tests_passed": tests_passed}
    save_ticket(cfg.tickets_dir, ticket)
    notify(cfg, f"ankifix: {ticket_id} applied, restart Anki+")
    echo(f"ankifix: {ticket_id} applied to {repo} (tests_passed={tests_passed})")
    return ticket
