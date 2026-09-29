"""ankiship CLI.

    ankiship plan [REPO]                 show how the working tree would be grouped
    ankiship run  [REPO] [options]       commit each group and update its PR
    ankiship install-agent [REPO]        launchd agent: `run --settle` every N minutes
    ankiship uninstall-agent [REPO]
"""
from __future__ import annotations

import argparse
import hashlib
import os
import plistlib
import subprocess
import sys
from pathlib import Path

from . import areas, ship

HOME = Path.home()
LOG_DIR = HOME / "Library/Logs/ankiship"
KINDS = ["feat", "fix", "perf", "refactor", "test", "docs", "build", "chore"]


def _label(repo: Path) -> str:
    return f"com.aidanjones.ankiship.{repo.name}-{hashlib.sha1(str(repo).encode()).hexdigest()[:6]}"


def _plist_path(repo: Path) -> Path:
    return HOME / "Library/LaunchAgents" / f"{_label(repo)}.plist"


def install_agent(repo: Path, every_s: int, settle_s: int) -> Path:
    exe = Path(sys.argv[0]).resolve() if Path(sys.argv[0]).name == "ankiship" else None
    prog = [str(exe)] if exe else [sys.executable, "-m", "ankiship"]
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    plist = {
        "Label": _label(repo),
        "ProgramArguments": prog + ["run", str(repo), "--settle", str(settle_s)],
        "StartInterval": every_s,
        "RunAtLoad": False,
        "StandardOutPath": str(LOG_DIR / f"{repo.name}.log"),
        "StandardErrorPath": str(LOG_DIR / f"{repo.name}.log"),
        "EnvironmentVariables": {
            "PATH": ":".join([str(HOME / ".local/bin"), "/opt/homebrew/bin", "/usr/local/bin",
                              "/usr/bin", "/bin"]),
        },
    }
    path = _plist_path(repo)
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}", str(path)], capture_output=True)
    path.write_bytes(plistlib.dumps(plist))
    subprocess.run(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(path)], check=True)
    return path


def uninstall_agent(repo: Path) -> Path:
    path = _plist_path(repo)
    subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}", str(path)], capture_output=True)
    path.unlink(missing_ok=True)
    return path


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="ankiship", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("plan", "run"):
        p = sub.add_parser(name)
        p.add_argument("repo", nargs="?", default=".")
        p.add_argument("--path", action="append", dest="paths", help="limit to these paths (repeatable)")
        p.add_argument("--type", choices=KINDS, help="force the commit type for every group")
        p.add_argument("--profile", choices=sorted(areas.PROFILES), help="area rules (default: auto)")
        p.add_argument("--base", help="PR base branch (default: git config ship.base, else remote HEAD)")
        p.add_argument("--prefix", help="group branch prefix (default: ship.prefix, else auto/)")
        p.add_argument("--settle", type=float, default=0,
                       help="skip files modified in the last N seconds")
        p.add_argument("-m", "--message", help="commit summary (default: generated from file names)")
        p.add_argument("--body", default="", help="commit body text")
        p.add_argument("--trailer", action="append", default=[], help="commit trailer line (repeatable)")
        if name == "run":
            p.add_argument("--no-publish", action="store_true", help="commit locally only")
    for name in ("install-agent", "uninstall-agent"):
        p = sub.add_parser(name)
        p.add_argument("repo", nargs="?", default=".")
        if name == "install-agent":
            p.add_argument("--every", type=int, default=900, help="seconds between runs")
            p.add_argument("--settle", type=int, default=600, help="quiet period per file, seconds")
    a = ap.parse_args(argv)
    repo = Path(a.repo).resolve()

    if a.cmd == "install-agent":
        print(f"ankiship: installed {install_agent(repo, a.every, a.settle)}")
        return 0
    if a.cmd == "uninstall-agent":
        print(f"ankiship: removed {uninstall_agent(repo)}")
        return 0
    try:
        groups = ship.ship(
            repo, paths=a.paths, kind=a.type, summary=a.message, body=a.body, settle_s=a.settle,
            dry_run=a.cmd == "plan", publish=not getattr(a, "no_publish", False),
            profile=a.profile, base=a.base, prefix=a.prefix, trailers=a.trailer,
        )
    except ship.ShipError as e:
        print(f"ankiship: {e}", file=sys.stderr)
        return 2
    return 1 if any(g.state in ("conflict", "error") for g in groups) else 0


if __name__ == "__main__":
    sys.exit(main())
