"""`ankifix doctor`: can the watcher actually do its job on this machine?

Each check prints PASS or FAIL with a one-line detail; any FAIL makes the
command exit non-zero. `install-agent` runs it before loading the plist and
refuses when claude/git/node/python3 are not resolvable on the PATH the agent
will get. `--watch` runs the light subset every `doctor_interval_s` and posts
a notification when a check flips to FAIL.
"""
from __future__ import annotations

import os
import plistlib
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Callable, Dict, List, NamedTuple, Optional

from . import agent as agent_mod
from .config import Config
from .runner import is_files_access_error, notify, tcc_binary

TOOLS = ("claude", "git", "node", "python3")
BUILD_DIR_CHECK = "course build dir readable"


class Check(NamedTuple):
    name: str
    ok: bool
    detail: str


def agent_path(plist_path: Path = agent_mod.DEFAULT_PLIST_PATH) -> str:
    """The PATH the LaunchAgent runs with: the installed plist's if there is
    one, else what install-agent would write from this shell."""
    try:
        with open(plist_path, "rb") as fh:
            return plistlib.load(fh)["EnvironmentVariables"]["PATH"]
    except (OSError, KeyError, ValueError, plistlib.InvalidFileException):
        return agent_mod.resolve_path_env()


def check_tools(path: str) -> List[Check]:
    out = []
    for tool in TOOLS:
        found = shutil.which(tool, path=path)
        out.append(Check(f"{tool} on agent PATH", bool(found), found or f"not found on PATH={path}"))
    return out


def check_playwright(cfg: Config, path: str) -> List[Check]:
    node = shutil.which("node", path=path)
    out = []
    for key in ("node_path", "apply_node_path"):
        np = getattr(cfg, key)
        name = f"playwright importable with {key}"
        if not node:
            out.append(Check(name, False, "no node binary"))
            continue
        try:
            r = subprocess.run(
                [node, "-e", "require('playwright'); console.log('ok')"],
                env={**os.environ, "PATH": path, "NODE_PATH": np},
                capture_output=True, text=True, timeout=30,
            )
            ok = r.returncode == 0
            detail = np if ok else f"{np}: {(r.stderr or r.stdout).strip().splitlines()[-1:] or r.returncode}"
        except (OSError, subprocess.TimeoutExpired) as e:
            ok, detail = False, f"{np}: {type(e).__name__}: {e}"
        out.append(Check(name, ok, detail))
    return out


def check_repo(cfg: Config) -> Check:
    name = "addon repo usable for apply"
    repo = Path(cfg.app_repo)
    if not (repo / ".git").exists():
        return Check(name, False, f"{repo} is not a git checkout")
    st = subprocess.run(["git", "-C", str(repo), "status", "--porcelain"], capture_output=True, text=True)
    if st.returncode != 0:
        return Check(name, False, f"git status failed: {st.stderr.strip()}")
    base = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--verify", "-q", f"refs/heads/{cfg.app_base_branch}"],
        capture_output=True, text=True,
    )
    if base.returncode != 0:
        return Check(name, False, f"no local branch {cfg.app_base_branch!r} in {repo}")
    dirty = len([l for l in st.stdout.splitlines() if l.strip()])
    return Check(name, True, f"{repo} on git, base {cfg.app_base_branch}, {dirty} uncommitted paths "
                             "(apply only writes file contents, never commits)")


def check_tickets_dir(cfg: Config) -> Check:
    name = "tickets dir writable"
    try:
        Path(cfg.tickets_dir).mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=str(cfg.tickets_dir), prefix=".doctor.", delete=True):
            pass
        return Check(name, True, str(cfg.tickets_dir))
    except OSError as e:
        return Check(name, False, f"{cfg.tickets_dir}: {e}")


def check_build_dir(cfg: Config) -> Check:
    bdir = Path(cfg.deck_build_dir)
    try:
        names = sorted(os.listdir(bdir))
        target = next((bdir / n for n in ("build_out.json", "build_deck.py") if n in names), None)
        if target is None:
            target = next((bdir / n for n in names if (bdir / n).is_file()), None)
        if target is None:
            return Check(BUILD_DIR_CHECK, False, f"{bdir} has no files")
        with open(target, "rb") as fh:
            fh.read(1)
        return Check(BUILD_DIR_CHECK, True, f"opened {target}")
    except OSError as e:
        if is_files_access_error(e):
            fb = " Terminal fallback is on, so deck tickets still run." if cfg.terminal_fallback else ""
            return Check(BUILD_DIR_CHECK, False,
                         f"macOS blocked {getattr(e, 'filename', None) or bdir} (TCC, errno 1); grant "
                         f"Full Disk Access to {tcc_binary()}.{fb}")
        return Check(BUILD_DIR_CHECK, False, f"{bdir}: {e}")


def check_agent() -> Check:
    name = "launchd agent loaded with a PID"
    try:
        r = subprocess.run(["launchctl", "list", agent_mod.LABEL], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired) as e:
        return Check(name, False, f"launchctl: {e}")
    if r.returncode != 0:
        return Check(name, False, f"{agent_mod.LABEL} is not loaded")
    m = re.search(r'"PID"\s*=\s*(\d+);', r.stdout)
    if not m:
        return Check(name, False, f"{agent_mod.LABEL} is loaded but not running")
    return Check(name, True, f"{agent_mod.LABEL} pid {m.group(1)}")


def check_notifications(cfg: Config, path: str) -> Check:
    name = "notifications (osascript)"
    osa = shutil.which(cfg.osascript_bin, path=path) or shutil.which(cfg.osascript_bin)
    if not osa:
        return Check(name, False, "osascript not found")
    try:
        r = subprocess.run(
            [osa, "-e", 'display notification "ankifix doctor check" with title "ankifix"'],
            capture_output=True, text=True, timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        return Check(name, False, f"{type(e).__name__}: {e}")
    if r.returncode != 0:
        return Check(name, False, (r.stderr or r.stdout).strip() or f"exit {r.returncode}")
    return Check(name, True, f"{osa} posted a test notification")


def _safe(name: str, fn: Callable, *args) -> List[Check]:
    """Run one check; a check that raises is a FAIL, never a doctor crash."""
    try:
        r = fn(*args)
        return list(r) if isinstance(r, list) else [r]
    except Exception as e:  # noqa: BLE001
        return [Check(name, False, f"check raised {type(e).__name__}: {e}")]


def run_checks(cfg: Config, path: Optional[str] = None, light: bool = False,
               agent: bool = True) -> List[Check]:
    path = path or agent_path()
    checks = _safe("tools on agent PATH", check_tools, path)
    checks += _safe("playwright importable", check_playwright, cfg, path)
    checks += _safe("addon repo usable for apply", check_repo, cfg)
    checks += _safe("tickets dir writable", check_tickets_dir, cfg)
    checks += _safe(BUILD_DIR_CHECK, check_build_dir, cfg)
    if not light:
        if agent:
            checks += _safe("launchd agent loaded with a PID", check_agent)
        checks += _safe("notifications (osascript)", check_notifications, cfg, path)
    return checks


def print_checks(checks: List[Check], echo: Callable = print) -> bool:
    for c in checks:
        echo(f"{'PASS' if c.ok else 'FAIL'}  {c.name}: {c.detail}")
    ok = all(c.ok for c in checks)
    echo(f"doctor: {sum(c.ok for c in checks)}/{len(checks)} checks pass")
    return ok


def doctor(cfg: Config, echo: Callable = print, path: Optional[str] = None, agent: bool = True) -> int:
    return 0 if print_checks(run_checks(cfg, path=path, agent=agent), echo) else 1


def tools_ok(checks: List[Check]) -> bool:
    return all(c.ok for c in checks if c.name.endswith(" on agent PATH"))


def watch_check(cfg: Config, prev: Dict[str, bool], echo: Callable = print) -> Dict[str, bool]:
    """Light doctor for --watch. Checks the PATH this process hands its
    children. Notifies for each check that is FAIL now and was not before
    (a TCC-blocked build dir is expected, and handled, when the Terminal
    fallback is on, so it only logs)."""
    checks = run_checks(cfg, path=cfg.subprocess_path(), light=True)
    state = {c.name: c.ok for c in checks}
    bad = [c for c in checks if not c.ok]
    echo("ankifix: doctor " + ("all PASS" if not bad else "FAIL: " + "; ".join(f"{c.name}: {c.detail}" for c in bad)))
    for c in bad:
        if prev.get(c.name, True):
            if c.name == BUILD_DIR_CHECK and cfg.terminal_fallback:
                continue
            notify(cfg, f"ankifix doctor FAIL: {c.name}")
    return state
