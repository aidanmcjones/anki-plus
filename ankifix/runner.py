"""Run one ticket through `claude -p` and record the outcome in ticket.json."""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

from . import prompts
from .classify import classify
from .config import Config
from .tickets import load_ticket, now_iso, save_ticket, ticket_dir

LOG_NAME = "fix.log"
LOCK_NAME = ".ankifix.lock"

TEST_PATTERNS = [
    r"node\s+tests/[\w./-]+\.cjs",
    r"python3?\s+-m\s+pytest[^\n;&|]*",
    r"python3?\s+tests/test_[\w-]+\.py",
    r"\S*python\S*\s+build_deck\.py",
    r"\S*python\S*\s+verify\.py",
]


class AnkifixError(RuntimeError):
    pass


class LockBusy(AnkifixError):
    pass


# --------------------------------------------------------------------- lock
@contextmanager
def ticket_lock(tickets_dir: Path) -> Iterator[None]:
    Path(tickets_dir).mkdir(parents=True, exist_ok=True)
    path = Path(tickets_dir) / LOCK_NAME
    fh = open(path, "a+")
    try:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            fh.seek(0)
            raise LockBusy(f"another ankifix holds {path}: {fh.read().strip()}")
        fh.seek(0)
        fh.truncate()
        fh.write(f"pid {os.getpid()} since {now_iso()}\n")
        fh.flush()
        yield
    finally:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        finally:
            fh.close()


# ------------------------------------------------------------------ notify
def notify(cfg: Config, message: str) -> None:
    """Best-effort macOS notification. No-op if disabled or nothing to post with."""
    if not cfg.notify:
        return
    try:
        if shutil.which("terminal-notifier"):
            subprocess.run(
                ["terminal-notifier", "-title", "ankifix", "-message", message],
                check=False, capture_output=True, timeout=5,
            )
            return
        if shutil.which("osascript"):
            script = 'display notification {} with title "ankifix"'.format(json.dumps(message))
            subprocess.run(["osascript", "-e", script], check=False, capture_output=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        pass  # a notification failure must never fail the run


# ---------------------------------------------------------------------- git
def git(cwd: Path, *args: str, check: bool = True) -> str:
    p = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)
    if check and p.returncode != 0:
        raise AnkifixError(f"git {' '.join(args)} failed in {cwd}: {p.stderr.strip()}")
    return p.stdout.strip()


def prepare_worktree(cfg: Config, ticket_id: str) -> Path:
    repo = Path(cfg.app_repo)
    wt = cfg.worktree_path(ticket_id)
    branch = cfg.branch_name(ticket_id)
    if wt.exists():
        cur = git(wt, "rev-parse", "--abbrev-ref", "HEAD", check=False)
        if cur != branch:
            raise AnkifixError(f"{wt} exists but is on {cur!r}, not {branch!r}")
        return wt  # re-run on an existing fix worktree
    git(repo, "rev-parse", "--verify", "-q", f"refs/heads/{cfg.app_base_branch}")
    wt.parent.mkdir(parents=True, exist_ok=True)
    if git(repo, "rev-parse", "--verify", "-q", f"refs/heads/{branch}", check=False):
        git(repo, "worktree", "add", str(wt), branch)
    else:
        git(repo, "worktree", "add", "-b", branch, str(wt), cfg.app_base_branch)
    return wt


def branch_commits(cfg: Config, wt: Path) -> List[str]:
    out = git(wt, "log", "--format=%h", f"{cfg.app_base_branch}..HEAD", check=False)
    return [c for c in out.splitlines() if c]


def branch_pushed(cfg: Config, wt: Path, ticket_id: str) -> bool:
    remote = git(
        wt, "rev-parse", "--verify", "-q",
        f"refs/remotes/{cfg.app_remote}/{cfg.branch_name(ticket_id)}", check=False,
    )
    return bool(remote) and remote == git(wt, "rev-parse", "HEAD", check=False)


# ------------------------------------------------------------------- claude
def _fmt(rules: List[str], **kw: str) -> List[str]:
    return [r.format(**kw) for r in rules]


def build_command(cfg: Config, kind: str, ticket_id: str, add_dirs: List[Path]) -> List[str]:
    if kind == "app":
        kw = dict(
            remote=cfg.app_remote, branch=cfg.branch_name(ticket_id),
            base=cfg.app_base_branch, anki_python=cfg.anki_python,
        )
        allowed = _fmt(cfg.app_allowed_tools, **kw)
        denied = _fmt(cfg.app_disallowed_tools, **kw)
    else:
        kw = dict(deck_python=cfg.deck_python)
        allowed = _fmt(cfg.deck_allowed_tools, **kw)
        denied = _fmt(cfg.deck_disallowed_tools, **kw)
    cmd = [
        cfg.claude_bin, "-p",
        "--output-format", "stream-json", "--verbose",
        "--max-turns", str(cfg.max_turns),
        "--permission-mode", cfg.permission_mode,
        "--allowedTools", ",".join(allowed),
        "--disallowedTools", ",".join(denied),
        "--name", f"ankifix {ticket_id}",
    ]
    for d in add_dirs:
        cmd += ["--add-dir", str(d)]
    if cfg.max_budget_usd:
        cmd += ["--max-budget-usd", str(cfg.max_budget_usd)]
    if cfg.model:
        cmd += ["--model", cfg.model]
    return cmd


def run_claude(
    cmd: List[str], prompt: str, cwd: Path, env: Dict[str, str], log_path: Path,
    timeout_s: float, meta: Dict[str, Any],
) -> Tuple[Optional[int], bool]:
    """Stream claude's stdout (stream-json) into log_path. Returns (rc, timed_out)."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "w") as log:
        log.write(json.dumps({"type": "ankifix", "event": "start", **meta}) + "\n")
        log.flush()
        proc = subprocess.Popen(
            cmd, cwd=str(cwd), env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, start_new_session=True,
        )
        lock = threading.Lock()

        def pump() -> None:
            assert proc.stdout is not None
            for line in proc.stdout:
                with lock:
                    log.write(line if line.endswith("\n") else line + "\n")
                    log.flush()

        t = threading.Thread(target=pump, daemon=True)
        t.start()
        try:
            assert proc.stdin is not None
            proc.stdin.write(prompt)
            proc.stdin.close()
        except BrokenPipeError:
            pass
        timed_out = False
        try:
            rc: Optional[int] = proc.wait(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            timed_out = True
            for sig in (signal.SIGTERM, signal.SIGKILL):
                try:
                    os.killpg(proc.pid, sig)
                except ProcessLookupError:
                    break
                try:
                    proc.wait(timeout=10)
                    break
                except subprocess.TimeoutExpired:
                    continue
            rc = proc.returncode
        t.join(timeout=10)
        with lock:
            log.write(json.dumps({
                "type": "ankifix", "event": "end", "rc": rc, "timed_out": timed_out, "at": now_iso(),
            }) + "\n")
    return rc, timed_out


def parse_transcript(log_path: Path) -> Dict[str, Any]:
    """Pull the final result, the ankifix-result block and the test commands."""
    out: Dict[str, Any] = {
        "result_text": "", "result_event": None, "block": None, "tests": [],
        "session_id": None, "bash_commands": [],
    }
    if not log_path.is_file():
        return out
    for line in log_path.read_text(errors="replace").splitlines():
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if not isinstance(ev, dict):
            continue
        if ev.get("session_id") and not out["session_id"]:
            out["session_id"] = ev["session_id"]
        if ev.get("type") == "assistant":
            for block in (ev.get("message") or {}).get("content") or []:
                if block.get("type") == "tool_use" and block.get("name") == "Bash":
                    c = (block.get("input") or {}).get("command")
                    if c:
                        out["bash_commands"].append(c)
                if block.get("type") == "text" and "ankifix-result" in (block.get("text") or ""):
                    out["result_text"] = block["text"]
        elif ev.get("type") == "result":
            out["result_event"] = ev
            if ev.get("result"):
                out["result_text"] = ev["result"]
    m = None
    for m in re.finditer(r"```ankifix-result\s*\n(.*?)```", out["result_text"], re.S):
        pass
    if m:
        try:
            out["block"] = json.loads(m.group(1))
        except ValueError:
            out["block"] = None
    seen: List[str] = []
    for c in out["bash_commands"]:
        for pat in TEST_PATTERNS:
            for hit in re.findall(pat, c):
                hit = hit.strip()
                if hit not in seen:
                    seen.append(hit)
    out["tests"] = seen
    return out


# ----------------------------------------------------------------- deck io
def _snapshot(build_dir: Path) -> Dict[str, str]:
    files = sorted((build_dir / "cards").glob("*.yaml")) + [build_dir / "build_deck.py"]
    return {
        str(f.relative_to(build_dir)): hashlib.sha256(f.read_bytes()).hexdigest()
        for f in files if f.is_file()
    }


def _backup_deck(build_dir: Path, dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    for rel in _snapshot(build_dir):
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(build_dir / rel, target)


def _preflight_pass(build_dir: Path, since: float) -> bool:
    rep = build_dir / "preflight_report.txt"
    if not rep.is_file() or rep.stat().st_mtime < since:
        return False
    lines = [l for l in rep.read_text(errors="replace").splitlines() if l.startswith("RESULT:")]
    return bool(lines) and lines[-1].startswith("RESULT: PASS")


# -------------------------------------------------------------------- main
def child_env(cfg: Config, ticket_id: str, kind: str) -> Dict[str, str]:
    env = dict(os.environ)
    for k in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT"):
        env.pop(k, None)
    env["NODE_PATH"] = cfg.node_path
    env["ANKIFIX_TICKET_ID"] = ticket_id
    env["ANKIFIX_KIND"] = kind
    return env


def plan(ticket: Dict[str, Any], cfg: Config, kind_override: Optional[str] = None) -> Dict[str, Any]:
    kind, reason = classify(ticket, cfg, kind_override)
    if not kind_override and ticket.get("kind") in ("app", "deck"):
        kind, reason = ticket["kind"], "kind already set on ticket"
    tdir = ticket_dir(cfg.tickets_dir, ticket["id"])
    if kind == "app":
        cwd = cfg.worktree_path(ticket["id"])
        add_dirs = [tdir]
    else:
        cwd = prompts.deck_build_dir(ticket, cfg)
        cdm = prompts.find_course_deck_md(cwd)
        add_dirs = [tdir] + ([cdm.parent] if cdm and cdm.parent != cwd else [])
    return {
        "kind": kind, "reason": reason, "cwd": cwd,
        "cmd": build_command(cfg, kind, ticket["id"], add_dirs),
        "prompt": prompts.render(ticket, kind, cfg),
    }


def run_ticket(
    ticket_id: str, cfg: Config, kind_override: Optional[str] = None,
    force: bool = False, echo=print,
) -> Dict[str, Any]:
    ticket = load_ticket(cfg.tickets_dir, ticket_id)
    status = ticket.get("status")
    if status in ("fixed", "needs-review", "wontfix", "fixing") and not force:
        raise AnkifixError(f"ticket {ticket_id} is {status}; pass --force to run anyway")
    tdir = ticket_dir(cfg.tickets_dir, ticket_id)
    log_path = tdir / LOG_NAME
    started = now_iso()
    t0 = time.time()

    p = plan(ticket, cfg, kind_override)
    kind = p["kind"]
    ticket["kind"] = kind
    ticket["status"] = "fixing"
    ticket["fix"] = {
        "branch": cfg.branch_name(ticket_id) if kind == "app" else None,
        "commits": [], "tests": [], "log_path": LOG_NAME,
        "started": started, "finished": None, "summary": None,
    }
    save_ticket(cfg.tickets_dir, ticket)
    echo(f"ankifix: {ticket_id} kind={kind} ({p['reason']})")

    summary_extra: List[str] = []
    try:
        if kind == "app":
            cwd = prepare_worktree(cfg, ticket_id)
            p["prompt"] = prompts.render(ticket, kind, cfg)  # CLAUDE.md now from the worktree
        else:
            cwd = p["cwd"]
            if not (cwd / "build_deck.py").is_file():
                raise AnkifixError(f"no build_deck.py in {cwd}")
            _backup_deck(cwd, tdir / "deck_before")
            before = _snapshot(cwd)
        echo(f"ankifix: running claude in {cwd} (max {cfg.max_turns} turns, {cfg.wall_clock_minutes} min)")
        rc, timed_out = run_claude(
            p["cmd"], p["prompt"], cwd, child_env(cfg, ticket_id, kind), log_path,
            cfg.wall_clock_minutes * 60,
            {"ticket": ticket_id, "kind": kind, "cwd": str(cwd), "cmd": p["cmd"], "at": started},
        )
        parsed = parse_transcript(log_path)
        block = parsed["block"] or {}
        rev = parsed["result_event"] or {}
        # `reported_fixed`: claude's own result block says it fixed the ticket.
        # `tests_green`: claude's own result block says the harness is fully green.
        # A fix can exist (branch/commits, or a changed+preflight-passed deck)
        # without the harness being fully green (e.g. unrelated pre-existing
        # failures the fixer could not clear) - that lands as needs-review,
        # not failed, so a human looks at it instead of it silently vanishing.
        reported_fixed = block.get("status") == "fixed"
        tests_green = bool(block.get("tests_green"))
        tests = parsed["tests"] or list(block.get("tests") or [])

        if kind == "app":
            commits = branch_commits(cfg, cwd)
            pushed = branch_pushed(cfg, cwd, ticket_id)
            has_fix = bool(commits) and reported_fixed and not timed_out
            summary_extra.append(f"worktree {cwd}; {'pushed' if pushed else 'NOT pushed'} to {cfg.app_remote}")
        else:
            commits = []
            changed = [k for k, v in _snapshot(cwd).items() if before.get(k) != v]
            passed = _preflight_pass(cwd, t0)
            has_fix = bool(changed) and passed and reported_fixed and not timed_out
            summary_extra.append(f"changed: {', '.join(changed) or 'nothing'}")
            summary_extra.append(f"preflight {'RESULT: PASS' if passed else 'did not pass (or not rebuilt)'}")
            summary_extra.append(f"pre-run copies in {tdir / 'deck_before'}")
            if block.get("apply_command"):
                summary_extra.append(f"APPLY WITH ANKI+ CLOSED: {block['apply_command']}")
            if passed and not any("build_deck.py" in t for t in tests):
                tests.append("build_deck.py preflight: RESULT: PASS")

        if timed_out:
            summary_extra.insert(0, f"killed at wall clock limit ({cfg.wall_clock_minutes} min)")
        elif rev.get("subtype") and rev.get("subtype") != "success":
            summary_extra.insert(0, f"claude ended with {rev.get('subtype')}")
        elif rc not in (0, None):
            summary_extra.insert(0, f"claude exited {rc}")
        if not parsed["block"]:
            summary_extra.insert(0, "no ankifix-result block in transcript")
        if has_fix and not tests_green:
            summary_extra.insert(0, "fix branch exists but tests_green is false; needs human review")
        if rev.get("total_cost_usd") is not None:
            summary_extra.append(f"cost ${rev['total_cost_usd']:.2f}, {rev.get('num_turns')} turns")
        if parsed["session_id"]:
            summary_extra.append(f"session {parsed['session_id']}")

        claude_summary = block.get("summary") or (parsed["result_text"] or "").strip()[-600:]
        ticket["fix"].update(
            commits=commits, tests=tests, finished=now_iso(),
            summary=" | ".join([s for s in [claude_summary] + summary_extra if s]),
        )
        if has_fix and tests_green:
            ticket["status"] = "fixed"
        elif has_fix:
            ticket["status"] = "needs-review"
        else:
            ticket["status"] = "failed"
    except BaseException as e:  # noqa: BLE001 - record any failure, incl. ^C
        ticket["status"] = "failed"
        ticket["fix"].update(
            finished=now_iso(),
            summary=f"ankifix error: {type(e).__name__}: {e}" + (
                " | " + " | ".join(summary_extra) if summary_extra else ""),
        )
        save_ticket(cfg.tickets_dir, ticket)
        if isinstance(e, (KeyboardInterrupt, SystemExit)):
            raise
        echo(f"ankifix: {ticket_id} failed: {e}")
        return ticket
    save_ticket(cfg.tickets_dir, ticket)
    echo(f"ankifix: {ticket_id} -> {ticket['status']}: {ticket['fix']['summary']}")
    return ticket


def next_new(cfg: Config) -> Optional[str]:
    from .tickets import list_tickets

    for t in list_tickets(cfg.tickets_dir):
        if t.get("status") == "new":
            return t["id"]
    return None


def watch(cfg: Config, interval: float, once: bool = False, echo=print) -> int:
    """Process status=new tickets one at a time, oldest first."""
    from . import apply as apply_mod  # local: apply imports this module

    handled = 0
    while True:
        try:
            with ticket_lock(cfg.tickets_dir):
                while True:
                    tid = next_new(cfg)
                    if not tid:
                        break
                    result = run_ticket(tid, cfg, echo=echo)
                    handled += 1
                    notify(cfg, f"ankifix: {tid} {result.get('status')}")
                    if (
                        cfg.auto_apply
                        and result.get("kind") == "app"
                        and result.get("status") in ("fixed", "needs-review")
                    ):
                        try:
                            apply_mod.apply_ticket(tid, cfg, echo=echo)
                        except AnkifixError as e:
                            echo(f"ankifix: auto-apply for {tid} failed: {e}")
        except LockBusy as e:
            echo(f"ankifix: {e}; waiting")
        if once:
            return handled
        time.sleep(interval)
