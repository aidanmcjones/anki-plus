"""Group working-tree changes by (type, area), commit each group, and keep one
standing PR per group.

Flow for one run in ``repo``:

1. read ``git status`` (optionally limited to ``paths`` and to files that
   have been untouched for ``settle_s`` seconds);
2. classify every file to an area and each area group to a conventional type;
3. make one local commit per group on the current branch (so the live
   checkout ends clean and keeps running the same code);
4. cherry-pick each commit onto ``<prefix><type>/<area>`` in a throwaway
   worktree based on that branch's remote tip (or on the base branch when it
   does not exist yet) and push it - fast-forward only, never forced;
5. make sure the branch has exactly one open PR into the base branch, and
   refresh its body with the branch's commit list.

It never pushes to the base branch, main or master, never force-pushes and
never merges. A group whose commit does not cherry-pick cleanly onto its PR
branch stays committed locally and is reported as ``conflict``.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from . import areas

PROTECTED = {"main", "master"}
# scratch files an agent leaves behind (and stray files named like a flag,
# e.g. a mistyped `screencapture -R...`); never auto-committed
SKIP_RE = re.compile(r"(^|/)(-[^/]*|_probe[^/]*|[^/]*_tmp\.[a-z]+|\.context/.*|\.claude/.*|\.pytest_cache/.*)$")
NOUN = {
    "feat": "features", "fix": "fixes", "perf": "performance", "refactor": "refactors",
    "test": "tests", "docs": "docs", "build": "build and tooling", "chore": "maintenance",
}
BODY_MARK = "<!-- ankiship:commits -->"


class ShipError(RuntimeError):
    pass


@dataclass
class Group:
    kind: str
    area: str
    files: List[Tuple[str, str]]  # (status, path)
    message: str = ""
    sha: str = ""
    branch: str = ""
    state: str = "planned"  # planned | committed | pushed | conflict | error
    pr: str = ""
    note: str = ""

    @property
    def paths(self) -> List[str]:
        return [p for _, p in self.files]

    @property
    def scope(self) -> str:
        return f"{self.kind}({self.area})"


@dataclass
class Settings:
    profile: str
    base: str
    prefix: str
    remote: str = "origin"
    labels: bool = True
    push_current: bool = True
    trailers: List[str] = field(default_factory=list)


def git(repo: Path, *args: str, check: bool = True, input_text: Optional[str] = None,
        env: Optional[Dict[str, str]] = None) -> str:
    proc = subprocess.run(["git", *args], cwd=str(repo), capture_output=True, text=True,
                          input=input_text, env=env)
    if check and proc.returncode != 0:
        raise ShipError(f"git {' '.join(args)}: {proc.stderr.strip() or proc.stdout.strip()}")
    return proc.stdout


def _git_ok(repo: Path, *args: str) -> bool:
    return subprocess.run(["git", *args], cwd=str(repo), capture_output=True).returncode == 0


def load_settings(repo: Path, **over) -> Settings:
    def cfg(key: str) -> str:
        return git(repo, "config", "--get", f"ship.{key}", check=False).strip()

    profile = over.get("profile") or cfg("profile") or areas.detect_profile(repo)
    remote = over.get("remote") or cfg("remote") or "origin"
    base = over.get("base") or cfg("base")
    if not base:
        head = git(repo, "symbolic-ref", "--short", f"refs/remotes/{remote}/HEAD", check=False).strip()
        base = head.split("/", 1)[1] if "/" in head else "main"
    prefix = over.get("prefix") or cfg("prefix") or "auto/"
    s = Settings(profile=profile, base=base, prefix=prefix, remote=remote)
    if cfg("labels") == "false":
        s.labels = False
    if cfg("pushCurrent") == "false":
        s.push_current = False
    s.trailers = list(over.get("trailers") or [])
    return s


# --- 1. what changed --------------------------------------------------------

def changed_files(repo: Path, paths: Optional[Sequence[str]] = None,
                  settle_s: float = 0, now: Optional[float] = None) -> List[Tuple[str, str]]:
    """[(status, path)] for every uncommitted change, status in A/M/D/R."""
    args = ["status", "--porcelain=v1", "-z", "--untracked-files=all"]
    if paths:
        args += ["--", *paths]
    raw = git(repo, *args).split("\0")
    out: List[Tuple[str, str]] = []
    now = now if now is not None else time.time()
    i = 0
    while i < len(raw):
        entry = raw[i]
        i += 1
        if len(entry) < 4:
            continue
        xy, path = entry[:2], entry[3:]
        if "R" in xy or "C" in xy:
            i += 1  # the rename source follows as its own field
        if xy == "??" or "A" in xy:
            status = "A"
        elif "D" in xy:
            status = "D"
        elif "R" in xy:
            status = "R"
        else:
            status = "M"
        if SKIP_RE.search(path):
            continue
        if settle_s and status != "D":
            try:
                if now - (repo / path).stat().st_mtime < settle_s:
                    continue  # still being edited
            except FileNotFoundError:
                pass
        out.append((status, path))
    return out


# --- 2. grouping ------------------------------------------------------------

def plan(files: List[Tuple[str, str]], profile: str, forced_kind: Optional[str] = None) -> List[Group]:
    by_area: Dict[str, List[Tuple[str, str]]] = {}
    for status, path in files:
        by_area.setdefault(areas.area_of(path, profile), []).append((status, path))
    groups: Dict[Tuple[str, str], Group] = {}
    for area, fs in sorted(by_area.items()):
        kind = areas.kind_of(fs, area, forced_kind)
        g = groups.setdefault((kind, area), Group(kind=kind, area=area, files=[]))
        g.files.extend(fs)
    return list(groups.values())


def auto_summary(g: Group) -> str:
    stems = []
    for _, p in g.files:
        s = Path(p).stem
        if s not in stems:
            stems.append(s)
    head = ", ".join(stems[:3])
    more = f" and {len(stems) - 3} more" if len(stems) > 3 else ""
    verb = {"A": "add", "D": "remove"}.get(g.files[0][0], "update") if len(g.files) == 1 else "update"
    return f"{verb} {head}{more}"


def build_message(g: Group, summary: Optional[str], body: str, trailers: Sequence[str]) -> str:
    subject = f"{g.scope}: {(summary or auto_summary(g)).strip()}"
    if len(subject) > 72:
        subject = subject[:71].rstrip() + "…"
    lines = [subject, ""]
    if body.strip():
        lines += [body.strip(), ""]
    lines += [f"- {p}" + (" (new)" if s == "A" else " (deleted)" if s == "D" else "") for s, p in g.files]
    if trailers:
        lines += [""] + list(trailers)
    return "\n".join(lines) + "\n"


# --- 3. local commits -------------------------------------------------------

def commit_group(repo: Path, g: Group) -> None:
    git(repo, "add", "-A", "--", *g.paths)
    git(repo, "commit", "-q", "-F", "-", "--only", "--", *g.paths, input_text=g.message)
    g.sha = git(repo, "rev-parse", "HEAD").strip()
    g.state = "committed"


# --- 4/5. PR branches -------------------------------------------------------

def gh(repo: Path, *args: str, check: bool = True) -> str:
    proc = subprocess.run(["gh", *args], cwd=str(repo), capture_output=True, text=True)
    if check and proc.returncode != 0:
        raise ShipError(f"gh {' '.join(args[:3])}: {proc.stderr.strip()}")
    return proc.stdout


def gh_repo(repo: Path, remote: str) -> str:
    url = git(repo, "remote", "get-url", remote).strip()
    # resolve renames (anki-design-private -> anki-plus) to the canonical name
    out = gh(repo, "repo", "view", url, "--json", "nameWithOwner", "-q", ".nameWithOwner", check=False).strip()
    if out:
        return out
    m = re.search(r"github\.com[:/](.+?)(\.git)?$", url)
    if not m:
        raise ShipError(f"remote {remote} is not a GitHub URL: {url}")
    return m.group(1)


def ensure_labels(repo: Path, slug: str, g: Group, seen: set) -> List[str]:
    labels = [f"type:{g.kind}", f"area:{g.area}"]
    for lab in labels:
        if lab not in seen:
            color = "d73a4a" if lab == "type:fix" else "0e8a16" if lab == "type:feat" else "c5def5"
            gh(repo, "label", "create", lab, "--repo", slug, "--color", color, "--force", check=False)
            seen.add(lab)
    return labels


def pr_body(g: Group, commits: str, base: str) -> str:
    return (
        f"Standing PR for **{g.scope}**: every {NOUN.get(g.kind, g.kind)} change in the "
        f"`{g.area}` area lands here as its own commit, grouped automatically by `ankiship`.\n\n"
        f"Merge into `{base}` when the batch is ready; the next change in this group opens a fresh PR.\n\n"
        f"{BODY_MARK}\n### Commits\n{commits}\n\n"
        "🤖 Generated with [Claude Code](https://claude.com/claude-code)\n"
    )


def publish_group(repo: Path, s: Settings, g: Group, slug: str, labels_seen: set) -> None:
    g.branch = f"{s.prefix}{g.kind}/{g.area}"
    if g.branch in PROTECTED or g.branch == s.base:
        raise ShipError(f"refusing to push to protected branch {g.branch}")
    git(repo, "fetch", "-q", s.remote, s.base)
    exists = _git_ok(repo, "fetch", "-q", s.remote, f"+refs/heads/{g.branch}:refs/ankiship/{g.branch}")
    start = f"refs/ankiship/{g.branch}" if exists else f"{s.remote}/{s.base}"
    if not exists:
        git(repo, "update-ref", "-d", f"refs/ankiship/{g.branch}", check=False)
    tmp = Path(tempfile.mkdtemp(prefix="ankiship-"))
    wt = tmp / "wt"
    git(repo, "worktree", "add", "-q", "--detach", str(wt), start)
    try:
        pick = subprocess.run(["git", "cherry-pick", "-x", "--allow-empty", g.sha], cwd=str(wt),
                              capture_output=True, text=True)
        if pick.returncode != 0:
            git(wt, "cherry-pick", "--abort", check=False)
            g.state, g.note = "conflict", f"does not apply onto {start}; kept as local commit {g.sha[:8]}"
            return
        git(wt, "push", "-q", s.remote, f"HEAD:refs/heads/{g.branch}")
        g.state = "pushed"
        commits = git(wt, "log", "--reverse", "--format=- %s (%h)", f"{s.remote}/{s.base}..HEAD").strip()
    finally:
        git(repo, "worktree", "remove", "--force", str(wt), check=False)
    open_prs = json.loads(gh(repo, "pr", "list", "--repo", slug, "--head", g.branch, "--state", "open",
                             "--json", "number,url") or "[]")
    body = pr_body(g, commits, s.base)
    labels = ensure_labels(repo, slug, g, labels_seen) if s.labels else []
    if open_prs:
        g.pr = open_prs[0]["url"]
        gh(repo, "pr", "edit", str(open_prs[0]["number"]), "--repo", slug, "--body", body, check=False)
    else:
        title = f"{g.scope}: {g.area.replace('-', ' ')} {NOUN.get(g.kind, g.kind)}"
        args = ["pr", "create", "--repo", slug, "--base", s.base, "--head", g.branch,
                "--title", title, "--body", body]
        for lab in labels:
            args += ["--label", lab]
        g.pr = gh(repo, *args).strip().splitlines()[-1]


def push_current(repo: Path, s: Settings) -> str:
    branch = git(repo, "branch", "--show-current").strip()
    if not branch or branch in PROTECTED or branch == s.base:
        return ""
    if not _git_ok(repo, "rev-parse", "--abbrev-ref", f"{branch}@{{upstream}}"):
        return ""
    return branch if _git_ok(repo, "push", "-q") else ""


# --- entry point ------------------------------------------------------------

def ship(repo: Path, *, paths: Optional[Sequence[str]] = None, kind: Optional[str] = None,
         summary: Optional[str] = None, body: str = "", settle_s: float = 0,
         dry_run: bool = False, publish: bool = True, echo=print, **over) -> List[Group]:
    repo = Path(repo).resolve()
    s = load_settings(repo, **over)
    if _git_ok(repo, "rev-parse", "-q", "--verify", "CHERRY_PICK_HEAD") or \
            _git_ok(repo, "rev-parse", "-q", "--verify", "MERGE_HEAD") or \
            (Path(git(repo, "rev-parse", "--git-dir").strip()) / "rebase-merge").exists():
        raise ShipError("a merge, rebase or cherry-pick is in progress; not committing")
    files = changed_files(repo, paths, settle_s)
    groups = plan(files, s.profile, kind)
    for g in groups:
        g.message = build_message(g, summary, body, s.trailers)
    if dry_run or not groups:
        for g in groups:
            echo(f"{g.scope}: {len(g.files)} file(s) -> {s.prefix}{g.kind}/{g.area}")
            for st, p in g.files:
                echo(f"    {st} {p}")
        if not groups:
            echo("ankiship: nothing to ship")
        return groups
    for g in groups:
        commit_group(repo, g)
        echo(f"ankiship: committed {g.sha[:8]} {g.message.splitlines()[0]}")
    if not publish:
        return groups
    slug = gh_repo(repo, s.remote)
    seen: set = set()
    for g in groups:
        try:
            publish_group(repo, s, g, slug, seen)
        except ShipError as e:
            g.state, g.note = "error", str(e)
        echo(f"ankiship: {g.scope} {g.state} {g.pr or g.note}".rstrip())
    if s.push_current:
        pushed = push_current(repo, s)
        if pushed:
            echo(f"ankiship: pushed {pushed}")
    return groups
