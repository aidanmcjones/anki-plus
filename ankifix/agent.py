"""launchd LaunchAgent for `ankifix --watch --interval 20`.

`ankifix install-agent` writes and loads a LaunchAgent plist so the watcher
survives login/logout and restarts if it dies (RunAtLoad + KeepAlive).
`ankifix uninstall-agent` unloads it and removes the plist. Neither touches
a watcher started by hand (`nohup ankifix --watch ...`); install-agent
refuses to run while one of those is alive so the two never double-process
the same tickets dir.
"""
from __future__ import annotations

import os
import plistlib
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional

from .runner import AnkifixError

LABEL = "com.aidanjones.ankifix-watch"
DEFAULT_PLIST_PATH = Path.home() / "Library/LaunchAgents" / f"{LABEL}.plist"
DEFAULT_LOG_PATH = Path.home() / "AnkiTickets/ankifix-watch.log"
DEFAULT_ANKIFIX_BIN = Path.home() / ".venvs/ankibug/bin/ankifix"
# ankifix shells out to `claude`, `git`, `node`, `python3`; .local/bin is
# where the `claude` symlink lives (see `which claude`).
DEFAULT_PATH_ENV = ":".join([
    "~/.local/bin",
    "/opt/homebrew/bin",
    "/usr/local/bin",
    "/usr/bin",
    "/bin",
])


def build_plist(
    ankifix_bin: str = str(DEFAULT_ANKIFIX_BIN),
    interval: int = 20,
    log_path: str = str(DEFAULT_LOG_PATH),
    path_env: str = DEFAULT_PATH_ENV,
    label: str = LABEL,
) -> Dict[str, Any]:
    return {
        "Label": label,
        "ProgramArguments": [ankifix_bin, "--watch", "--interval", str(interval)],
        "RunAtLoad": True,
        "KeepAlive": True,
        "StandardOutPath": log_path,
        "StandardErrorPath": log_path,
        "EnvironmentVariables": {"PATH": path_env},
    }


def write_plist(dest: Path = DEFAULT_PLIST_PATH, **kw: Any) -> Path:
    """Render build_plist(**kw) to dest as a binary/XML plist. Returns dest.
    `dest` is a parameter (not hardcoded) so tests can point it at a tmp dir."""
    dest = Path(dest)
    data = build_plist(**kw)
    dest.parent.mkdir(parents=True, exist_ok=True)
    with open(dest, "wb") as f:
        plistlib.dump(data, f)
    return dest


def running_watch_pids() -> List[int]:
    """pids of any process whose command line matches `ankifix ... --watch`,
    launchd-managed or not (best-effort via pgrep -f)."""
    try:
        out = subprocess.run(["pgrep", "-f", "ankifix.*--watch"], capture_output=True, text=True)
    except OSError:
        return []
    return [int(p) for p in out.stdout.split() if p.strip().isdigit()]


def install_agent(
    plist_path: Path = DEFAULT_PLIST_PATH,
    ankifix_bin: str = str(DEFAULT_ANKIFIX_BIN),
    interval: int = 20,
    echo=print,
) -> Path:
    existing = running_watch_pids()
    if existing:
        raise AnkifixError(
            "an `ankifix --watch` process is already running (pid "
            f"{', '.join(str(p) for p in existing)}); stop it first "
            "(it is not launchd-managed, so installing the LaunchAgent now "
            "would double-run against the same tickets dir)"
        )
    dest = write_plist(plist_path, ankifix_bin=ankifix_bin, interval=interval)
    subprocess.run(
        ["launchctl", "bootstrap", f"gui/{os.getuid()}", str(dest)], check=True,
        capture_output=True, text=True,
    )
    echo(f"ankifix: wrote {dest} and loaded it with launchctl bootstrap")
    return dest


def uninstall_agent(plist_path: Path = DEFAULT_PLIST_PATH, echo=print) -> None:
    subprocess.run(
        ["launchctl", "bootout", f"gui/{os.getuid()}/{LABEL}"],
        check=False, capture_output=True, text=True,
    )
    plist_path = Path(plist_path)
    if plist_path.is_file():
        plist_path.unlink()
    echo(f"ankifix: unloaded {LABEL} and removed {plist_path}")
