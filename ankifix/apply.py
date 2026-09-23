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
import signal
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .config import Config
from .runner import AnkifixError, notify
from .tickets import load_ticket, now_iso, save_ticket, ticket_dir

PATCH_NAME = "apply.patch"


def _run(cwd: Path, args: List[str], input_text: Optional[str] = None,
         env: Optional[Dict[str, str]] = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        args, cwd=str(cwd), input=input_text, capture_output=True, text=True, env=env,
    )


def test_env(cfg: Config) -> Dict[str, str]:
    """Environment for re-running ticket.fix.tests: explicit PATH (node lives
    in ~/.local/node/bin, which launchd's PATH lacks) and NODE_PATH =
    cfg.apply_node_path (the live checkout's Playwright cache)."""
    env = dict(os.environ)
    env["PATH"] = cfg.subprocess_path()
    env.setdefault("HOME", str(Path.home()))
    env["NODE_PATH"] = cfg.apply_node_path
    return env


def run_test_command(cmd: str, cwd: Path, env: Dict[str, str],
                     timeout_s: float) -> Tuple[Optional[int], str, str, bool]:
    """Run one shell test command in its own process group. On timeout the
    whole group (shell plus node/Playwright children) is killed. Returns
    (rc, stdout, stderr, timed_out)."""
    proc = subprocess.Popen(
        cmd, shell=True, cwd=str(cwd), env=env, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True, start_new_session=True,
    )
    try:
        out, err = proc.communicate(timeout=timeout_s)
        return proc.returncode, out, err, False
    except subprocess.TimeoutExpired:
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(proc.pid, sig)
            except ProcessLookupError:
                break
            try:
                out, err = proc.communicate(timeout=5)
                break
            except subprocess.TimeoutExpired:
                out, err = "", ""
                continue
        return proc.returncode, out or "", err or "", True


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

    env = test_env(cfg)
    tests_passed = True
    timed_out_tests: List[str] = []
    for cmd in fix.get("tests") or []:
        rc, out, err, timed_out = run_test_command(cmd, repo, env, cfg.apply_test_timeout_s)
        if timed_out:
            tests_passed = False
            timed_out_tests.append(cmd)
            echo(f"ankifix: {ticket_id} apply test timed out after {cfg.apply_test_timeout_s:g}s: {cmd}")
        elif rc != 0:
            tests_passed = False
            echo(f"ankifix: {ticket_id} apply test failed: {cmd}\n{out}\n{err}")

    ticket["fix"]["applied"] = {"at": now_iso(), "patch": PATCH_NAME, "tests_passed": tests_passed}
    if timed_out_tests:
        ticket["fix"]["applied"]["tests_timed_out"] = timed_out_tests
    save_ticket(cfg.tickets_dir, ticket)
    notify(cfg, f"ankifix: {ticket_id} applied, restart Anki+")
    echo(f"ankifix: {ticket_id} applied to {repo} (tests_passed={tests_passed})")
    return ticket
