"""Run one ticket through `claude -p` and record the outcome in ticket.json."""
from __future__ import annotations

import errno
import fcntl
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import traceback
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

from . import prompts
from .classify import classify
from .config import Config
from .tickets import load_ticket, now_iso, save_ticket, ticket_dir

LOG_NAME = "fix.log"
ERROR_LOG_NAME = "error.log"
LOCK_NAME = ".ankifix.lock"
# fix.blocked value for a deck ticket the watcher cannot read (macOS TCC)
FILES_ACCESS = "files-access"
CLOUD_STORAGE = Path.home() / "Library/CloudStorage"

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


# ------------------------------------------------------ files access (TCC)
def is_files_access_error(e: BaseException) -> bool:
    """macOS TCC refusal: open() of a protected folder raises EPERM (errno 1),
    not EACCES, for a process with no Files and Folders / Full Disk Access
    grant (a launchd agent reading ~/Library/CloudStorage, for example)."""
    return isinstance(e, PermissionError) and e.errno == errno.EPERM


def tcc_binary() -> str:
    """The binary macOS TCC attributes this process's file access to.

    The ankibug venv python is a symlink chain to the CommandLineTools
    python3, a framework build whose bin/python3.9 re-execs
    Python3.framework/Versions/3.9/Resources/Python.app/Contents/MacOS/Python;
    that app bundle is what Full Disk Access has to be granted to."""
    real = Path(os.path.realpath(sys.executable))
    if ".framework/Versions/" in str(real):
        app = real.parent.parent / "Resources" / "Python.app"
        if app.is_dir():
            return str(app)
    return str(real)


def files_access_summary(ticket_id: str, path: Any = None) -> str:
    where = f" ({path} is under {CLOUD_STORAGE})" if path and str(path).startswith(str(CLOUD_STORAGE)) else (
        f" ({path})" if path else "")
    return (
        f"deck fixes need Files access for the watcher{where}; run `ankifix {ticket_id}` "
        f"from a terminal, or grant Full Disk Access to {tcc_binary()} in System Settings "
        "> Privacy & Security"
    )


def probe_files_access(build_dir: Path) -> Optional[PermissionError]:
    """Try to list build_dir and open a file in it. Returns the EPERM error if
    TCC blocks it, else None (other errors are left to the real run)."""
    try:
        names = sorted(os.listdir(build_dir))
        for name in ("build_out.json", "build_deck.py"):
            if name in names:
                with open(Path(build_dir) / name, "rb") as fh:
                    fh.read(1)
                break
    except PermissionError as e:
        if is_files_access_error(e):
            return e
    except OSError:
        pass
    return None


def write_error_log(tdir: Path, ticket_id: str, e: BaseException) -> Optional[Path]:
    """Append the traceback of e to <ticket>/error.log. Never raises."""
    try:
        tdir.mkdir(parents=True, exist_ok=True)
        path = tdir / ERROR_LOG_NAME
        with open(path, "a") as fh:
            fh.write(f"=== {now_iso()} ankifix {ticket_id}: {type(e).__name__}: {e}\n")
            fh.write("".join(traceback.format_exception(type(e), e, e.__traceback__)))
            fh.write("\n")
        return path
    except OSError:
        return None


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
    # explicit, so claude (and the node/git/python3 it runs) resolve even
    # under launchd's minimal PATH
    env["PATH"] = cfg.subprocess_path()
    env.setdefault("HOME", str(Path.home()))
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


def new_fix(started: Optional[str] = None, prev: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """A schema-complete ticket.fix, carrying attempts over from prev."""
    fix: Dict[str, Any] = {
        "branch": None, "commits": [], "tests": [], "log_path": LOG_NAME,
        "started": started, "finished": None, "summary": None,
    }
    if prev and prev.get("attempts"):
        fix["attempts"] = prev["attempts"]
    return fix


def run_ticket(
    ticket_id: str, cfg: Config, kind_override: Optional[str] = None,
    force: bool = False, echo=print, retry_on_error: bool = False,
    from_terminal: bool = False,
) -> Dict[str, Any]:
    """Run one ticket. Never raises for a failure inside the run (only for a
    refused status or a missing ticket): the outcome is recorded on the
    ticket. retry_on_error (watch mode) puts a ticket whose run raised back
    to status new until cfg.max_attempts; from_terminal is the Terminal.app
    child of the TCC fallback (no fallback of its own, attempt not counted)."""
    ticket = load_ticket(cfg.tickets_dir, ticket_id)
    status = ticket.get("status")
    prev_fix = ticket.get("fix") if isinstance(ticket.get("fix"), dict) else {}
    # a ticket parked because the watcher had no Files access may be run from
    # a terminal without --force; that is the documented remedy
    parked = status == "needs-review" and prev_fix.get("blocked") == FILES_ACCESS
    if status in ("fixed", "needs-review", "wontfix", "fixing") and not force and not parked:
        raise AnkifixError(f"ticket {ticket_id} is {status}; pass --force to run anyway")
    tdir = ticket_dir(cfg.tickets_dir, ticket_id)
    log_path = tdir / LOG_NAME
    started = now_iso()
    t0 = time.time()

    ticket["status"] = "fixing"
    ticket["fix"] = new_fix(started, prev_fix)
    ticket["fix"]["pid"] = os.getpid()
    if not from_terminal:
        ticket["fix"]["attempts"] = int(prev_fix.get("attempts") or 0) + 1
    attempts = int(ticket["fix"].get("attempts") or 1)
    summary_extra: List[str] = []
    kind = ticket.get("kind")
    deck_dir: Optional[Path] = None
    try:
        save_ticket(cfg.tickets_dir, ticket)
        # plan() reads build_out.json for deck tickets, so it is inside the
        # try: any failure here is recorded on the ticket, never raised
        # past the watcher.
        kind, reason = classify(ticket, cfg, kind_override)
        if not kind_override and ticket.get("kind") in ("app", "deck"):
            kind, reason = ticket["kind"], "kind already set on ticket"
        if kind == "deck":
            deck_dir = prompts.deck_build_dir(ticket, cfg)
            blocked = probe_files_access(deck_dir)
            if blocked:
                if cfg.terminal_fallback and not from_terminal:
                    return run_via_terminal(ticket, cfg, blocked, deck_dir, echo=echo)
                raise blocked
        p = plan(ticket, cfg, kind_override)
        kind = p["kind"]
        ticket["kind"] = kind
        ticket["fix"]["branch"] = cfg.branch_name(ticket_id) if kind == "app" else None
        save_ticket(cfg.tickets_dir, ticket)
        echo(f"ankifix: {ticket_id} kind={kind} ({p['reason']})")

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
        ticket["kind"] = kind if kind in ("app", "deck") else ticket.get("kind")
        err_log = write_error_log(tdir, ticket_id, e)
        if is_files_access_error(e):
            ticket["status"] = "needs-review"
            ticket["fix"]["blocked"] = FILES_ACCESS
            summary = files_access_summary(ticket_id, getattr(e, "filename", None) or deck_dir)
            summary += f" | {type(e).__name__}: {e}"
        elif isinstance(e, (KeyboardInterrupt, SystemExit)):
            ticket["status"] = "failed"
            summary = f"ankifix error: {type(e).__name__}: {e}"
        elif retry_on_error and attempts < cfg.max_attempts:
            ticket["status"] = "new"
            summary = (f"ankifix error on attempt {attempts} of {cfg.max_attempts}, "
                       f"will retry: {type(e).__name__}: {e}")
        else:
            ticket["status"] = "failed"
            gave_up = f"gave up after {attempts} attempts: " if retry_on_error else ""
            summary = f"{gave_up}ankifix error: {type(e).__name__}: {e}"
        if summary_extra:
            summary += " | " + " | ".join(summary_extra)
        if err_log:
            summary += f" | traceback in {err_log}"
        ticket["fix"].update(finished=now_iso(), summary=summary)
        if err_log:
            ticket["fix"]["error_log"] = ERROR_LOG_NAME
        try:
            save_ticket(cfg.tickets_dir, ticket)
        except Exception as save_err:  # noqa: BLE001
            echo(f"ankifix: {ticket_id}: could not save ticket.json: {save_err}")
        if isinstance(e, (KeyboardInterrupt, SystemExit)):
            raise
        echo(f"ankifix: {ticket_id} {ticket['status']}: {summary}")
        return ticket
    save_ticket(cfg.tickets_dir, ticket)
    echo(f"ankifix: {ticket_id} -> {ticket['status']}: {ticket['fix']['summary']}")
    return ticket


def _applescript_str(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def terminal_command(cfg: Config, ticket_id: str) -> List[str]:
    """osascript argv that runs `<ankifix_bin> <id> --once-from-terminal` in
    a new Terminal.app window (Terminal has Files access; launchd agents do not)."""
    import shlex

    shell = f"{shlex.quote(cfg.ankifix_bin)} {shlex.quote(ticket_id)} --once-from-terminal; exit"
    script = f'tell application "Terminal" to do script {_applescript_str(shell)}'
    return [cfg.osascript_bin, "-e", script]


def run_via_terminal(
    ticket: Dict[str, Any], cfg: Config, blocked: BaseException, deck_dir: Optional[Path],
    echo=print,
) -> Dict[str, Any]:
    """TCC fallback for a deck ticket: hand it to Terminal.app and wait for
    the child to finish ticket.json. If osascript is refused (Automation
    permission) or never finishes, park the ticket as needs-review with the
    Full Disk Access instructions."""
    ticket_id = ticket["id"]
    ticket["kind"] = "deck"
    ticket["fix"]["delegated"] = "terminal"
    ticket["fix"]["summary"] = "no Files access under launchd; handed to Terminal.app"
    save_ticket(cfg.tickets_dir, ticket)
    cmd = terminal_command(cfg, ticket_id)
    echo(f"ankifix: {ticket_id}: no Files access ({blocked}); running it in Terminal.app")
    err = None
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=cfg.osascript_timeout_s,
                           env={**os.environ, "PATH": cfg.subprocess_path()})
        if r.returncode != 0:
            err = (r.stderr or r.stdout).strip() or f"osascript exited {r.returncode}"
    except subprocess.TimeoutExpired:
        err = f"osascript did not return in {cfg.osascript_timeout_s:g}s (Automation prompt unanswered?)"
    except OSError as e:
        err = f"{type(e).__name__}: {e}"

    if err is None:
        deadline = time.time() + cfg.terminal_fallback_timeout_min * 60
        # the child rewrites ticket.json when it starts (new fix.pid) and ends
        while time.time() < deadline:
            time.sleep(cfg.terminal_poll_s)
            try:
                cur = load_ticket(cfg.tickets_dir, ticket_id)
            except (OSError, ValueError):
                continue
            if cur.get("status") not in ("fixing", "new"):
                echo(f"ankifix: {ticket_id} -> {cur.get('status')} (Terminal run)")
                return cur
        err = f"Terminal run did not finish within {cfg.terminal_fallback_timeout_min:g} min"
        try:
            cur = load_ticket(cfg.tickets_dir, ticket_id)
            if cur.get("fix", {}).get("pid") not in (None, os.getpid()) and _pid_alive(cur["fix"]["pid"]):
                # still running in Terminal; leave it to finish on its own
                echo(f"ankifix: {ticket_id}: {err}; left running in Terminal")
                return cur
        except (OSError, ValueError, KeyError):
            pass

    ticket["status"] = "needs-review"
    ticket["fix"]["blocked"] = FILES_ACCESS
    ticket["fix"]["finished"] = now_iso()
    ticket["fix"]["summary"] = (
        files_access_summary(ticket_id, getattr(blocked, "filename", None) or deck_dir)
        + f" | Terminal fallback failed: {err}"
    )
    save_ticket(cfg.tickets_dir, ticket)
    echo(f"ankifix: {ticket_id} needs-review: {ticket['fix']['summary']}")
    return ticket


def _pid_alive(pid: Any) -> bool:
    try:
        os.kill(int(pid), 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except (TypeError, ValueError, OSError):
        return False


def recover_stale(cfg: Config, echo=print) -> List[str]:
    """Call with the ticket lock held. A ticket still "fixing" whose fix.pid
    is gone was orphaned by a watcher that died mid-run (Mac slept, killed):
    requeue it as new, or give up at cfg.max_attempts."""
    from .tickets import list_tickets

    touched = []
    for t in list_tickets(cfg.tickets_dir):
        if t.get("status") != "fixing":
            continue
        fix = t.get("fix") if isinstance(t.get("fix"), dict) else {}
        pid = fix.get("pid")
        if pid and _pid_alive(pid):
            continue
        attempts = int(fix.get("attempts") or 1)
        fix = {**new_fix(fix.get("started"), fix), **fix}
        fix["finished"] = now_iso()
        if attempts >= cfg.max_attempts:
            t["status"] = "failed"
            fix["summary"] = (f"gave up after {attempts} attempts: the run was interrupted "
                              "(watcher died mid-run)")
        else:
            t["status"] = "new"
            fix["summary"] = f"attempt {attempts} was interrupted (watcher died mid-run); requeued"
        t["fix"] = fix
        try:
            save_ticket(cfg.tickets_dir, t)
            touched.append(t["id"])
            echo(f"ankifix: {t['id']}: {fix['summary']}")
        except Exception as e:  # noqa: BLE001
            echo(f"ankifix: {t['id']}: could not recover stale ticket: {e}")
    return touched


def next_new(cfg: Config, skip: Optional[set] = None) -> Optional[str]:
    from .tickets import list_tickets

    for t in list_tickets(cfg.tickets_dir):
        if t.get("status") == "new" and t["id"] not in (skip or ()):
            return t["id"]
    return None


def record_crash(cfg: Config, ticket_id: str, e: BaseException, echo=print) -> None:
    """Last-resort bookkeeping when run_ticket itself raised: mark the ticket
    failed with the error and write the traceback to <ticket>/error.log.
    Never raises."""
    tdir = ticket_dir(cfg.tickets_dir, ticket_id)
    err_log = write_error_log(tdir, ticket_id, e)
    try:
        ticket = load_ticket(cfg.tickets_dir, ticket_id)
        prev = ticket.get("fix") if isinstance(ticket.get("fix"), dict) else {}
        fix = {**new_fix(prev.get("started") or now_iso(), prev), **prev}
        attempts = int(prev.get("attempts") or 0)
        if ticket.get("status") in ("new", None):
            attempts += 1  # run_ticket raised before it could count this one
        fix["attempts"] = attempts
        fix.update(
            finished=now_iso(),
            summary=f"ankifix crashed: {type(e).__name__}: {e}"
            + (f" | traceback in {err_log}" if err_log else ""),
        )
        if err_log:
            fix["error_log"] = ERROR_LOG_NAME
        ticket["fix"] = fix
        ticket["status"] = "failed"
        save_ticket(cfg.tickets_dir, ticket)
    except Exception as e2:  # noqa: BLE001
        echo(f"ankifix: {ticket_id}: could not record the crash on ticket.json: {e2}")


def watch(cfg: Config, interval: float, once: bool = False, echo=print) -> int:
    """Process status=new tickets one at a time, oldest first.

    One ticket can never take the watcher down: any exception out of
    run_ticket (or the apply step) is recorded on that ticket and the loop
    moves on. A ticket whose run raised is retried on the next poll, up to
    cfg.max_attempts. Tickets that crashed in this process are also skipped
    for the rest of its life, so a ticket.json that cannot even be rewritten
    does not spin. Every cfg.doctor_interval_s a light `ankifix doctor` runs
    and a check that flips to FAIL posts a notification."""
    from . import apply as apply_mod  # local: apply imports this module
    from . import doctor as doctor_mod

    echo(f"ankifix: watcher started pid {os.getpid()} at {now_iso()} (interval {interval:g}s)")
    handled = 0
    skip: set = set()
    last_doctor = 0.0
    doctor_state: Dict[str, bool] = {}
    while True:
        if cfg.doctor_interval_s and time.time() - last_doctor >= cfg.doctor_interval_s:
            last_doctor = time.time()
            try:
                doctor_state = doctor_mod.watch_check(cfg, doctor_state, echo=echo)
            except Exception as e:  # noqa: BLE001
                echo(f"ankifix: doctor error: {type(e).__name__}: {e}")
        try:
            with ticket_lock(cfg.tickets_dir):
                recover_stale(cfg, echo=echo)
                this_pass: set = set()
                while True:
                    tid = next_new(cfg, skip | this_pass)
                    if not tid:
                        break
                    this_pass.add(tid)  # a retried ticket waits for the next poll
                    try:
                        result = run_ticket(tid, cfg, echo=echo, retry_on_error=True)
                    except Exception as e:  # noqa: BLE001
                        skip.add(tid)
                        handled += 1
                        echo(f"ankifix: {tid} crashed: {type(e).__name__}: {e}")
                        record_crash(cfg, tid, e, echo=echo)
                        notify(cfg, f"ankifix: {tid} failed")
                        continue
                    handled += 1
                    try:
                        on_disk = load_ticket(cfg.tickets_dir, tid).get("status")
                    except Exception:  # noqa: BLE001
                        on_disk = None
                    if on_disk is None or (on_disk == "new" and result.get("status") != "new"):
                        skip.add(tid)  # result could not be saved; do not spin on it
                    notify(cfg, f"ankifix: {tid} {result.get('status')}")
                    if (
                        cfg.auto_apply
                        and result.get("kind") == "app"
                        and result.get("status") in ("fixed", "needs-review")
                    ):
                        try:
                            apply_mod.apply_ticket(tid, cfg, echo=echo)
                        except Exception as e:  # noqa: BLE001
                            echo(f"ankifix: auto-apply for {tid} failed: {type(e).__name__}: {e}")
        except LockBusy as e:
            echo(f"ankifix: {e}; waiting")
        except Exception as e:  # noqa: BLE001 - keep the watcher alive
            echo(f"ankifix: watch loop error: {type(e).__name__}: {e}")
            echo(traceback.format_exc())
        if once:
            return handled
        time.sleep(interval)
