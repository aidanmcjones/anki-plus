"""Apply an app ticket's fix branch to the live add-on checkout.

`ankifix apply <ticket-id>` (and, when `Config.auto_apply` is on, `watch()`
after a `fixed`/`needs-review` app run) builds a patch of the ticket's fix
branch against its base with `git format-patch --stdout`, `git apply
--check`s it against the *main* add-on checkout (`cfg.app_repo`, not the
fixer's worktree), and - only if that check passes - applies it to the
working tree with a plain `git apply` (no `--index`, no `--cached`, no
`--3way`): the checkout carries the user's own uncommitted edits, so this
only ever touches file contents, never the index, and never commits.

After the patch it re-runs the fix's own tests and then its LIVE tests
(`fix.live_tests`, tests/live/test_*.py) through the add-on's live harness
(`cfg.anki_pyenv cfg.live_harness`), which runs each one inside a throwaway,
offscreen copy of the real Anki app. A failing live test, or an app fix with
no live test at all (cfg.require_live_tests), leaves the ticket in
needs-review and the fix is NOT landed. A fully green apply is landed in the
user's running Anki+ by restart.land() when cfg.auto_restart_app is on.

It never runs `git checkout`, `git reset`, or `git commit` in `cfg.app_repo`,
and it refuses anything that is not an `app` ticket with a `fix.branch` -
deck fixes are applied through the course's own apply scripts, which is the
user's manual step.
"""
from __future__ import annotations

import os
import shlex
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


def run_live_tests(cfg: Config, repo: Path, live_tests: List[str], env: Dict[str, str],
                   echo=print) -> Tuple[bool, List[Dict[str, Any]]]:
    """Run each live test through the harness in repo (the live checkout,
    now carrying the fix). Returns (all passed, per-test results)."""
    harness = repo / cfg.live_harness
    results: List[Dict[str, Any]] = []
    if not harness.is_file():
        for t in live_tests:
            results.append({"test": t, "ok": False, "reason": f"live harness {harness} not found"})
        return False, results
    for t in live_tests:
        if not (repo / t).is_file():
            results.append({"test": t, "ok": False, "reason": f"{t} not found in {repo}"})
            continue
        cmd = shlex.join([cfg.anki_pyenv, str(harness), "--timeout",
                          f"{cfg.live_test_timeout_s:g}", t])
        rc, out, err, timed_out = run_test_command(cmd, repo, env, cfg.live_test_timeout_s + 60)
        ok = rc == 0 and not timed_out
        res: Dict[str, Any] = {"test": t, "ok": ok, "rc": rc, "command": cmd}
        if timed_out:
            res["reason"] = f"timed out after {cfg.live_test_timeout_s + 60:g}s"
        if not ok:
            res["output"] = ((out or "") + (err or ""))[-2000:]
        results.append(res)
        echo(f"ankifix: live test {'passed' if ok else 'FAILED'}: {t}"
             + ("" if ok else f"\n{res.get('output', '')}"))
    return all(r["ok"] for r in results), results


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
    tests_run: List[str] = []
    tests_skipped: List[str] = []
    timed_out_tests: List[str] = []
    for cmd in fix.get("tests") or []:
        # known-failing pre-existing tests (cfg.known_failing_tests) are never
        # re-run here: they fail (or hang) for reasons unrelated to this fix,
        # so they must not count against tests_passed. Recorded separately,
        # never silently dropped.
        if cfg.is_known_failing_test(cmd):
            tests_skipped.append(cmd)
            echo(f"ankifix: {ticket_id} apply test skipped (known failing): {cmd}")
            continue
        tests_run.append(cmd)
        rc, out, err, timed_out = run_test_command(cmd, repo, env, cfg.apply_test_timeout_s)
        if timed_out:
            tests_passed = False
            timed_out_tests.append(cmd)
            echo(f"ankifix: {ticket_id} apply test timed out after {cfg.apply_test_timeout_s:g}s: {cmd}")
        elif rc != 0:
            tests_passed = False
            echo(f"ankifix: {ticket_id} apply test failed: {cmd}\n{out}\n{err}")

    # Live tests: the real app, offscreen, with the fix loaded. They are the
    # proof a user-visible fix works; unit tests against stubbed Qt are not.
    live_tests = list(fix.get("live_tests") or [])
    live_ok, live_results = True, []
    if live_tests:
        live_ok, live_results = run_live_tests(cfg, repo, live_tests, env, echo=echo)
    missing_live = cfg.require_live_tests and not live_tests

    ticket["fix"]["applied"] = {"at": now_iso(), "patch": PATCH_NAME, "tests_passed": tests_passed}
    if live_tests:
        ticket["fix"]["applied"]["live_tests_passed"] = live_ok
        ticket["fix"]["applied"]["live_tests"] = live_results
    if tests_skipped:
        ticket["fix"]["applied"]["tests_skipped"] = tests_skipped
    if timed_out_tests:
        ticket["fix"]["applied"]["tests_timed_out"] = timed_out_tests

    # The apply step is the authority for the ticket's final status: it is
    # the only place that actually re-runs the fix's own tests against the
    # live checkout. A ticket the fixer reported fixed (status fixed or
    # needs-review) is corrected here, explicitly and logged, once known-
    # failing pre-existing tests are excluded - so a fix whose own tests are
    # green does not sit in needs-review just because two unrelated,
    # already-broken tests were on the harness list.
    prior_status = ticket.get("status")
    good = tests_passed and live_ok and not missing_live
    not_landed = ""
    if not live_ok:
        failed = [r["test"] for r in live_results if not r["ok"]]
        not_landed = f"live test failed: {', '.join(failed)}"
    elif missing_live:
        not_landed = "no live test"
    elif not tests_passed:
        not_landed = "tests failed"
    if prior_status in ("fixed", "needs-review"):
        new_status = "fixed" if good else "needs-review"
        outcome = "all passed" if good else "failures remain"
        echo(
            f"ankifix: {ticket_id}: apply re-ran {len(tests_run)} tests, {len(tests_skipped)} "
            f"skipped (known failing), {len(live_tests)} live, {outcome} -> {new_status}"
        )
        ticket["status"] = new_status
    if not_landed:
        ticket["fix"]["applied"]["landed"] = {"state": "not-landed", "at": now_iso(),
                                              "reason": not_landed}
        summary = ticket["fix"].get("summary") or ""
        ticket["fix"]["summary"] = (f"{not_landed}; fix NOT landed | {summary}").rstrip(" |")

    save_ticket(cfg.tickets_dir, ticket)
    echo(f"ankifix: {ticket_id} applied to {repo} (tests_passed={tests_passed}, "
         f"live_tests_passed={live_ok if live_tests else 'none'})")
    if not_landed:
        notify(cfg, f"ankifix: {ticket_id} {not_landed}, not landed")
        return ticket
    if good and prior_status in ("fixed", "needs-review") and cfg.auto_restart_app:
        from . import restart as restart_mod

        try:
            restart_mod.land(cfg, ticket_id, echo=echo)
        except Exception as e:  # noqa: BLE001 - landing never fails the apply
            echo(f"ankifix: landing {ticket_id} failed: {type(e).__name__}: {e}")
        return load_ticket(cfg.tickets_dir, ticket_id)
    notify(cfg, f"ankifix: {ticket_id} applied, restart Anki+")
    return ticket
