"""ankiship: grouping rules and the commit -> standing-PR flow, against real
throwaway git repos and a fake `gh` that records its calls."""
import json
import os
import stat
import subprocess
from pathlib import Path

import pytest

from ankifix.apply import patch_paths
from ankiship import areas, ship

FAKE_GH = r"""#!/usr/bin/env python3
import json, os, sys
state_p = os.environ["FAKE_GH_STATE"]
st = json.load(open(state_p)) if os.path.exists(state_p) else {"calls": [], "prs": {}}
a = sys.argv[1:]
st["calls"].append(a)
out = ""
if a[:2] == ["repo", "view"]:
    out = "aidan/anki-plus"
elif a[:2] == ["pr", "list"]:
    head = a[a.index("--head") + 1]
    n = st["prs"].get(head)
    out = json.dumps([{"number": n, "url": f"https://gh/pr/{n}"}] if n else [])
elif a[:2] == ["pr", "create"]:
    head = a[a.index("--head") + 1]
    n = len(st["prs"]) + 1
    st["prs"][head] = n
    out = f"https://gh/pr/{n}"
json.dump(st, open(state_p, "w"))
print(out)
"""


def sh(cwd, *args):
    return subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True).stdout


@pytest.fixture
def env(tmp_path, monkeypatch):
    origin = tmp_path / "origin.git"
    sh(tmp_path, "git", "init", "-q", "--bare", "-b", "main", str(origin))
    repo = tmp_path / "addon"
    sh(tmp_path, "git", "clone", "-q", str(origin), str(repo))
    for k, v in (("user.name", "t"), ("user.email", "t@t"), ("commit.gpgsign", "false")):
        sh(repo, "git", "config", k, v)
    (repo / "browse_embed.py").write_text("a = 1\n")
    (repo / "sidebar_decks.py").write_text("b = 1\n")
    (repo / "README.md").write_text("# x\n")
    sh(repo, "git", "add", "-A")
    sh(repo, "git", "commit", "-qm", "init")
    sh(repo, "git", "push", "-q", "origin", "main")
    sh(repo, "git", "remote", "set-head", "origin", "main")
    sh(repo, "git", "checkout", "-qb", "video-backdrop")
    sh(repo, "git", "push", "-q", "-u", "origin", "video-backdrop")
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    gh = bin_ / "gh"
    gh.write_text(FAKE_GH)
    gh.chmod(gh.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bin_}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_GH_STATE", str(tmp_path / "gh.json"))
    return repo, origin, tmp_path / "gh.json"


def remote_log(origin, branch):
    return sh(origin, "git", "log", "--format=%s", branch).splitlines()


def test_area_rules():
    a = areas.area_of
    assert a("browse_card_drag.py", "addon") == "browse"
    assert a("tests/live/test_browse_sweep_select.py", "addon") == "browse"
    assert a("tests/test_sidebar_tag_drag.py", "addon") == "sidebar"
    assert a("web/video-backdrop.js", "addon") == "backdrop"
    assert a("web/reviewer.css", "addon") == "reviewer"
    assert a("tests/_realqt.py", "addon") == "test-harness"
    assert a("Makefile", "addon") == "build"
    assert a("CHANGELOG.md", "addon") == "docs"
    assert a("rslib/src/scheduler/fsrs/simulator.rs", "engine") == "scheduling"
    assert a("ts/routes/graphs/velocity.ts", "engine") == "stats"
    assert a("qt/aqt/main.py", "engine") == "desktop"


def test_kind_rules():
    k = areas.kind_of
    assert k([("M", "browse_embed.py"), ("A", "tests/test_browse.py")], "browse") == "fix"
    assert k([("A", "browse_new.py")], "browse") == "feat"
    assert k([("M", "tests/test_x.py")], "browse") == "test"
    assert k([("M", "docs/a.md")], "docs") == "docs"
    assert k([("M", "Makefile")], "build") == "build"
    assert k([("A", "browse_new.py")], "browse", "perf") == "perf"


def test_plan_groups_and_skips(env):
    repo, _, _ = env
    (repo / "browse_embed.py").write_text("a = 2\n")
    (repo / "tests").mkdir()
    (repo / "tests" / "test_browse_embed.py").write_text("ok\n")
    (repo / "sidebar_decks.py").write_text("b = 2\n")
    (repo / "tests" / "_probe_tmp.py").write_text("scratch\n")
    (repo / "-R0,0,1512,30").write_text("stray\n")
    groups = ship.ship(repo, dry_run=True, echo=lambda *_: None)
    got = {g.scope: sorted(g.paths) for g in groups}
    assert got == {
        "fix(browse)": ["browse_embed.py", "tests/test_browse_embed.py"],
        "fix(sidebar)": ["sidebar_decks.py"],
    }
    assert sh(repo, "git", "status", "--porcelain")  # dry run leaves the tree alone


def test_run_commits_and_opens_one_pr_per_group(env):
    repo, origin, ghs = env
    (repo / "browse_embed.py").write_text("a = 2\n")
    (repo / "sidebar_decks.py").write_text("b = 2\n")
    groups = ship.ship(repo, summary="first", echo=lambda *_: None)
    assert [g.state for g in groups] == ["pushed", "pushed"]
    assert sh(repo, "git", "status", "--porcelain") == ""
    assert remote_log(origin, "auto/fix/browse")[0] == "fix(browse): first"
    assert remote_log(origin, "auto/fix/sidebar")[0] == "fix(sidebar): first"
    assert remote_log(origin, "main") == ["init"]  # base never pushed
    assert remote_log(origin, "video-backdrop")[:2] == ["fix(sidebar): first", "fix(browse): first"]

    # second browse change stacks on the same branch and reuses the open PR
    (repo / "browse_embed.py").write_text("a = 3\n")
    ship.ship(repo, summary="second", echo=lambda *_: None)
    assert remote_log(origin, "auto/fix/browse")[:2] == ["fix(browse): second", "fix(browse): first"]
    calls = json.load(open(ghs))["calls"]
    creates = [c for c in calls if c[:2] == ["pr", "create"]]
    edits = [c for c in calls if c[:2] == ["pr", "edit"]]
    assert len(creates) == 2 and len(edits) == 1
    assert "type:fix" in creates[0] and "area:browse" in creates[0]


def test_paths_limit_and_unrelated_edits_untouched(env):
    repo, origin, _ = env
    (repo / "browse_embed.py").write_text("a = 2\n")
    (repo / "sidebar_decks.py").write_text("b = 9\n")
    ship.ship(repo, paths=["browse_embed.py"], summary="only browse", echo=lambda *_: None)
    assert sh(repo, "git", "status", "--porcelain").strip() == "M sidebar_decks.py"


def test_deleted_file_and_feature(env):
    repo, origin, _ = env
    (repo / "sidebar_decks.py").unlink()
    (repo / "browse_preview.py").write_text("new\n")
    groups = ship.ship(repo, echo=lambda *_: None)
    assert {g.scope for g in groups} == {"fix(sidebar)", "feat(browse)"}
    assert remote_log(origin, "auto/fix/sidebar")[0] == "fix(sidebar): remove sidebar_decks"
    assert remote_log(origin, "auto/feat/browse")[0] == "feat(browse): add browse_preview"


def test_conflict_keeps_local_commit(env, tmp_path):
    repo, origin, _ = env
    other = tmp_path / "other"
    sh(tmp_path, "git", "clone", "-q", str(origin), str(other))
    sh(other, "git", "config", "user.email", "o@o")
    sh(other, "git", "config", "user.name", "o")
    sh(other, "git", "checkout", "-qb", "auto/fix/browse")
    (other / "browse_embed.py").write_text("a = 'theirs'\n")
    sh(other, "git", "commit", "-qam", "diverge")
    sh(other, "git", "push", "-q", "origin", "auto/fix/browse")

    (repo / "browse_embed.py").write_text("a = 'ours'\n")
    (g,) = ship.ship(repo, echo=lambda *_: None)
    assert g.state == "conflict"
    assert remote_log(origin, "auto/fix/browse")[0] == "diverge"  # never forced
    assert sh(repo, "git", "log", "-1", "--format=%s").startswith("fix(browse)")


def test_main_is_never_pushed(env):
    repo, origin, _ = env
    sh(repo, "git", "checkout", "-q", "main")
    (repo / "browse_embed.py").write_text("a = 5\n")
    ship.ship(repo, echo=lambda *_: None)
    assert remote_log(origin, "main") == ["init"]
    assert remote_log(origin, "auto/fix/browse")[0].startswith("fix(browse)")


def test_settle_skips_fresh_files(env):
    repo, _, _ = env
    (repo / "browse_embed.py").write_text("a = 2\n")
    assert ship.changed_files(repo, settle_s=600) == []
    old = os.path.getmtime(repo / "browse_embed.py") - 3600
    os.utime(repo / "browse_embed.py", (old, old))
    assert ship.changed_files(repo, settle_s=600) == [("M", "browse_embed.py")]


def test_patch_paths():
    patch = "diff --git a/web/sidebar.js b/web/sidebar.js\n+x\ndiff --git a/old.py b/new.py\n"
    assert patch_paths(patch) == ["web/sidebar.js", "old.py", "new.py"]
