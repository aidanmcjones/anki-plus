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

# fix.log is a symlink to the latest attempt's transcript, fix.<n>.log; every
# attempt keeps its own file (fix.log used to be reopened with "w", so a
# retry erased the previous attempt's evidence). A pre-existing regular
# fix.log (older runs) is preserved as fix.0.log.
LOG_NAME = "fix.log"
ATTEMPT_LOG_RE = re.compile(r"fix\.(\d+)\.log")
# stdout/stderr of the Terminal.app child (--once-from-terminal), tee'd
TERMINAL_LOG_NAME = "terminal.log"
ERROR_LOG_NAME = "error.log"
LOCK_NAME = ".ankifix.lock"
# ids of Terminal delegations the watcher started whose result it has not
# logged yet; a file (not memory) so a watcher restart still reports them
DELEGATIONS_NAME = ".ankifix-delegations.json"
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


# ---------------------------------------------------------------- evidence
def next_attempt_log(tdir: Path) -> Path:
    """The transcript path for the next attempt: fix.<n+1>.log, n the highest
    existing attempt number. A legacy regular fix.log becomes fix.0.log first."""
    tdir.mkdir(parents=True, exist_ok=True)
    legacy = tdir / LOG_NAME
    if legacy.is_file() and not legacy.is_symlink():
        dest = tdir / "fix.0.log"
        if dest.exists():
            dest = tdir / f"fix.0.{int(time.time())}.log"
        os.replace(legacy, dest)
    nums = [int(m.group(1)) for f in tdir.iterdir() for m in [ATTEMPT_LOG_RE.fullmatch(f.name)] if m]
    return tdir / f"fix.{max(nums, default=0) + 1}.log"


def point_latest_log(log_path: Path) -> None:
    """Make <ticket>/fix.log a symlink to log_path (best effort)."""
    link = log_path.parent / LOG_NAME
    try:
        if link.is_symlink() or link.exists():
            link.unlink()
        link.symlink_to(log_path.name)
    except OSError:
        pass


def tail_file(path: Path, lines: int = 6, limit: int = 600) -> str:
    try:
        text = path.read_text(errors="replace")
    except OSError:
        return ""
    out = " / ".join(l.strip() for l in text.strip().splitlines()[-lines:] if l.strip())
    return out[-limit:]


# ------------------------------------------------------------------- claude
def _fmt(rules: List[str], **kw: str) -> List[str]:
    return [r.format(**kw) for r in rules]


def build_command(cfg: Config, kind: str, ticket_id: str, add_dirs: List[Path]) -> List[str]:
    if kind == "app":
        kw = dict(
            remote=cfg.app_remote, branch=cfg.branch_name(ticket_id),
            base=cfg.app_base_branch, anki_python=cfg.anki_python,
            anki_pyenv=cfg.anki_pyenv, live_harness=cfg.live_harness,
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
    # append, never truncate: log_path is per attempt (next_attempt_log), and
    # nothing that ran before must be erased even if a name is ever reused
    with open(log_path, "a") as log:
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


LIVE_TEST_RE = re.compile(r"tests/live/[\w./-]*?test_[\w.-]+\.py")


def parse_live_tests(block: Dict[str, Any]) -> List[str]:
    """Normalize the result block's `live_tests` to worktree-relative script
    paths (tests/live/test_*.py). Entries may be bare paths or whole harness
    commands; anything that names no tests/live/test_*.py script is dropped."""
    raw = block.get("live_tests") if isinstance(block, dict) else None
    if isinstance(raw, str):
        raw = [raw]
    out: List[str] = []
    for item in raw or []:
        if not isinstance(item, str):
            continue
        for hit in LIVE_TEST_RE.findall(item):
            if hit not in out:
                out.append(hit)
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
        "branch": None, "commits": [], "tests": [], "tests_skipped": [], "live_tests": [],
        "log_path": LOG_NAME,
        "started": started, "finished": None, "summary": None,
    }
    if prev and prev.get("attempts"):
        fix["attempts"] = prev["attempts"]
    if prev and prev.get("kind_flip"):
        fix["kind_flip"] = prev["kind_flip"]  # the one-flip guard survives reruns
    return fix


def _human_touched_since(t: Dict[str, Any], since: Optional[str]) -> bool:
    """True if `ankibug set` (which stamps ticket.manual_set) wrote this ticket
    at or after `since` (an ISO timestamp, e.g. fix.started)."""
    manual = t.get("manual_set")
    if not isinstance(manual, dict) or not manual.get("at") or not since:
        return False
    try:
        import datetime as _dt

        return _dt.datetime.fromisoformat(manual["at"]) >= _dt.datetime.fromisoformat(since)
    except (TypeError, ValueError):
        return False


def commit_result(
    cfg: Config, ticket: Dict[str, Any], started: str, pid: int, manual0: Any, echo=print,
) -> Dict[str, Any]:
    """Write a run's result, unless someone else changed the ticket while it
    ran. Re-reads ticket.json first: if its status left "fixing", fix.started
    or fix.pid is no longer this run's, or `ankibug set` stamped a new
    manual_set, the on-disk ticket (the human's decision) is kept and this
    run's result is recorded under fix.result_ignored instead. The caller
    (watch) never auto-applies a result that was ignored."""
    tid = ticket["id"]
    try:
        disk = load_ticket(cfg.tickets_dir, tid)
    except (OSError, ValueError):
        disk = None
    if disk is not None:
        dfix = disk.get("fix") if isinstance(disk.get("fix"), dict) else None
        ours = (
            disk.get("status") == "fixing"
            and dfix is not None
            and dfix.get("started") == started
            and dfix.get("pid") == pid
            and disk.get("manual_set") == manual0
        )
        if not ours:
            fix = ticket.get("fix") or {}
            ignored = {
                "at": now_iso(), "status": ticket.get("status"), "kind": ticket.get("kind"),
                "summary": fix.get("summary"), "commits": fix.get("commits") or [],
                "log_path": fix.get("log_path"), "started": started,
            }
            if dfix is None:
                dfix = new_fix(started, fix)
                dfix["finished"] = now_iso()
            elif dfix.get("started") == started and dfix.get("finished") is None:
                dfix["finished"] = now_iso()  # this run did end; nothing to requeue
            dfix["result_ignored"] = ignored
            disk["fix"] = dfix
            save_ticket(cfg.tickets_dir, disk)
            echo(
                f"ankifix: {tid}: changed by someone else during the run (now "
                f"{disk.get('status')!r}); keeping that, this run's {ignored['status']!r} "
                "result is in fix.result_ignored and will not be applied"
            )
            return disk
    save_ticket(cfg.tickets_dir, ticket)
    return ticket


def run_ticket(
    ticket_id: str, cfg: Config, kind_override: Optional[str] = None,
    force: bool = False, echo=print, retry_on_error: bool = False,
    from_terminal: bool = False, wait_terminal: bool = True,
) -> Dict[str, Any]:
    """Run one ticket. Never raises for a failure inside the run (only for a
    refused status or a missing ticket): the outcome is recorded on the
    ticket. retry_on_error (watch mode) puts a ticket whose run raised back
    to status new until cfg.max_attempts; from_terminal is the Terminal.app
    child of the TCC fallback (no fallback of its own, attempt not counted).
    wait_terminal=False (watch mode) hands a TCC-blocked deck ticket to
    Terminal.app and returns at once with it still "fixing" (delegated); the
    watcher settles it on a later poll (settle_delegations) instead of holding
    the ticket lock for the whole Terminal run.

    fix.owner records who claimed the run ("watcher", "cli" or "terminal");
    recover_stale only requeues a run the watcher itself claimed that never
    finished, never a "fixing" status a human set with `ankibug set`."""
    ticket = load_ticket(cfg.tickets_dir, ticket_id)
    status = ticket.get("status")
    prev_fix = ticket.get("fix") if isinstance(ticket.get("fix"), dict) else {}
    # a ticket parked because the watcher had no Files access may be run from
    # a terminal without --force; that is the documented remedy
    parked = status == "needs-review" and prev_fix.get("blocked") == FILES_ACCESS
    if status in ("fixed", "needs-review", "wontfix", "fixing") and not force and not parked:
        raise AnkifixError(f"ticket {ticket_id} is {status}; pass --force to run anyway")
    tdir = ticket_dir(cfg.tickets_dir, ticket_id)
    started = now_iso()
    t0 = time.time()
    pid = os.getpid()
    manual0 = ticket.get("manual_set")

    ticket["status"] = "fixing"
    ticket["fix"] = new_fix(started, prev_fix)
    ticket["fix"]["pid"] = pid
    if from_terminal:
        ticket["fix"]["owner"] = "terminal"
        ticket["fix"]["delegated"] = "terminal"
        if prev_fix.get("delegated_at"):
            ticket["fix"]["delegated_at"] = prev_fix["delegated_at"]
    else:
        ticket["fix"]["owner"] = "watcher" if retry_on_error else "cli"
        ticket["fix"]["attempts"] = int(prev_fix.get("attempts") or 0) + 1
    attempts = int(ticket["fix"].get("attempts") or 1)
    summary_extra: List[str] = []
    kind = ticket.get("kind")
    deck_dir: Optional[Path] = None

    def commit(t: Dict[str, Any]) -> Dict[str, Any]:
        return commit_result(cfg, t, started, pid, manual0, echo=echo)

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
                    return run_via_terminal(ticket, cfg, blocked, deck_dir, echo=echo,
                                            wait=wait_terminal, commit=commit)
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
        log_path = next_attempt_log(tdir)
        ticket["fix"]["log_path"] = log_path.name
        save_ticket(cfg.tickets_dir, ticket)
        point_latest_log(log_path)
        echo(f"ankifix: running claude in {cwd} (max {cfg.max_turns} turns, {cfg.wall_clock_minutes} min)"
             f"; transcript {log_path}")
        rc, timed_out = run_claude(
            p["cmd"], p["prompt"], cwd, child_env(cfg, ticket_id, kind), log_path,
            cfg.wall_clock_minutes * 60,
            {"ticket": ticket_id, "kind": kind, "cwd": str(cwd), "cmd": p["cmd"], "at": started,
             "attempt_log": log_path.name, "owner": ticket["fix"]["owner"]},
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
        wrong_kind = block.get("status") == "wrong-kind"
        tests_green = bool(block.get("tests_green"))
        all_tests = parsed["tests"] or list(block.get("tests") or [])
        # known-failing pre-existing tests (cfg.known_failing_tests, e.g. the
        # add-on's own editor_tools.cjs/test_editor_crop.py) are excluded from
        # the tests this ticket is judged by; recorded separately (never
        # silently dropped) so `ankifix apply` can skip re-running them too.
        tests = [t for t in all_tests if not cfg.is_known_failing_test(t)]
        tests_skipped = [t for t in all_tests if cfg.is_known_failing_test(t)]
        # live tests (tests/live/*, run in the real app by the harness) are
        # the proof a user-visible fix works; `ankifix apply` re-runs them
        live_tests = parse_live_tests(block) if kind == "app" else []

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
        missing_live = kind == "app" and has_fix and cfg.require_live_tests and not live_tests
        if missing_live:
            summary_extra.insert(
                0, "no live test (tests/live/ in the real app); unit tests alone are not proof, needs human review"
            )
        if live_tests:
            summary_extra.append(f"live tests: {', '.join(live_tests)}")
        if tests_skipped:
            summary_extra.append(f"tests_skipped (known failing): {', '.join(tests_skipped)}")
        if rev.get("total_cost_usd") is not None:
            summary_extra.append(f"cost ${rev['total_cost_usd']:.2f}, {rev.get('num_turns')} turns")
        if parsed["session_id"]:
            summary_extra.append(f"session {parsed['session_id']}")
        summary_extra.append(f"transcript {log_path.name}")

        claude_summary = block.get("summary") or (parsed["result_text"] or "").strip()[-600:]
        ticket["fix"].update(
            commits=commits, tests=tests, tests_skipped=tests_skipped, live_tests=live_tests,
            finished=now_iso(),
            summary=" | ".join([s for s in [claude_summary] + summary_extra if s]),
        )
        if wrong_kind:
            wk_reason = block.get("reason") or block.get("summary") or "(no reason given)"
            if kind == "deck" and not ticket["fix"].get("kind_flip") and not timed_out:
                # the deck fixer says this is an add-on/app bug, not course
                # card content: flip it to app and requeue, ONCE (kind_flip is
                # carried by new_fix, so a second wrong-kind can never flip
                # back; the app prompt has no wrong-kind status at all)
                ticket["kind"] = "app"
                ticket["status"] = "new"
                ticket["fix"]["kind_flip"] = {"from": "deck", "to": "app", "reason": wk_reason,
                                              "at": now_iso()}
                ticket["fix"]["attempts"] = 0  # the app fixer gets its own full retry budget
                ticket["fix"]["summary"] = " | ".join(
                    [f"deck fixer: wrong kind, this is an app bug ({wk_reason}); flipped to "
                     "kind=app and requeued as new for the app fixer"] + summary_extra)
                out = commit(ticket)
                echo(f"ankifix: {ticket_id} -> {out['status']} (kind {out.get('kind')}): {out['fix']['summary']}")
                return out
            ticket["fix"]["summary"] = " | ".join(
                [f"fixer returned wrong-kind for kind={kind} ({wk_reason}) but "
                 + ("the ticket was already flipped once; not flipping again" if ticket["fix"].get("kind_flip")
                    else "only a deck run may flip a ticket")] + summary_extra)
        if has_fix and tests_green and not missing_live:
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
            ticket = commit(ticket)
        except Exception as save_err:  # noqa: BLE001
            echo(f"ankifix: {ticket_id}: could not save ticket.json: {save_err}")
        if isinstance(e, (KeyboardInterrupt, SystemExit)):
            raise
        echo(f"ankifix: {ticket_id} {ticket['status']}: {ticket['fix'].get('summary')}")
        return ticket
    ticket = commit(ticket)
    echo(f"ankifix: {ticket_id} -> {ticket['status']}: {ticket['fix']['summary']}")
    return ticket


def _applescript_str(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def terminal_command(cfg: Config, ticket_id: str) -> List[str]:
    """osascript argv that runs `<ankifix_bin> <id> --once-from-terminal` in
    a new Terminal.app window (Terminal has Files access; launchd agents do
    not). The child's stdout and stderr are tee'd (unbuffered) into
    <ticket>/terminal.log so a failed Terminal run leaves evidence; the
    window still shows it live."""
    import shlex

    tlog = ticket_dir(cfg.tickets_dir, ticket_id) / TERMINAL_LOG_NAME
    shell = (
        f"PYTHONUNBUFFERED=1 {shlex.quote(cfg.ankifix_bin)} {shlex.quote(ticket_id)} "
        f"--once-from-terminal 2>&1 | tee -a {shlex.quote(str(tlog))}; exit"
    )
    script = f'tell application "Terminal" to do script {_applescript_str(shell)}'
    return [cfg.osascript_bin, "-e", script]


def delegation_in_flight(t: Dict[str, Any]) -> bool:
    """A ticket handed to Terminal.app whose run has not finished yet."""
    fix = t.get("fix") if isinstance(t.get("fix"), dict) else {}
    return (
        t.get("status") == "fixing"
        and fix.get("delegated") == "terminal"
        and fix.get("finished") is None
    )


def terminal_result_line(cfg: Config, t: Dict[str, Any]) -> str:
    """The watch-log line for a finished Terminal run: status, kind flip and
    the child's own summary/reason, plus the tail of terminal.log when the
    child left no summary of its own."""
    fix = t.get("fix") if isinstance(t.get("fix"), dict) else {}
    summary = (fix.get("summary") or "").strip()
    line = f"ankifix: {t['id']} -> {t.get('status')} (Terminal run, kind {t.get('kind')})"
    if summary:
        line += f": {summary[:500]}"
    if not summary or "without a result" in summary:
        tail = tail_file(ticket_dir(cfg.tickets_dir, t["id"]) / TERMINAL_LOG_NAME)
        if tail and tail not in summary:
            line += f" | terminal.log: {tail}"
    return line


def settle_delegation(cfg: Config, cur: Dict[str, Any], echo=print,
                      now: Optional[float] = None) -> Optional[Dict[str, Any]]:
    """One check of a Terminal delegation. Returns None while the Terminal
    child is (or may still be) working, else the settled ticket (which is
    logged). Settled means: the ticket left "fixing" (the child's result, a
    wrong-kind flip to new, or a human's `ankibug set`, which is never
    overwritten), the child's recorded pid died without a result (-> failed),
    or no child ever started (-> needs-review, files-access)."""
    now = time.time() if now is None else now
    tid = cur["id"]
    fix = cur.get("fix") if isinstance(cur.get("fix"), dict) else {}
    if not delegation_in_flight(cur) or _human_touched_since(cur, fix.get("started")):
        echo(terminal_result_line(cfg, cur))
        return cur
    pid = fix.get("pid")
    if fix.get("owner") == "terminal":
        if pid is not None and not _pid_alive(pid):
            cur["status"] = "failed"
            fix["finished"] = now_iso()
            fix["summary"] = "terminal run ended without a result"
            tail = tail_file(ticket_dir(cfg.tickets_dir, tid) / TERMINAL_LOG_NAME)
            if tail:
                fix["summary"] += f" | terminal.log: {tail}"
            cur["fix"] = fix
            save_ticket(cfg.tickets_dir, cur)
            echo(f"ankifix: {tid}: terminal child (pid {pid}) is gone but status was 'fixing'; "
                 "treating as an abnormal end")
            echo(terminal_result_line(cfg, cur))
            return cur
        return None
    # the child has not claimed the ticket yet (fix.owner still "watcher")
    try:
        import datetime as _dt

        at = _dt.datetime.fromisoformat(fix.get("delegated_at") or fix.get("started")).timestamp()
    except (TypeError, ValueError):
        at = now
    if now - at > cfg.osascript_timeout_s + cfg.terminal_start_grace_s:
        cur["status"] = "needs-review"
        fix["blocked"] = FILES_ACCESS
        fix["finished"] = now_iso()
        fix["summary"] = (
            files_access_summary(tid) + " | Terminal fallback failed: the Terminal child never "
            f"started within {cfg.osascript_timeout_s + cfg.terminal_start_grace_s:g}s"
        )
        cur["fix"] = fix
        save_ticket(cfg.tickets_dir, cur)
        echo(terminal_result_line(cfg, cur))
        return cur
    return None


def run_via_terminal(
    ticket: Dict[str, Any], cfg: Config, blocked: BaseException, deck_dir: Optional[Path],
    echo=print, wait: bool = True, commit=None,
) -> Dict[str, Any]:
    """TCC fallback for a deck ticket: hand it to Terminal.app. If osascript is
    refused (Automation permission) the ticket is parked as needs-review with
    the Full Disk Access instructions.

    wait=False (the --watch loop): return right away with the ticket
    "fixing", fix.delegated="terminal"; the watcher releases the global ticket
    lock and settles the delegation on later polls (settle_delegations), so
    other tickets are not blocked for the up-to-40-minute Terminal run. The
    child (`--once-from-terminal`) never takes the lock and claims the ticket
    by writing its own pid and fix.owner="terminal".

    wait=True (a manual `ankifix <id>`): poll ticket.json every
    terminal_poll_s with settle_delegation until it settles or
    terminal_fallback_timeout_min passes. A dead child pid ends the wait
    within one poll; a status a human set is kept, never overwritten."""
    ticket_id = ticket["id"]
    commit = commit or (lambda t: (save_ticket(cfg.tickets_dir, t), t)[1])
    ticket["kind"] = "deck"
    ticket["fix"]["delegated"] = "terminal"
    ticket["fix"]["delegated_at"] = now_iso()
    ticket["fix"]["summary"] = "no Files access under launchd; handed to Terminal.app"
    save_ticket(cfg.tickets_dir, ticket)
    tdir = ticket_dir(cfg.tickets_dir, ticket_id)
    try:
        with open(tdir / TERMINAL_LOG_NAME, "a") as fh:
            fh.write(f"=== {now_iso()} ankifix {ticket_id} --once-from-terminal "
                     f"(delegated by pid {os.getpid()}, attempt {ticket['fix'].get('attempts')})\n")
    except OSError:
        pass
    cmd = terminal_command(cfg, ticket_id)
    echo(f"ankifix: {ticket_id}: no Files access ({blocked}); running it in Terminal.app "
         f"(output in {tdir / TERMINAL_LOG_NAME})")
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

    if err is None and not wait:
        echo(f"ankifix: {ticket_id}: delegated to Terminal.app; not holding the ticket lock, "
             "the result is picked up on a later poll")
        return ticket

    if err is None:
        deadline = time.time() + cfg.terminal_fallback_timeout_min * 60
        while time.time() < deadline:
            time.sleep(cfg.terminal_poll_s)
            try:
                cur = load_ticket(cfg.tickets_dir, ticket_id)
            except (OSError, ValueError):
                continue
            settled = settle_delegation(cfg, cur, echo=echo)
            if settled is not None:
                return settled
        err = f"Terminal run did not finish within {cfg.terminal_fallback_timeout_min:g} min"
        try:
            cur = load_ticket(cfg.tickets_dir, ticket_id)
            fix = cur.get("fix") or {}
            if fix.get("owner") == "terminal" and _pid_alive(fix.get("pid")):
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
    ticket = commit(ticket)
    echo(f"ankifix: {ticket_id} {ticket['status']}: {ticket['fix']['summary']}")
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
    """Call with the ticket lock held. Requeues ONLY a run the watcher itself
    claimed that never finished: status "fixing", fix.finished None,
    fix.owner "watcher" (or no owner but a fix.pid: a run recorded before
    owners existed), and fix.pid dead (the watcher died mid-run: Mac slept,
    killed). It gives up at cfg.max_attempts.

    Never touched: a "fixing" status a human set with `ankibug set` (the fix
    on disk is a finished run's, or has no pid at all, or manual_set is newer
    than fix.started); a Terminal delegation (settle_delegations owns those).
    A dead manual `ankifix <id>` run (owner "cli") is marked failed, not
    requeued, since the watcher did not start it."""
    from .tickets import list_tickets

    touched = []
    for t in list_tickets(cfg.tickets_dir):
        if t.get("status") != "fixing":
            continue
        fix = t.get("fix") if isinstance(t.get("fix"), dict) else {}
        if fix.get("finished") is not None or fix.get("delegated") == "terminal":
            continue
        if _human_touched_since(t, fix.get("started")):
            continue
        owner = fix.get("owner")
        pid = fix.get("pid")
        if owner not in (None, "watcher", "cli") or (owner is None and not pid):
            continue
        if pid and _pid_alive(pid):
            continue
        attempts = int(fix.get("attempts") or 1)
        fix = {**new_fix(fix.get("started"), fix), **fix}
        fix["finished"] = now_iso()
        if owner == "cli":
            t["status"] = "failed"
            fix["summary"] = (f"manual `ankifix {t['id']}` run (pid {pid}) ended without a result; "
                              "not requeued (the watcher did not start it)")
        elif attempts >= cfg.max_attempts:
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


def load_pending(cfg: Config) -> set:
    try:
        return set(json.loads((Path(cfg.tickets_dir) / DELEGATIONS_NAME).read_text()))
    except (OSError, ValueError, TypeError):
        return set()


def save_pending(cfg: Config, pending: set) -> None:
    path = Path(cfg.tickets_dir) / DELEGATIONS_NAME
    try:
        if pending:
            path.write_text(json.dumps(sorted(pending)) + "\n")
        elif path.exists():
            path.unlink()
    except OSError:
        pass


def settle_delegations(cfg: Config, pending: Optional[set] = None, echo=print) -> List[Dict[str, Any]]:
    """Call with the ticket lock held (watch). Settles every in-flight
    Terminal delegation that has ended, and logs the result of any the
    watcher delegated (pending, persisted in DELEGATIONS_NAME) that the
    child already finished."""
    from .tickets import list_tickets

    persisted = pending is None
    if persisted:
        pending = load_pending(cfg)
    done = []
    for t in list_tickets(cfg.tickets_dir):
        if delegation_in_flight(t) or t.get("id") in pending:
            try:
                settled = settle_delegation(cfg, t, echo=echo)
            except Exception as e:  # noqa: BLE001
                echo(f"ankifix: {t.get('id')}: could not settle Terminal run: {type(e).__name__}: {e}")
                continue
            if settled is not None:
                pending.discard(t["id"])
                done.append(settled)
    if persisted:
        save_pending(cfg, pending)
    return done


def _planned_kind(t: Dict[str, Any], cfg: Config) -> str:
    if t.get("kind") in ("app", "deck"):
        return t["kind"]
    try:
        return classify(t, cfg)[0]
    except Exception:  # noqa: BLE001
        return "unknown"


def next_new(cfg: Config, skip: Optional[set] = None, avoid_deck: bool = False) -> Optional[str]:
    """Oldest status=new ticket. avoid_deck: skip deck tickets (a Terminal
    child is already editing the course build dir; two deck fixers must not
    edit it at once)."""
    from .tickets import list_tickets

    for t in list_tickets(cfg.tickets_dir):
        if t.get("status") == "new" and t["id"] not in (skip or ()):
            if avoid_deck and _planned_kind(t, cfg) == "deck":
                continue
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


def _any_delegation_in_flight(cfg: Config) -> bool:
    from .tickets import list_tickets

    return any(delegation_in_flight(t) for t in list_tickets(cfg.tickets_dir))


def watch(cfg: Config, interval: float, once: bool = False, echo=print) -> int:
    """Process status=new tickets one at a time, oldest first.

    One ticket can never take the watcher down: any exception out of
    run_ticket (or the apply step) is recorded on that ticket and the loop
    moves on. A ticket whose run raised is retried on the next poll, up to
    cfg.max_attempts. Tickets that crashed in this process are also skipped
    for the rest of its life, so a ticket.json that cannot even be rewritten
    does not spin. Every cfg.doctor_interval_s a light `ankifix doctor` runs
    and a check that flips to FAIL posts a notification.

    A TCC-blocked deck ticket is handed to Terminal.app without waiting
    (run_ticket wait_terminal=False): the lock is released at the end of the
    pass and settle_delegations picks the result up on a later poll, so
    other (app) tickets keep flowing. While a delegation is in flight, deck
    tickets wait (one deck fixer in the build dir at a time)."""
    from . import apply as apply_mod  # local: apply imports this module
    from . import doctor as doctor_mod

    echo(f"ankifix: watcher started pid {os.getpid()} at {now_iso()} (interval {interval:g}s)")
    handled = 0
    skip: set = set()
    last_doctor = 0.0
    doctor_state: Dict[str, bool] = {}
    while True:
        if cfg.auto_restart_app:
            try:
                from . import restart as restart_mod

                restart_mod.tick(cfg, echo=echo)
            except Exception as e:  # noqa: BLE001
                echo(f"ankifix: restart retry error: {type(e).__name__}: {e}")
        if cfg.doctor_interval_s and time.time() - last_doctor >= cfg.doctor_interval_s:
            last_doctor = time.time()
            try:
                doctor_state = doctor_mod.watch_check(cfg, doctor_state, echo=echo)
            except Exception as e:  # noqa: BLE001
                echo(f"ankifix: doctor error: {type(e).__name__}: {e}")
        try:
            with ticket_lock(cfg.tickets_dir):
                recover_stale(cfg, echo=echo)
                for settled in settle_delegations(cfg, echo=echo):
                    notify(cfg, f"ankifix: {settled['id']} {settled.get('status')} (Terminal run)")
                this_pass: set = set()
                while True:
                    tid = next_new(cfg, skip | this_pass, avoid_deck=_any_delegation_in_flight(cfg))
                    if not tid:
                        break
                    this_pass.add(tid)  # a retried ticket waits for the next poll
                    try:
                        result = run_ticket(tid, cfg, echo=echo, retry_on_error=True,
                                            wait_terminal=False)
                    except Exception as e:  # noqa: BLE001
                        skip.add(tid)
                        handled += 1
                        echo(f"ankifix: {tid} crashed: {type(e).__name__}: {e}")
                        record_crash(cfg, tid, e, echo=echo)
                        notify(cfg, f"ankifix: {tid} failed")
                        continue
                    handled += 1
                    if delegation_in_flight(result):
                        save_pending(cfg, load_pending(cfg) | {tid})
                        notify(cfg, f"ankifix: {tid} handed to Terminal.app")
                        continue
                    try:
                        on_disk = load_ticket(cfg.tickets_dir, tid).get("status")
                    except Exception:  # noqa: BLE001
                        on_disk = None
                    if on_disk is None or (on_disk == "new" and result.get("status") != "new"):
                        skip.add(tid)  # result could not be saved; do not spin on it
                    notify(cfg, f"ankifix: {tid} {result.get('status')}")
                    ignored = (result.get("fix") or {}).get("result_ignored")
                    if (
                        cfg.auto_apply
                        and not ignored
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
