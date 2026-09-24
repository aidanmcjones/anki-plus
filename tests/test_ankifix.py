"""ankifix tests. A fake `claude` on PATH exercises the whole loop without tokens."""
from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from ankifix import agent as agent_mod  # noqa: E402
from ankifix import apply as apply_mod  # noqa: E402
from ankifix import cli, prompts, runner  # noqa: E402
from ankifix.classify import classify  # noqa: E402
from ankifix.config import Config  # noqa: E402
from ankifix.tickets import load_ticket, render_index, save_ticket, write_index  # noqa: E402

CO_AUTHOR = "Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"

FAKE_CLAUDE = r"""#!/bin/bash
# Fake `claude -p`: records argv + prompt, pretends to fix, emits stream-json.
printf '%s\n' "$@" > "$FAKE_DIR/argv.txt"
cat > "$FAKE_DIR/prompt.txt"
pwd > "$FAKE_DIR/cwd.txt"
echo "NODE_PATH=$NODE_PATH" > "$FAKE_DIR/env.txt"
mode="${FAKE_MODE:-fix}"
[ -f build_deck.py ] && [ "$mode" = fix ] && mode=deck
echo '{"type":"system","subtype":"init","session_id":"fake-session-1","tools":["Bash"]}'
case "$mode" in
  sleep) sleep 30 ;;
  fix)
    echo 'fixed' > fixed.txt
    echo 'print("ok")' > tests/test_fake_bug.py
    git add -A >/dev/null
    git commit -q -m "Fix the fake bug

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
    git push -q -u origin "$(git rev-parse --abbrev-ref HEAD)" 2>/dev/null
    echo '{"type":"assistant","message":{"content":[{"type":"tool_use","name":"Bash","input":{"command":"node tests/reviewer_click.cjs && python3 tests/test_fake_bug.py"}}]}}'
    echo '{"type":"result","subtype":"success","is_error":false,"num_turns":7,"total_cost_usd":0.42,"session_id":"fake-session-1","result":"Done.\n\n```ankifix-result\n{\"status\": \"fixed\", \"tests_green\": true, \"tests\": [\"node tests/reviewer_click.cjs\"], \"reproduced\": true, \"pushed\": true, \"live_tests\": [\"tests/live/test_fake_bug.py\"], \"summary\": \"Root cause X; fixed Y; added test Z.\"}\n```\n"}'
    ;;
  deck)
    echo "- set: {id: C1-Q04, Answer: fixed}" >> cards/overrides.yaml
    printf 'warn something\nRESULT: PASS (0 failures, 1 warnings)\n' > preflight_report.txt
    echo '{"type":"assistant","message":{"content":[{"type":"tool_use","name":"Bash","input":{"command":"/x/fb-anki/bin/python build_deck.py"}}]}}'
    echo '{"type":"result","subtype":"success","is_error":false,"num_turns":5,"total_cost_usd":0.1,"result":"```ankifix-result\n{\"status\": \"fixed\", \"tests_green\": true, \"tests\": [\"build_deck.py\"], \"card_ids\": [\"C1-Q04\"], \"apply_command\": \"cd ~/dev/anki && out/pyenv/bin/python apply_highyield.py --repatch=C1-Q04\", \"summary\": \"Answer typo fixed via overrides.\"}\n```"}'
    ;;
  nolive)
    echo 'fixed' > fixed.txt
    git add -A >/dev/null
    git commit -q -m "Fix with only a stubbed-Qt test

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
    echo '{"type":"result","subtype":"success","is_error":false,"num_turns":4,"result":"```ankifix-result\n{\"status\": \"fixed\", \"tests_green\": true, \"tests\": [\"python3 tests/test_fake_bug.py\"], \"reproduced\": true, \"pushed\": true, \"summary\": \"fake-Qt test only.\"}\n```"}'
    ;;
  nofix)
    echo '{"type":"result","subtype":"error_max_turns","is_error":true,"num_turns":60,"result":"ran out"}'
    ;;
  needs_review)
    echo 'fixed' > fixed.txt
    echo 'print("ok")' > tests/test_fake_bug.py
    git add -A >/dev/null
    git commit -q -m "Fix the fake bug, harness not fully green

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
    git push -q -u origin "$(git rev-parse --abbrev-ref HEAD)" 2>/dev/null
    echo '{"type":"assistant","message":{"content":[{"type":"tool_use","name":"Bash","input":{"command":"node tests/reviewer_click.cjs"}}]}}'
    echo '{"type":"result","subtype":"success","is_error":false,"num_turns":9,"total_cost_usd":0.55,"session_id":"fake-session-1","result":"Fixed but one pre-existing unrelated test still fails.\n\n```ankifix-result\n{\"status\": \"fixed\", \"tests_green\": false, \"tests\": [\"node tests/reviewer_click.cjs\"], \"reproduced\": true, \"pushed\": true, \"summary\": \"Root cause X; fixed Y; one unrelated pre-existing test still fails.\"}\n```\n"}'
    ;;
esac
exit 0
"""


def sh(cwd, *args):
    return subprocess.run(args, cwd=str(cwd), check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def env(tmp_path, monkeypatch):
    # app repo with a video-backdrop branch and a bare origin
    origin = tmp_path / "origin.git"
    sh(tmp_path, "git", "init", "-q", "--bare", str(origin))
    repo = tmp_path / "anki-design"
    repo.mkdir()
    sh(repo, "git", "init", "-q", "-b", "video-backdrop")
    sh(repo, "git", "config", "user.name", "Test")
    sh(repo, "git", "config", "user.email", "test@example.com")
    (repo / "CLAUDE.md").write_text("# CLAUDE.md\n\n## Silent screenshots\n\n```bash\n# not a heading\n```\n\n### Workflow\n")
    (repo / "tests").mkdir()
    (repo / "tests" / "reviewer_click.cjs").write_text("// fake\n")
    sh(repo, "git", "add", "-A")
    sh(repo, "git", "commit", "-q", "-m", "init")
    sh(repo, "git", "remote", "add", "origin", str(origin))

    # deck build dir
    course = tmp_path / "Course"
    build = course / "Anki" / "build"
    (build / "cards").mkdir(parents=True)
    (course / "COURSE_DECK.md").write_text("## 7. Card conventions\n## 8. High-yield pass\n")
    (build / "build_deck.py").write_text("print('build')\n")
    (build / "cards" / "overrides.yaml").write_text("# overrides\n")
    (build / "build_out.json").write_text(json.dumps({
        "C1-Q04": {"model": "basic", "fields": ["C1-Q04", "Would you expect that a drug dissolves in aqueous solution better when charged?", "", "Charged."]},
        "C2-Q01": {"model": "basic", "fields": ["C2-Q01", "Something completely unrelated to the ticket at all here.", "", "No."]},
    }))

    # fake claude on PATH
    fake_dir = tmp_path / "fake"
    fake_dir.mkdir()
    bindir = tmp_path / "bin"
    bindir.mkdir()
    fake = bindir / "claude"
    fake.write_text(FAKE_CLAUDE)
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_DIR", str(fake_dir))
    monkeypatch.setenv("ANKIFIX_CONFIG", str(tmp_path / "no-config.json"))

    cfg = Config()
    cfg.tickets_dir = tmp_path / "AnkiTickets"
    cfg.app_repo = repo
    cfg.deck_build_dir = build
    cfg.deck_python = "/x/fb-anki/bin/python"
    cfg.node_path = "/fake/node_modules"
    # Off by default in tests: notify() can pop a real macOS notification, and
    # auto_apply drives real `git format-patch`/`git apply` against env["repo"].
    # Both get their own dedicated tests below with these turned on deliberately.
    cfg.notify = False
    cfg.auto_apply = False
    cfg.doctor_interval_s = 0  # the hourly doctor runs real node/git checks
    cfg.auto_restart_app = False  # never touch the real Anki+ from tests
    return {"cfg": cfg, "repo": repo, "build": build, "fake": fake_dir, "tmp": tmp_path, "course": course}


def make_ticket(cfg, tid="20260922-214503-editor-image-won-t-grow", note="Editor: image won't grow after hotkey", **over):
    t = {
        "schema": 1, "id": tid, "created": "2026-09-22T21:45:03-05:00", "source": "hotkey",
        "note": note, "status": "new", "kind": "unknown",
        "app": {"anki_version": "26.08.1", "addon_repo": str(cfg.app_repo), "branch": "video-backdrop",
                "commit": "de48ca2", "dirty": False},
        "reviewer": {"state": "question", "card_id": 1, "note_id": 2, "notetype": None, "deck": None,
                     "template_ord": None},
        "deck_build": None,
        "capture": {"pages": [{"title": "main webview", "url": "http://127.0.0.1:40000/"}],
                    "qa_html_path": "qa.html",
                    "visible_text": "Would you expect that a drug dissolves in aqueous solution better when charged? Why?",
                    "console_errors": ["TypeError: x is undefined"], "screenshot_path": "screenshot.png",
                    "captured_at": "2026-09-22T21:45:04-05:00"},
        "fix": None,
    }
    t.update(over)
    save_ticket(cfg.tickets_dir, t)
    d = cfg.tickets_dir / tid
    (d / "qa.html").write_text("<div id='qa'><p>QA_MARKER</p></div>")
    (d / "screenshot.png").write_bytes(b"\x89PNG\r\n")
    return t


# ------------------------------------------------------------ classification
@pytest.mark.parametrize("note,notetype,expected", [
    ("the answer on this card is wrong", None, "deck"),
    ("image on card is cut off", None, "deck"),
    ("cloze blank shows the answer", None, "deck"),
    ("editor crashes when I paste", None, "app"),
    ("hotkey does nothing in the reviewer UI", None, "app"),
    ("deadline for the deck is not saved", None, "app"),
    ("deck browser freezes", None, "app"),
    ("screen goes blank after hotkey", None, "app"),
    ("something weird", "CourseB-Basic", "deck"),
    # captured on a course card, but "editor crashes" has an app signal
    # (editor) and no card-content signal: notetype prefix is evidence, not
    # a verdict - this is an app bug, not a card fix
    ("editor crashes", "Micro-Cloze", "app"),
    ("something weird", "Basic", "app"),
])
def test_classify(note, notetype, expected):
    t = {"note": note, "reviewer": {"notetype": notetype}, "capture": None}
    kind, reason = classify(t, Config())
    assert kind == expected, reason


def test_classify_override_and_config_prefixes():
    # neutral note (no app or deck signal words) so this test isolates the
    # prefix mechanism itself, not the app-signal/content-signal weighing
    # (see test_classify_course_notetype_is_evidence_not_verdict for that)
    t = {"note": "something about this", "reviewer": {"notetype": "Genetics-Basic"}}
    cfg = Config()
    assert classify(t, cfg)[0] == "app"
    cfg.course_notetype_prefixes.append("Genetics-")
    assert classify(t, cfg)[0] == "deck"
    assert classify(t, cfg, "app")[0] == "app"
    with pytest.raises(ValueError):
        classify(t, cfg, "bogus")


def test_console_errors_bias_app():
    t = {"note": "the card looks off", "reviewer": {}, "capture": {"console_errors": ["TypeError"]}}
    assert classify(t, Config())[0] == "app"  # 1 deck hit vs console error


# ------------------------------------------------------------------ prompts
def test_render_app_prompt(env):
    cfg = env["cfg"]
    t = make_ticket(cfg)
    p = prompts.render_app(t, cfg)
    assert "${" not in p
    assert "Editor: image won't grow after hotkey" in p
    assert '"id": "20260922-214503-editor-image-won-t-grow"' in p
    assert "QA_MARKER" in p
    assert str(cfg.tickets_dir / t["id"] / "screenshot.png") in p
    assert "TypeError: x is undefined" in p
    assert "## Silent screenshots" in p and "### Workflow" in p and "not a heading" not in p
    assert "node tests/editor_tools.cjs" in p and "node tests/reviewer_click.cjs" in p
    assert "tests/test_" in p
    assert CO_AUTHOR in p
    assert "fix/20260922-214503-editor-image-won-t-grow" in p
    assert "git push -u origin fix/20260922-214503-editor-image-won-t-grow" in p
    assert "ankifix-result" in p
    assert "restart Anki+" in p


def test_render_app_prompt_includes_known_failing_tests(env):
    cfg = env["cfg"]
    cfg.known_failing_tests = ["tests/editor_tools.cjs", "tests/test_editor_crop.py"]
    t = make_ticket(cfg)
    p = prompts.render_app(t, cfg)
    assert "${known_failing_tests}" not in p
    assert "known, pre-existing failures" in p
    assert "`tests/editor_tools.cjs`" in p
    assert "`tests/test_editor_crop.py`" in p
    cfg.known_failing_tests = ["tests/only_this_one.cjs"]
    p2 = prompts.render_app(t, cfg)
    assert "`tests/only_this_one.cjs`" in p2
    assert "tests/test_editor_crop.py" not in p2


def test_render_deck_prompt_matches_build_out(env):
    cfg = env["cfg"]
    t = make_ticket(cfg, note="the answer on this card is wrong",
                    deck_build={"course_dir": str(env["course"]), "build_marker": None})
    assert prompts.deck_build_dir(t, cfg) == env["build"]
    p = prompts.render_deck(t, cfg)
    assert "${" not in p
    assert "### C1-Q04" in p and "C2-Q01" not in p
    assert str(env["course"] / "COURSE_DECK.md") in p
    assert "/x/fb-anki/bin/python build_deck.py" in p
    assert "RESULT: PASS" in p
    assert "NEVER run `apply_*.py`" in p


def test_index_escapes_and_truncates(env):
    cfg = env["cfg"]
    make_ticket(cfg, tid="a", note="pipe | in note " + "x" * 100)
    idx = (cfg.tickets_dir / "index.md").read_text()
    assert "| id | created | source | kind | status | note |" in idx
    row = [l for l in idx.splitlines() if l.startswith("| a |")][0]
    assert "pipe \\| in note" in row and "x" * 100 not in row
    assert render_index([]) .count("\n") == 4


# ---------------------------------------------------------------- run loop
def test_dry_run_prints_prompt_and_does_nothing(env, capsys):
    cfg = env["cfg"]
    t = make_ticket(cfg)
    os.environ["ANKIFIX_TICKETS_DIR"] = str(cfg.tickets_dir)
    os.environ["ANKIFIX_APP_REPO"] = str(cfg.app_repo)
    try:
        rc = cli.main(["--dry-run", t["id"]])
    finally:
        del os.environ["ANKIFIX_TICKETS_DIR"], os.environ["ANKIFIX_APP_REPO"]
    out = capsys.readouterr()
    assert rc == 0
    assert "QA_MARKER" in out.out
    assert "--max-turns 60" in out.err and "--permission-mode dontAsk" in out.err
    assert not (env["fake"] / "argv.txt").exists()
    assert not cfg.worktree_path(t["id"]).exists()
    assert load_ticket(cfg.tickets_dir, t["id"])["status"] == "new"


def test_app_run_end_to_end(env):
    cfg = env["cfg"]
    t = make_ticket(cfg)
    out = runner.run_ticket(t["id"], cfg, echo=lambda *a: None)
    wt = cfg.worktree_path(t["id"])
    assert out["status"] == "fixed", out["fix"]["summary"]
    assert out["kind"] == "app"
    fix = out["fix"]
    assert fix["branch"] == f"fix/{t['id']}"
    assert len(fix["commits"]) == 1
    assert "node tests/reviewer_click.cjs" in fix["tests"]
    assert "python3 tests/test_fake_bug.py" in fix["tests"]
    # every attempt keeps its own transcript; fix.log links to the latest
    assert fix["log_path"] == "fix.1.log" and fix["started"] and fix["finished"]
    assert (cfg.tickets_dir / t["id"] / "fix.log").resolve().name == "fix.1.log"
    assert "Root cause X" in fix["summary"] and "pushed to origin" in fix["summary"]
    assert "$0.42" in fix["summary"]
    # claude ran inside the worktree, on the fix branch, main checkout untouched
    assert Path((env["fake"] / "cwd.txt").read_text().strip()).resolve() == wt.resolve()
    assert sh(env["repo"], "git", "rev-parse", "--abbrev-ref", "HEAD") == "video-backdrop"
    assert not (env["repo"] / "fixed.txt").exists()
    assert CO_AUTHOR in sh(wt, "git", "log", "-1", "--format=%B")
    # flags
    argv = (env["fake"] / "argv.txt").read_text().splitlines()
    assert argv[0] == "-p"
    assert argv[argv.index("--max-turns") + 1] == "60"
    assert argv[argv.index("--output-format") + 1] == "stream-json" and "--verbose" in argv
    assert argv[argv.index("--permission-mode") + 1] == "dontAsk"
    allowed = argv[argv.index("--allowedTools") + 1]
    assert f"Bash(git push origin fix/{t['id']}*)" in allowed
    assert "Bash(git checkout video-backdrop*)" in argv[argv.index("--disallowedTools") + 1]
    assert argv[argv.index("--add-dir") + 1] == str(cfg.tickets_dir / t["id"])
    assert "NODE_PATH=/fake/node_modules" in (env["fake"] / "env.txt").read_text()
    assert "QA_MARKER" in (env["fake"] / "prompt.txt").read_text()
    # transcript + index
    log = (cfg.tickets_dir / t["id"] / "fix.log").read_text().splitlines()
    assert json.loads(log[0])["type"] == "ankifix" and json.loads(log[-1])["event"] == "end"
    idx = (cfg.tickets_dir / "index.md").read_text()
    assert f"| {t['id']} |" in idx and "| app | fixed |" in idx


def test_app_run_fixed_but_not_green_is_needs_review(env, monkeypatch):
    monkeypatch.setenv("FAKE_MODE", "needs_review")
    cfg = env["cfg"]
    t = make_ticket(cfg)
    out = runner.run_ticket(t["id"], cfg, echo=lambda *a: None)
    assert out["status"] == "needs-review", out["fix"]["summary"]
    assert len(out["fix"]["commits"]) == 1
    assert "needs human review" in out["fix"]["summary"]
    idx = (cfg.tickets_dir / "index.md").read_text()
    assert "| needs-review |" in idx
    # needs-review is a completed-ish state: re-running without --force is refused,
    # same as fixed/wontfix/fixing.
    with pytest.raises(runner.AnkifixError):
        runner.run_ticket(t["id"], cfg, echo=lambda *a: None)


def test_app_run_without_commit_fails(env, monkeypatch):
    monkeypatch.setenv("FAKE_MODE", "nofix")
    cfg = env["cfg"]
    t = make_ticket(cfg)
    out = runner.run_ticket(t["id"], cfg, echo=lambda *a: None)
    assert out["status"] == "failed"
    assert out["fix"]["commits"] == []
    assert "error_max_turns" in out["fix"]["summary"]


def test_wall_clock_limit_kills_claude(env, monkeypatch):
    monkeypatch.setenv("FAKE_MODE", "sleep")
    cfg = env["cfg"]
    cfg.wall_clock_minutes = 1.0 / 60  # 1 second
    t = make_ticket(cfg)
    out = runner.run_ticket(t["id"], cfg, echo=lambda *a: None)
    assert out["status"] == "failed"
    assert "wall clock" in out["fix"]["summary"]


def test_deck_run_end_to_end(env):
    cfg = env["cfg"]
    t = make_ticket(cfg, note="the answer on this card is wrong",
                    reviewer={"state": "answer", "card_id": 1, "note_id": 2,
                              "notetype": "CourseB-Basic", "deck": "x", "template_ord": 0})
    out = runner.run_ticket(t["id"], cfg, echo=lambda *a: None)
    assert out["kind"] == "deck"
    assert out["status"] == "fixed", out["fix"]["summary"]
    s = out["fix"]["summary"]
    assert "APPLY WITH ANKI+ CLOSED: cd ~/dev/anki" in s and "--repatch=C1-Q04" in s
    assert "cards/overrides.yaml" in s
    assert out["fix"]["branch"] is None and out["fix"]["commits"] == []
    assert any("build_deck.py" in x for x in out["fix"]["tests"])
    assert (cfg.tickets_dir / t["id"] / "deck_before" / "cards" / "overrides.yaml").read_text() == "# overrides\n"
    assert Path((env["fake"] / "cwd.txt").read_text().strip()).resolve() == env["build"].resolve()
    argv = (env["fake"] / "argv.txt").read_text().splitlines()
    allowed = argv[argv.index("--allowedTools") + 1]
    assert "Bash(/x/fb-anki/bin/python build_deck.py*)" in allowed
    assert "apply" not in allowed


def test_status_guard_and_force(env):
    cfg = env["cfg"]
    t = make_ticket(cfg, status="wontfix")
    with pytest.raises(runner.AnkifixError):
        runner.run_ticket(t["id"], cfg, echo=lambda *a: None)
    assert runner.run_ticket(t["id"], cfg, force=True, echo=lambda *a: None)["status"] == "fixed"


def test_watch_drains_new_tickets_in_order(env):
    cfg = env["cfg"]
    make_ticket(cfg, tid="t2", created="2026-09-22T22:00:00-05:00")
    make_ticket(cfg, tid="t1", created="2026-09-22T21:00:00-05:00")
    make_ticket(cfg, tid="t0", status="fixed")
    seen = []
    n = runner.watch(cfg, interval=0, once=True, echo=seen.append)
    assert n == 2
    assert [s for s in seen if "kind=" in s][0].startswith("ankifix: t1 ")
    assert load_ticket(cfg.tickets_dir, "t1")["status"] == "fixed"
    assert load_ticket(cfg.tickets_dir, "t2")["status"] == "fixed"
    assert load_ticket(cfg.tickets_dir, "t0")["fix"] is None


def test_lock_is_exclusive(env):
    cfg = env["cfg"]
    with runner.ticket_lock(cfg.tickets_dir):
        code = (
            "import sys; sys.path.insert(0, %r)\n"
            "from ankifix import runner\n"
            "try:\n"
            "    with runner.ticket_lock(%r): print('got')\n"
            "except runner.LockBusy: print('busy')\n"
        ) % (str(ROOT), str(cfg.tickets_dir))
        out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True).stdout
        assert out.strip() == "busy"
    with runner.ticket_lock(cfg.tickets_dir):
        pass


def test_parse_transcript_takes_last_block(tmp_path):
    log = tmp_path / "fix.log"
    ev = {"type": "result", "subtype": "success",
          "result": "```ankifix-result\n{\"status\": \"failed\"}\n```\nlater\n```ankifix-result\n{\"status\": \"fixed\", \"tests_green\": true}\n```"}
    log.write_text("not json\n" + json.dumps(ev) + "\n")
    p = runner.parse_transcript(log)
    assert p["block"] == {"status": "fixed", "tests_green": True}


# -------------------------------------------------------------------- notify
def test_notify_noop_when_disabled(monkeypatch):
    called = []
    monkeypatch.setattr(runner.subprocess, "run", lambda *a, **k: called.append(a))
    cfg = Config()
    cfg.notify = False
    runner.notify(cfg, "hello")
    assert called == []


def test_notify_prefers_terminal_notifier(monkeypatch):
    calls = []
    monkeypatch.setattr(runner.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(runner.subprocess, "run", lambda args, **k: calls.append(args))
    cfg = Config()
    cfg.notify = True
    runner.notify(cfg, "ankifix: t1 fixed")
    assert len(calls) == 1
    assert calls[0][0] == "terminal-notifier"
    assert "ankifix: t1 fixed" in calls[0]


def test_notify_falls_back_to_osascript(monkeypatch):
    calls = []
    monkeypatch.setattr(runner.shutil, "which", lambda name: "/usr/bin/osascript" if name == "osascript" else None)
    monkeypatch.setattr(runner.subprocess, "run", lambda args, **k: calls.append(args))
    cfg = Config()
    cfg.notify = True
    runner.notify(cfg, "ankifix: t1 needs-review")
    assert len(calls) == 1
    assert calls[0][0] == "osascript"
    assert "ankifix: t1 needs-review" in calls[0][2]


def test_notify_noop_when_nothing_available(monkeypatch):
    calls = []
    monkeypatch.setattr(runner.shutil, "which", lambda name: None)
    monkeypatch.setattr(runner.subprocess, "run", lambda *a, **k: calls.append(a))
    cfg = Config()
    cfg.notify = True
    runner.notify(cfg, "hello")
    assert calls == []


def test_watch_notifies_and_auto_applies(env, monkeypatch):
    cfg = env["cfg"]
    cfg.notify = True
    cfg.auto_apply = True
    notified = []
    monkeypatch.setattr(runner, "notify", lambda c, msg: notified.append(msg))
    applied = []
    monkeypatch.setattr(apply_mod, "apply_ticket", lambda tid, c, echo=print: applied.append(tid))
    t = make_ticket(cfg)
    n = runner.watch(cfg, interval=0, once=True, echo=lambda *a: None)
    assert n == 1
    assert notified == [f"ankifix: {t['id']} fixed"]
    assert applied == [t["id"]]


def test_watch_does_not_auto_apply_when_disabled(env, monkeypatch):
    cfg = env["cfg"]
    cfg.notify = False
    cfg.auto_apply = False
    applied = []
    monkeypatch.setattr(apply_mod, "apply_ticket", lambda tid, c, echo=print: applied.append(tid))
    make_ticket(cfg)
    runner.watch(cfg, interval=0, once=True, echo=lambda *a: None)
    assert applied == []


# --------------------------------------------------------------------- apply
def _apply_ticket_dict(tid, branch, base, tests=None):
    return {
        "schema": 1, "id": tid, "created": "2026-09-22T21:45:03-05:00", "source": "terminal",
        "note": "apply test", "status": "fixed", "kind": "app",
        "app": {"anki_version": None, "addon_repo": None, "branch": base, "commit": None, "dirty": False},
        "reviewer": {"state": None, "card_id": None, "note_id": None, "notetype": None, "deck": None,
                     "template_ord": None},
        "deck_build": None, "capture": None,
        "fix": {"branch": branch, "commits": ["abc123"], "tests": tests or [], "log_path": "fix.log",
                "started": "2026-09-22T21:46:00-05:00", "finished": "2026-09-22T21:50:00-05:00",
                "summary": "fixed"},
    }


@pytest.fixture
def apply_repo(tmp_path):
    """base branch `main` + a fix branch `fix/t1` with one commit changing
    a.txt's middle line. Left checked out on `main` (clean)."""
    repo = tmp_path / "apply-repo"
    repo.mkdir()
    sh(repo, "git", "init", "-q", "-b", "main")
    sh(repo, "git", "config", "user.name", "Test")
    sh(repo, "git", "config", "user.email", "test@example.com")
    (repo / "a.txt").write_text("line1\nline2\nline3\n")
    (repo / "b.txt").write_text("other\n")
    sh(repo, "git", "add", "-A")
    sh(repo, "git", "commit", "-q", "-m", "init")
    sh(repo, "git", "branch", "fix/t1")
    sh(repo, "git", "checkout", "-q", "fix/t1")
    (repo / "a.txt").write_text("line1\nCHANGED\nline3\n")
    sh(repo, "git", "add", "-A")
    sh(repo, "git", "commit", "-q", "-m", "Fix the fake bug\n\nCo-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>")
    sh(repo, "git", "checkout", "-q", "main")

    cfg = Config()
    cfg.app_repo = repo
    cfg.app_base_branch = "main"
    cfg.tickets_dir = tmp_path / "AnkiTickets"
    cfg.notify = False
    cfg.auto_restart_app = False  # never touch the real Anki+ from tests
    # these apply tests predate live tests; the live-test tests turn it on
    cfg.require_live_tests = False
    return {"cfg": cfg, "repo": repo}


def test_apply_ticket_non_overlapping_dirty_tree(apply_repo):
    cfg, repo = apply_repo["cfg"], apply_repo["repo"]
    (repo / "b.txt").write_text("other\nDIRTY EDIT\n")  # untouched by the fix commit
    t = _apply_ticket_dict("20260922-220000-apply-ok", "fix/t1", "main")
    save_ticket(cfg.tickets_dir, t)

    out = apply_mod.apply_ticket(t["id"], cfg, echo=lambda *a: None)

    assert "CHANGED" in (repo / "a.txt").read_text()
    assert "DIRTY EDIT" in (repo / "b.txt").read_text()  # dirty edit preserved
    assert sh(repo, "git", "log", "--oneline", "-1") .endswith("init")  # no commit created
    assert sh(repo, "git", "rev-parse", "--abbrev-ref", "HEAD") == "main"
    applied = out["fix"]["applied"]
    assert applied["patch"] == "apply.patch"
    assert applied["tests_passed"] is True
    assert (cfg.tickets_dir / t["id"] / "apply.patch").is_file()
    reread = load_ticket(cfg.tickets_dir, t["id"])
    assert reread["fix"]["applied"] == applied


def test_apply_ticket_overlapping_dirty_tree_leaves_tree_untouched(apply_repo):
    cfg, repo = apply_repo["cfg"], apply_repo["repo"]
    # dirty edit to the exact line the fix commit also changes -> no context match
    (repo / "a.txt").write_text("line1\nDIRTY CONFLICT\nline3\n")
    t = _apply_ticket_dict("20260922-220100-apply-conflict", "fix/t1", "main")
    save_ticket(cfg.tickets_dir, t)

    with pytest.raises(runner.AnkifixError):
        apply_mod.apply_ticket(t["id"], cfg, echo=lambda *a: None)

    assert (repo / "a.txt").read_text() == "line1\nDIRTY CONFLICT\nline3\n"  # untouched
    assert sh(repo, "git", "log", "--oneline", "-1") .endswith("init")  # no commit created
    reread = load_ticket(cfg.tickets_dir, t["id"])
    assert "error" in reread["fix"]["applied"]
    assert "patch" not in reread["fix"]["applied"]


def test_apply_ticket_rejects_deck_kind(apply_repo):
    cfg = apply_repo["cfg"]
    t = _apply_ticket_dict("20260922-220200-apply-deck", "fix/t1", "main")
    t["kind"] = "deck"
    save_ticket(cfg.tickets_dir, t)
    with pytest.raises(runner.AnkifixError):
        apply_mod.apply_ticket(t["id"], cfg, echo=lambda *a: None)


def test_apply_ticket_rejects_no_fix_branch(apply_repo):
    cfg = apply_repo["cfg"]
    t = _apply_ticket_dict("20260922-220300-apply-nobranch", None, "main")
    save_ticket(cfg.tickets_dir, t)
    with pytest.raises(runner.AnkifixError):
        apply_mod.apply_ticket(t["id"], cfg, echo=lambda *a: None)


# --------------------------------------------------------------- launchd agent
def test_write_plist_contents(tmp_path):
    dest = agent_mod.write_plist(
        tmp_path / "com.aidanjones.ankifix-watch.plist",
        ankifix_bin="/x/bin/ankifix", interval=20,
        log_path=str(tmp_path / "ankifix-watch.log"), path_env="/a/bin:/b/bin",
    )
    assert dest.is_file()
    import plistlib
    data = plistlib.loads(dest.read_bytes())
    assert data["Label"] == agent_mod.LABEL
    assert data["ProgramArguments"] == ["/x/bin/ankifix", "--watch", "--interval", "20"]
    assert data["RunAtLoad"] is True
    assert data["KeepAlive"] is True
    assert data["ThrottleInterval"] == 30
    assert data["StandardOutPath"] == str(tmp_path / "ankifix-watch.log")
    assert data["StandardErrorPath"] == str(tmp_path / "ankifix-watch.log")
    assert data["EnvironmentVariables"] == {"PATH": "/a/bin:/b/bin"}


def test_install_agent_refuses_when_watch_already_running(tmp_path, monkeypatch):
    monkeypatch.setattr(agent_mod, "running_watch_pids", lambda: [99999])
    dest = tmp_path / "x.plist"
    with pytest.raises(runner.AnkifixError):
        agent_mod.install_agent(plist_path=dest)
    assert not dest.exists()


# ------------------------------------------------ overnight crash-loop fixes
DRAG_NOTE = ("When selecting one card or multiple, I should be able to select one or a group "
             "and drag and drop it to another deck or a different place in the deck")


@pytest.mark.parametrize("note,reviewer,expected", [
    # ticket 20260923-085141: UI interaction words, no notetype, state question
    (DRAG_NOTE, {"state": "question", "notetype": None}, "app"),
    (DRAG_NOTE, {"state": None, "notetype": None}, "app"),
    (DRAG_NOTE, {}, "app"),
    ("I should be able to resort the list of decks", {"state": "deckBrowser", "notetype": None}, "app"),
    ("in full screen the dropdown is cut off", {"state": "question", "notetype": None}, "app"),
    # ticket 20260923-091337: deck names in the search bar / deck column
    ("The names of decks, as seen in the top search bar and in the deck column, shouldn't have "
     ":: or quotations, it should just be the clean title of the deck. That's it this should "
     "apply to the entire Anki Plus app. No unnecessary punctuation or formatting in deck names",
     {"state": "question", "card_id": None, "notetype": None}, "app"),
    # genuine deck notes still go to deck, with or without a card on screen
    ("the answer on card C2-Q05 is wrong", {"state": "question", "card_id": None, "notetype": None}, "deck"),
    ("the answer on card C2-Q05 is wrong", {"state": "answer", "notetype": None}, "deck"),
    ("the answer on card C2-Q05 is wrong", {"state": None, "notetype": None}, "deck"),
    ("the answer on card C2-Q05 is wrong", {"state": "answer", "notetype": "CourseB-Basic"}, "deck"),
    ("image on card is cut off", {"state": None, "notetype": None}, "deck"),
])
def test_classify_ui_interaction_vs_deck(note, reviewer, expected):
    t = {"note": note, "reviewer": reviewer, "capture": {"console_errors": []}}
    kind, reason = classify(t, Config())
    assert kind == expected, reason


TICKET_102640_NOTE = (
    "I should be able to right-click on an image and copy it or cut it. Add "
    "these two options to the pop-up menu. From there I should be able to "
    "paste a copied image from my clipboard into any field I want"
)


@pytest.mark.parametrize("note,notetype,expected", [
    # ticket 20260923-102640: captured while a CourseB-E1 card was
    # showing, but it's an app feature request (right-click/copy/cut/paste
    # a note-editor image) - the course notetype prefix must not force deck
    (TICKET_102640_NOTE, "CourseB-E1", "app"),
    # a course card whose note is about the card's own rendering must still
    # go to deck, even though it also contains app-ish words ("image", "cut")
    ("the image on this card is cut off", "CourseB-E1", "deck"),
])
def test_classify_course_notetype_is_evidence_not_verdict(note, notetype, expected):
    t = {"note": note, "reviewer": {"notetype": notetype}, "capture": None}
    kind, reason = classify(t, Config())
    assert kind == expected, reason


def test_classify_no_card_bias_leans_app():
    cfg = Config()
    t = {"note": "the card", "reviewer": {"state": "question", "notetype": None}}
    assert classify(t, cfg)[0] == "deck"  # 1 deck word, captured on a card
    cfg.no_card_app_bias = 1.0
    t["reviewer"]["state"] = "deckBrowser"
    kind, reason = classify(t, cfg)
    assert kind == "app" and "leaning app" in reason


def test_watch_survives_a_ticket_that_raises(env, monkeypatch):
    cfg = env["cfg"]
    make_ticket(cfg, tid="t1", created="2026-09-22T21:00:00-05:00")
    make_ticket(cfg, tid="t2", created="2026-09-22T22:00:00-05:00")
    notified = []
    cfg.notify = True
    monkeypatch.setattr(runner, "notify", lambda c, msg: notified.append(msg))
    real = runner.run_ticket

    def fake_run(tid, c, **kw):
        if tid == "t1":
            raise RuntimeError("boom from a fake run")
        return real(tid, c, **kw)

    monkeypatch.setattr(runner, "run_ticket", fake_run)
    seen = []
    n = runner.watch(cfg, interval=0, once=True, echo=seen.append)
    assert n == 2
    t1 = load_ticket(cfg.tickets_dir, "t1")
    assert t1["status"] == "failed"
    assert "RuntimeError: boom from a fake run" in t1["fix"]["summary"]
    err = (cfg.tickets_dir / "t1" / "error.log").read_text()
    assert "Traceback" in err and "boom from a fake run" in err
    assert load_ticket(cfg.tickets_dir, "t2")["status"] == "fixed"
    assert notified == ["ankifix: t1 failed", "ankifix: t2 fixed"]


def test_run_ticket_records_plan_failure_instead_of_raising(env, monkeypatch):
    cfg = env["cfg"]
    t = make_ticket(cfg)

    def bad_plan(*a, **k):
        raise ValueError("plan exploded")

    monkeypatch.setattr(runner, "plan", bad_plan)
    out = runner.run_ticket(t["id"], cfg, echo=lambda *a: None)
    assert out["status"] == "failed"  # direct (terminal) run: no automatic retry
    assert out["fix"]["attempts"] == 1
    assert "ValueError: plan exploded" in out["fix"]["summary"]
    assert load_ticket(cfg.tickets_dir, t["id"])["status"] == "failed"
    assert "plan exploded" in (cfg.tickets_dir / t["id"] / "error.log").read_text()


def test_deck_ticket_without_files_access_is_needs_review(env, monkeypatch):
    cfg = env["cfg"]
    t = make_ticket(cfg, note="the answer on card C1-Q04 is wrong", kind="deck",
                    deck_build={"course_dir": str(env["course"]), "build_marker": None})
    real_open = open

    def tcc_open(path, *a, **k):
        if str(path).startswith(str(env["build"])):
            raise PermissionError(1, "Operation not permitted", str(path))
        return real_open(path, *a, **k)

    monkeypatch.setattr("builtins.open", tcc_open)
    monkeypatch.setattr(runner, "tcc_binary", lambda: "/X/Python.app")
    cfg.terminal_fallback = False
    out = runner.run_ticket(t["id"], cfg, echo=lambda *a: None)
    assert out["status"] == "needs-review"
    s = out["fix"]["summary"]
    assert "deck fixes need Files access for the watcher" in s
    assert f"run `ankifix {t['id']}` from a terminal" in s
    assert "grant Full Disk Access to /X/Python.app in System Settings > Privacy & Security" in s
    assert out["fix"]["blocked"] == runner.FILES_ACCESS
    assert not (env["fake"] / "argv.txt").exists()  # claude never ran
    # watch() never picks it up again; a terminal run needs no --force
    monkeypatch.setattr("builtins.open", real_open)
    assert runner.next_new(cfg) is None
    assert runner.run_ticket(t["id"], cfg, echo=lambda *a: None)["status"] == "fixed"


def test_files_access_error_detection():
    assert runner.is_files_access_error(PermissionError(1, "Operation not permitted"))
    assert not runner.is_files_access_error(PermissionError(13, "Permission denied"))
    assert not runner.is_files_access_error(FileNotFoundError(2, "x"))


def test_child_env_has_explicit_path(env, monkeypatch):
    cfg = env["cfg"]
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    cfg.extra_path = ["/opt/node/bin", "/usr/bin"]
    e = runner.child_env(cfg, "t1", "app")
    assert e["PATH"] == "/usr/bin:/bin:/opt/node/bin"
    assert e["NODE_PATH"] == "/fake/node_modules"


def test_write_plist_path_contains_resolved_node_dir(tmp_path, monkeypatch):
    bindir = tmp_path / "node-home" / "bin"
    bindir.mkdir(parents=True)
    node = bindir / "node"
    node.write_text("#!/bin/sh\n")
    node.chmod(0o755)
    monkeypatch.setenv("PATH", f"/usr/bin:/bin:{bindir}")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(agent_mod, "running_watch_pids", lambda: [])
    dest = agent_mod.install_agent(plist_path=tmp_path / "x.plist", load=False)
    import plistlib
    envv = plistlib.loads(dest.read_bytes())["EnvironmentVariables"]
    parts = envv["PATH"].split(":")
    assert str(bindir) in parts
    assert parts.index(str(bindir)) < parts.index("/opt/homebrew/bin")  # resolved dirs first
    assert envv["HOME"] == str(tmp_path)


def test_apply_exports_node_path_and_times_out(apply_repo, monkeypatch):
    cfg, repo = apply_repo["cfg"], apply_repo["repo"]
    cfg.apply_node_path = "/live/node_modules"
    cfg.apply_test_timeout_s = 1
    t = _apply_ticket_dict("20260922-220400-apply-env", "fix/t1", "main", tests=[
        'test "$NODE_PATH" = /live/node_modules && command -v node >/dev/null || command -v git >/dev/null',
        "sleep 30",
    ])
    save_ticket(cfg.tickets_dir, t)
    import time as _time
    t0 = _time.time()
    out = apply_mod.apply_ticket(t["id"], cfg, echo=lambda *a: None)
    assert _time.time() - t0 < 15
    applied = out["fix"]["applied"]
    assert applied["tests_passed"] is False
    assert applied["tests_timed_out"] == ["sleep 30"]
    env = apply_mod.test_env(cfg)
    assert env["NODE_PATH"] == "/live/node_modules"
    assert env["PATH"] == cfg.subprocess_path()


def test_apply_excludes_known_failing_tests_and_flips_to_fixed(apply_repo):
    """A ticket the fixer left needs-review only because a known-failing,
    pre-existing test was on the harness list: apply excludes it, the fix's
    own test passes, and the ticket's status is corrected to fixed without a
    human relabeling it."""
    cfg, repo = apply_repo["cfg"], apply_repo["repo"]
    cfg.known_failing_tests = ["tests/editor_tools.cjs"]
    t = _apply_ticket_dict("20260923-100000-apply-known-failing", "fix/t1", "main", tests=[
        "node tests/editor_tools.cjs",  # known failing: must be skipped, not run
        "true",  # the fix's own test: passes
    ])
    t["status"] = "needs-review"
    save_ticket(cfg.tickets_dir, t)

    out = apply_mod.apply_ticket(t["id"], cfg, echo=lambda *a: None)

    applied = out["fix"]["applied"]
    assert applied["tests_passed"] is True
    assert applied["tests_skipped"] == ["node tests/editor_tools.cjs"]
    assert "tests_timed_out" not in applied
    assert out["status"] == "fixed"
    reread = load_ticket(cfg.tickets_dir, t["id"])
    assert reread["status"] == "fixed"
    assert reread["fix"]["applied"]["tests_skipped"] == ["node tests/editor_tools.cjs"]


def test_apply_real_failure_keeps_needs_review(apply_repo):
    """A genuine failure in the fix's own (non-excluded) test must still land
    needs-review, even with a known-failing test also on the list."""
    cfg, repo = apply_repo["cfg"], apply_repo["repo"]
    cfg.known_failing_tests = ["tests/editor_tools.cjs"]
    t = _apply_ticket_dict("20260923-100100-apply-real-failure", "fix/t1", "main", tests=[
        "node tests/editor_tools.cjs",  # known failing: skipped
        "false",  # a genuine failure of the fix's own test
    ])
    t["status"] = "fixed"
    save_ticket(cfg.tickets_dir, t)

    out = apply_mod.apply_ticket(t["id"], cfg, echo=lambda *a: None)

    applied = out["fix"]["applied"]
    assert applied["tests_passed"] is False
    assert applied["tests_skipped"] == ["node tests/editor_tools.cjs"]
    assert out["status"] == "needs-review"
    reread = load_ticket(cfg.tickets_dir, t["id"])
    assert reread["status"] == "needs-review"


def test_retry_cap_gives_up_after_two_attempts(env, monkeypatch):
    cfg = env["cfg"]
    t = make_ticket(cfg)
    calls = []

    def bad_plan(*a, **k):
        calls.append(1)
        raise ValueError("plan exploded")

    monkeypatch.setattr(runner, "plan", bad_plan)
    runner.watch(cfg, interval=0, once=True, echo=lambda *a: None)
    t1 = load_ticket(cfg.tickets_dir, t["id"])
    assert t1["status"] == "new" and t1["fix"]["attempts"] == 1
    assert "will retry" in t1["fix"]["summary"]
    runner.watch(cfg, interval=0, once=True, echo=lambda *a: None)
    t2 = load_ticket(cfg.tickets_dir, t["id"])
    assert t2["status"] == "failed" and t2["fix"]["attempts"] == 2
    assert "gave up after 2 attempts" in t2["fix"]["summary"]
    runner.watch(cfg, interval=0, once=True, echo=lambda *a: None)
    assert len(calls) == 2  # never retried automatically again


def test_stale_fixing_ticket_is_requeued_then_given_up(env):
    cfg = env["cfg"]
    fix = runner.new_fix("2026-09-23T01:00:00-05:00")
    fix.update(pid=999999, attempts=1)
    t = make_ticket(cfg, status="fixing", fix=fix)
    assert runner.recover_stale(cfg, echo=lambda *a: None) == [t["id"]]
    assert load_ticket(cfg.tickets_dir, t["id"])["status"] == "new"
    fix["attempts"] = 2
    t = make_ticket(cfg, status="fixing", fix=fix)
    runner.recover_stale(cfg, echo=lambda *a: None)
    t2 = load_ticket(cfg.tickets_dir, t["id"])
    assert t2["status"] == "failed" and "gave up after 2 attempts" in t2["fix"]["summary"]
    # a live pid (this process) is not stale
    fix.update(pid=os.getpid(), attempts=1)
    make_ticket(cfg, status="fixing", fix=fix)
    assert runner.recover_stale(cfg, echo=lambda *a: None) == []


def test_watch_logs_startup_line(env):
    seen = []
    runner.watch(env["cfg"], interval=0, once=True, echo=seen.append)
    assert seen[0].startswith(f"ankifix: watcher started pid {os.getpid()}")


FAKE_OSASCRIPT = r"""#!/bin/bash
# Fake osascript: record the script; for a Terminal `do script "..."`, run the
# shell command in the background the way Terminal.app would.
printf '%s\n' "$@" >> "$FAKE_DIR/osascript.txt"
if [ -n "$FAKE_OSA_DENY" ]; then
  echo "execution error: Not authorized to send Apple events to Terminal. (-1743)" >&2
  exit 1
fi
script="$2"
cmd=$(printf '%s' "$script" | sed -n 's/^tell application "Terminal" to do script "\(.*\)"$/\1/p' | sed 's/\\"/"/g')
[ -n "$cmd" ] && (bash -c "$cmd" > "$FAKE_DIR/terminal_out.txt" 2>&1 &)
exit 0
"""


def _tcc_deck_setup(env, monkeypatch):
    cfg = env["cfg"]
    bindir = env["tmp"] / "bin"
    osa = bindir / "osascript"
    osa.write_text(FAKE_OSASCRIPT)
    osa.chmod(0o755)
    ankifix_bin = bindir / "ankifix-test"
    ankifix_bin.write_text(f"#!/bin/bash\nexec {sys.executable} -m ankifix \"$@\"\n")
    ankifix_bin.chmod(0o755)
    cfg.ankifix_bin = str(ankifix_bin)
    cfg.osascript_bin = str(osa)
    cfg.terminal_poll_s = 0.2
    cfg.terminal_fallback_timeout_min = 1
    # the Terminal child reads config from the environment
    monkeypatch.setenv("ANKIFIX_TICKETS_DIR", str(cfg.tickets_dir))
    monkeypatch.setenv("ANKIFIX_APP_REPO", str(cfg.app_repo))
    monkeypatch.setenv("ANKIFIX_DECK_BUILD_DIR", str(env["build"]))
    monkeypatch.setenv("PYTHONPATH", str(ROOT))
    # only this (the watcher) process is TCC-blocked; the Terminal child is not
    monkeypatch.setattr(runner, "probe_files_access",
                        lambda d: PermissionError(1, "Operation not permitted", str(d / "build_out.json")))
    t = make_ticket(cfg, note="the answer on card C1-Q04 is wrong", kind="deck",
                    deck_build={"course_dir": str(env["course"]), "build_marker": None})
    return cfg, t


def test_tcc_deck_ticket_runs_through_terminal(env, monkeypatch):
    cfg, t = _tcc_deck_setup(env, monkeypatch)
    out = runner.run_ticket(t["id"], cfg, echo=lambda *a: None)
    assert out["status"] == "fixed", (env["fake"] / "terminal_out.txt").read_text()
    assert out["kind"] == "deck"
    osa = (env["fake"] / "osascript.txt").read_text()
    assert 'tell application "Terminal" to do script' in osa
    assert f"{t['id']} --once-from-terminal" in osa
    assert "APPLY WITH ANKI+ CLOSED" in out["fix"]["summary"]


def test_tcc_deck_ticket_osascript_denied_is_needs_review(env, monkeypatch):
    cfg, t = _tcc_deck_setup(env, monkeypatch)
    monkeypatch.setenv("FAKE_OSA_DENY", "1")
    monkeypatch.setattr(runner, "tcc_binary", lambda: "/X/Python.app")
    out = runner.run_ticket(t["id"], cfg, echo=lambda *a: None)
    assert out["status"] == "needs-review"
    s = out["fix"]["summary"]
    assert "deck fixes need Files access for the watcher" in s
    assert "grant Full Disk Access to /X/Python.app" in s
    assert "Not authorized to send Apple events" in s
    assert out["fix"]["blocked"] == runner.FILES_ACCESS


def test_tcc_deck_terminal_child_killed_is_marked_failed_within_one_poll(env, monkeypatch):
    """20260923-102640: the orchestrator killed both the `ankifix <id>
    --once-from-terminal` process and its claude child. The old poll loop
    only asked "has status left {fixing, new}?", which a killed child (status
    stuck at "fixing" forever, nothing rewrites ticket.json again) never
    satisfies, so it sat out the rest of terminal_fallback_timeout_min. Now
    every poll also checks whether the pid recorded in fix.pid is still
    alive, and ends the wait (failed, "terminal run ended without a result")
    within one poll interval once it isn't."""
    import threading
    import time as _time

    cfg, t = _tcc_deck_setup(env, monkeypatch)
    monkeypatch.setenv("FAKE_MODE", "sleep")  # the terminal child's `claude` blocks
    cfg.terminal_fallback_timeout_min = 2  # generous cap we expect to finish well under

    killed = {}

    def killer():
        deadline = _time.time() + 10
        while _time.time() < deadline:
            try:
                cur = load_ticket(cfg.tickets_dir, t["id"])
            except (OSError, ValueError):
                cur = {}
            pid = (cur.get("fix") or {}).get("pid")
            if pid and pid != os.getpid():
                try:
                    os.kill(pid, 9)
                except ProcessLookupError:
                    pass
                killed["pid"] = pid
                return
            _time.sleep(0.05)

    th = threading.Thread(target=killer, daemon=True)
    th.start()
    t0 = _time.time()
    out = runner.run_ticket(t["id"], cfg, echo=lambda *a: None)
    elapsed = _time.time() - t0
    th.join(timeout=2)

    assert killed.get("pid"), "killer thread never saw the terminal child's pid"
    assert out["status"] == "failed"
    assert out["fix"]["summary"].startswith("terminal run ended without a result")
    # caught within about one poll interval (0.2s), nowhere near the timeout cap
    assert elapsed < 5


def test_tcc_deck_terminal_child_killed_and_manually_requeued_keeps_human_status(env, monkeypatch):
    """Same incident, but someone also ran `ankibug set <id> status=new` by
    hand right after killing the child (to try to requeue it) before the
    watcher's poll caught up. The wait still ends quickly instead of sitting
    out the timeout (that hang needed a watcher restart in the field), and
    since 20260924-131713 the human's "new" is kept, not overwritten with
    "failed": the Terminal child never sets "new" itself, so a "new" is
    always someone's decision to requeue."""
    import threading
    import time as _time

    cfg, t = _tcc_deck_setup(env, monkeypatch)
    monkeypatch.setenv("FAKE_MODE", "sleep")
    cfg.terminal_fallback_timeout_min = 2

    killed = {}

    def killer():
        deadline = _time.time() + 10
        while _time.time() < deadline:
            try:
                cur = load_ticket(cfg.tickets_dir, t["id"])
            except (OSError, ValueError):
                cur = {}
            pid = (cur.get("fix") or {}).get("pid")
            if pid and pid != os.getpid():
                try:
                    os.kill(pid, 9)
                except ProcessLookupError:
                    pass
                killed["pid"] = pid
                cur["status"] = "new"  # simulate the manual `ankibug set`
                save_ticket(cfg.tickets_dir, cur)
                return
            _time.sleep(0.05)

    th = threading.Thread(target=killer, daemon=True)
    th.start()
    t0 = _time.time()
    out = runner.run_ticket(t["id"], cfg, echo=lambda *a: None)
    elapsed = _time.time() - t0
    th.join(timeout=2)

    assert killed.get("pid"), "killer thread never saw the terminal child's pid"
    assert out["status"] == "new"
    assert load_ticket(cfg.tickets_dir, t["id"])["status"] == "new"
    assert elapsed < 5


def test_install_agent_refuses_when_tools_missing(tmp_path, monkeypatch):
    from ankifix import doctor as doctor_mod
    monkeypatch.setenv("PATH", str(tmp_path))  # nothing resolvable
    monkeypatch.setattr(agent_mod, "DEFAULT_PATH_ENV", str(tmp_path))
    monkeypatch.setattr(agent_mod, "running_watch_pids", lambda: [])
    monkeypatch.setattr(doctor_mod, "check_notifications", lambda c, p: doctor_mod.Check("n", True, ""))
    cfg = Config()
    cfg.tickets_dir = tmp_path / "t"
    cfg.deck_build_dir = tmp_path
    dest = tmp_path / "x.plist"
    lines = []
    with pytest.raises(runner.AnkifixError, match="refusing to install"):
        agent_mod.install_agent(plist_path=dest, cfg=cfg, echo=lines.append, load=False)
    assert not dest.exists()
    assert any(l.startswith("FAIL  node on agent PATH") for l in lines)


def test_doctor_exit_code_and_output(tmp_path, monkeypatch):
    from ankifix import doctor as doctor_mod
    cfg = Config()
    cfg.tickets_dir = tmp_path / "t"
    cfg.deck_build_dir = tmp_path / "nope"
    monkeypatch.setattr(doctor_mod, "check_agent", lambda: doctor_mod.Check("agent", True, "pid 1"))
    monkeypatch.setattr(doctor_mod, "check_notifications", lambda c, p: doctor_mod.Check("n", True, ""))
    lines = []
    rc = doctor_mod.doctor(cfg, echo=lines.append, path=str(tmp_path))
    assert rc == 1
    assert any(l.startswith("PASS  tickets dir writable") for l in lines)
    assert any(l.startswith("FAIL  course build dir readable") for l in lines)


def test_watch_check_notifies_on_flip(tmp_path, monkeypatch):
    from ankifix import doctor as doctor_mod
    cfg = Config()
    cfg.notify = True
    sent = []
    monkeypatch.setattr(doctor_mod, "notify", lambda c, m: sent.append(m))
    state = {"x": True}
    monkeypatch.setattr(doctor_mod, "run_checks",
                        lambda *a, **k: [doctor_mod.Check("x", state["x"], "")])
    prev = doctor_mod.watch_check(cfg, {}, echo=lambda *a: None)
    assert sent == []
    state["x"] = False
    prev = doctor_mod.watch_check(cfg, prev, echo=lambda *a: None)
    prev = doctor_mod.watch_check(cfg, prev, echo=lambda *a: None)
    assert sent == ["ankifix doctor FAIL: x"]  # once, on the flip


# ------------------------------------------------------------ live tests
FAKE_HARNESS = r'''#!/usr/bin/env python3
# Fake tests/live/run_in_app.py: records argv; a test "fails" if its file says FAIL.
import os, sys
args = sys.argv[1:]
with open(os.environ.get("FAKE_HARNESS_LOG", "/dev/null"), "a") as fh:
    fh.write(" ".join(args) + "\n")
scripts = [a for a in args if a.endswith(".py")]
bad = [s for s in scripts if "FAIL" in open(s).read()]
for s in scripts:
    print(("FAIL " if s in bad else "PASS ") + s + ": check -- detail")
sys.exit(1 if bad else 0)
'''


def test_app_run_records_live_tests_and_allows_harness(env):
    cfg = env["cfg"]
    t = make_ticket(cfg)
    out = runner.run_ticket(t["id"], cfg, echo=lambda *a: None)
    assert out["status"] == "fixed", out["fix"]["summary"]
    assert out["fix"]["live_tests"] == ["tests/live/test_fake_bug.py"]
    argv = (env["fake"] / "argv.txt").read_text().splitlines()
    allowed = argv[argv.index("--allowedTools") + 1]
    assert f"Bash({cfg.anki_pyenv} tests/live/run_in_app.py*)" in allowed
    prompt = (env["fake"] / "prompt.txt").read_text()
    assert f"{cfg.anki_pyenv} tests/live/run_in_app.py tests/live/test_" in prompt
    assert '"live_tests"' in prompt


def test_app_run_without_live_test_is_needs_review(env, monkeypatch):
    monkeypatch.setenv("FAKE_MODE", "nolive")
    cfg = env["cfg"]
    t = make_ticket(cfg)
    out = runner.run_ticket(t["id"], cfg, echo=lambda *a: None)
    assert out["status"] == "needs-review"
    assert "no live test" in out["fix"]["summary"]
    assert out["fix"]["live_tests"] == []


def test_parse_live_tests_normalizes():
    block = {"live_tests": [
        "tests/live/test_deck_reorder.py",
        "/x/out/pyenv/bin/python tests/live/run_in_app.py tests/live/test_a.py tests/live/test_b.py",
        "tests/live/test_a.py", "node tests/reviewer_click.cjs", 7,
    ]}
    assert runner.parse_live_tests(block) == [
        "tests/live/test_deck_reorder.py", "tests/live/test_a.py", "tests/live/test_b.py",
    ]
    assert runner.parse_live_tests({"live_tests": "tests/live/test_x.py"}) == ["tests/live/test_x.py"]
    assert runner.parse_live_tests({}) == []


@pytest.fixture
def live_repo(apply_repo, tmp_path, monkeypatch):
    cfg, repo = apply_repo["cfg"], apply_repo["repo"]
    (repo / "tests" / "live").mkdir(parents=True)
    (repo / "tests" / "live" / "run_in_app.py").write_text(FAKE_HARNESS)
    (repo / "tests" / "live" / "test_ok.py").write_text("def run(t): pass\n")
    (repo / "tests" / "live" / "test_bad.py").write_text("# FAIL\n")
    log = tmp_path / "harness.log"
    monkeypatch.setenv("FAKE_HARNESS_LOG", str(log))
    cfg.anki_pyenv = sys.executable
    cfg.require_live_tests = True
    cfg.live_test_timeout_s = 30
    return {"cfg": cfg, "repo": repo, "log": log}


def test_apply_runs_live_tests_green_is_fixed(live_repo):
    cfg, repo = live_repo["cfg"], live_repo["repo"]
    t = _apply_ticket_dict("20260923-110000-live-ok", "fix/t1", "main", tests=["true"])
    t["fix"]["live_tests"] = ["tests/live/test_ok.py"]
    save_ticket(cfg.tickets_dir, t)
    out = apply_mod.apply_ticket(t["id"], cfg, echo=lambda *a: None)
    applied = out["fix"]["applied"]
    assert out["status"] == "fixed"
    assert applied["live_tests_passed"] is True
    assert applied["live_tests"][0]["test"] == "tests/live/test_ok.py"
    assert "landed" not in applied  # auto_restart_app is off in this fixture
    ran = live_repo["log"].read_text()
    assert "--timeout 30 tests/live/test_ok.py" in ran


def test_apply_live_test_failure_is_needs_review_not_landed(live_repo, monkeypatch):
    cfg = live_repo["cfg"]
    cfg.auto_restart_app = True
    from ankifix import restart as restart_mod
    landed = []
    monkeypatch.setattr(restart_mod, "land", lambda c, tid, echo=print: landed.append(tid))
    t = _apply_ticket_dict("20260923-110100-live-bad", "fix/t1", "main", tests=["true"])
    t["fix"]["live_tests"] = ["tests/live/test_ok.py", "tests/live/test_bad.py"]
    save_ticket(cfg.tickets_dir, t)
    out = apply_mod.apply_ticket(t["id"], cfg, echo=lambda *a: None)
    applied = out["fix"]["applied"]
    assert out["status"] == "needs-review"
    assert applied["live_tests_passed"] is False
    assert [r["ok"] for r in applied["live_tests"]] == [True, False]
    assert "FAIL tests/live/test_bad.py" in applied["live_tests"][1]["output"]
    assert applied["landed"]["state"] == "not-landed"
    assert "live test failed" in applied["landed"]["reason"]
    assert "live test failed: tests/live/test_bad.py" in out["fix"]["summary"]
    assert landed == []


def test_apply_without_live_test_is_needs_review(live_repo):
    cfg = live_repo["cfg"]
    t = _apply_ticket_dict("20260923-110200-live-none", "fix/t1", "main", tests=["true"])
    save_ticket(cfg.tickets_dir, t)
    out = apply_mod.apply_ticket(t["id"], cfg, echo=lambda *a: None)
    assert out["status"] == "needs-review"
    assert out["fix"]["applied"]["landed"]["reason"] == "no live test"


def test_apply_missing_harness_fails_live(live_repo):
    cfg, repo = live_repo["cfg"], live_repo["repo"]
    (repo / "tests" / "live" / "run_in_app.py").unlink()
    t = _apply_ticket_dict("20260923-110300-live-noharness", "fix/t1", "main", tests=["true"])
    t["fix"]["live_tests"] = ["tests/live/test_ok.py"]
    save_ticket(cfg.tickets_dir, t)
    out = apply_mod.apply_ticket(t["id"], cfg, echo=lambda *a: None)
    assert out["status"] == "needs-review"
    assert "not found" in out["fix"]["applied"]["live_tests"][0]["reason"]


def test_apply_green_lands_via_restart(live_repo, monkeypatch):
    cfg = live_repo["cfg"]
    cfg.auto_restart_app = True
    from ankifix import restart as restart_mod
    landed = []
    monkeypatch.setattr(restart_mod, "land", lambda c, tid, echo=print: landed.append(tid))
    t = _apply_ticket_dict("20260923-110400-live-land", "fix/t1", "main", tests=["true"])
    t["fix"]["live_tests"] = ["tests/live/test_ok.py"]
    save_ticket(cfg.tickets_dir, t)
    out = apply_mod.apply_ticket(t["id"], cfg, echo=lambda *a: None)
    assert out["status"] == "fixed"
    assert landed == [t["id"]]


# ------------------------------------------------------ landing (restart)
LAND_FAKE_PGREP = """#!/bin/bash
# running iff $FAKE_DIR/running exists; prints a pid like pgrep
echo "pgrep $*" >> "$FAKE_DIR/calls.txt"
[ -f "$FAKE_DIR/running" ] && { echo 424242; exit 0; }
exit 1
"""
LAND_FAKE_OSASCRIPT = """#!/bin/bash
echo "osascript $*" >> "$FAKE_DIR/calls.txt"
# the app quits unless told to hang; osascript reports the benign -128
[ -f "$FAKE_DIR/hang" ] || rm -f "$FAKE_DIR/running"
echo "execution error: User canceled. (-128)" >&2
exit 1
"""
LAND_FAKE_OPEN = """#!/bin/bash
echo "open $*" >> "$FAKE_DIR/calls.txt"
touch "$FAKE_DIR/running"
exit 0
"""


class _CDP:
    """A fake Chromium /json endpoint serving whatever `pages` holds."""

    def __init__(self):
        import http.server
        import threading

        outer = self
        self.pages = []

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                body = json.dumps(outer.pages).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        self.srv = http.server.HTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}/json"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self):
        self.srv.shutdown()


def _page(title, url="http://127.0.0.1:40000/_anki/legacyPageData?id=1"):
    return {"type": "page", "title": title, "url": url, "webSocketDebuggerUrl": "ws://fake"}


MAIN_PAGES = [_page("main webview"), _page("top toolbar"), _page("bottom toolbar"),
              {"type": "page", "title": "", "url": ""}, _page("about:blank", "about:blank")]


@pytest.fixture
def land_env(tmp_path, monkeypatch):
    from ankifix import restart as restart_mod
    fake = tmp_path / "fake"
    fake.mkdir()
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for name, body in (("pgrep", LAND_FAKE_PGREP), ("osascript", LAND_FAKE_OSASCRIPT), ("open", LAND_FAKE_OPEN)):
        f = bindir / name
        f.write_text(body)
        f.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_DIR", str(fake))
    (fake / "running").touch()
    cdp = _CDP()
    cfg = Config()
    cfg.tickets_dir = tmp_path / "AnkiTickets"
    cfg.cdp_url = cdp.url
    cfg.extra_path = [str(bindir)]
    cfg.restart_quit_timeout_s = 2
    notes = []
    monkeypatch.setattr(restart_mod, "notify", lambda c, msg: notes.append(msg))
    monkeypatch.setattr(restart_mod, "app_started_at", lambda c: None)
    t = _apply_ticket_dict("20260923-120000-land", "fix/t1", "main")
    t["fix"]["applied"] = {"at": "x", "patch": "apply.patch", "tests_passed": True}
    save_ticket(cfg.tickets_dir, t)
    yield {"cfg": cfg, "cdp": cdp, "fake": fake, "notes": notes, "id": t["id"], "mod": restart_mod}
    cdp.close()


def _calls(e):
    p = e["fake"] / "calls.txt"
    return p.read_text().splitlines() if p.exists() else []


def test_land_restarts_when_on_deck_list(land_env):
    e = land_env
    e["cdp"].pages = MAIN_PAGES
    state = e["mod"].land(e["cfg"], e["id"], echo=lambda *a: None, probe=lambda p: "decklist")
    assert state == "restarted"
    calls = _calls(e)
    assert 'osascript -e tell application "Anki+" to quit' in calls
    assert calls[-1] == "open -na Anki+"
    assert all("kill" not in c for c in calls)
    assert e["notes"] == [f"ankifix: restarting Anki+ to load {e['id']}"]
    landed = load_ticket(e["cfg"].tickets_dir, e["id"])["fix"]["applied"]["landed"]
    assert landed["state"] == "restarted"
    assert e["mod"].load_pending(e["cfg"]) is None


def test_land_congrats_url_is_safe_without_probe(land_env):
    e = land_env
    e["cdp"].pages = [_page("main webview", "http://127.0.0.1:40000/congrats#night")] + MAIN_PAGES[1:]

    def no_probe(p):
        raise AssertionError("congrats URL needs no probe")

    ok, why = e["mod"].check_safe(e["cfg"], probe=no_probe)
    assert ok and "congrats" in why


@pytest.mark.parametrize("pages,probe,reason", [
    (MAIN_PAGES, "reviewer", "main window shows reviewer"),
    (MAIN_PAGES, "other", "main window shows other"),
    (MAIN_PAGES + [_page("editor")], "decklist", "other Anki windows open: editor"),
    (MAIN_PAGES + [_page("deck options")], "decklist", "other Anki windows open: deck options"),
    ([_page("top toolbar")], "decklist", "no main webview"),
])
def test_check_safe_refuses(land_env, pages, probe, reason):
    e = land_env
    e["cdp"].pages = pages
    ok, why = e["mod"].check_safe(e["cfg"], probe=lambda p: probe)
    assert not ok and reason in why


def test_check_safe_cdp_down(land_env):
    e = land_env
    e["cfg"].cdp_url = "http://127.0.0.1:9/json"
    ok, why = e["mod"].check_safe(e["cfg"], probe=lambda p: "decklist")
    assert not ok and "not reachable" in why


def test_land_unsafe_queues_retry_and_tick_lands_later(land_env):
    e = land_env
    mod, cfg = e["mod"], e["cfg"]
    e["cdp"].pages = MAIN_PAGES
    state = mod.land(cfg, e["id"], echo=lambda *a: None, probe=lambda p: "reviewer")
    assert state == "pending"
    assert not any(c.startswith("osascript") for c in _calls(e))
    assert e["notes"] == [f"ankifix: {e['id']} fix applied, will load on next restart"]
    pend = mod.load_pending(cfg)
    assert pend["ids"] == [e["id"]]
    t0 = pend["first"]
    # within the 5 minute retry interval: nothing happens
    assert mod.tick(cfg, echo=lambda *a: None, now=t0 + 60, probe=lambda p: "decklist") is None
    # due, still unsafe: stays pending
    assert mod.tick(cfg, echo=lambda *a: None, now=t0 + 301, probe=lambda p: "reviewer") == "pending"
    # due again, now on the deck list: restarts and clears the queue
    assert mod.tick(cfg, echo=lambda *a: None, now=t0 + 700, probe=lambda p: "decklist") == "restarted"
    assert mod.load_pending(cfg) is None
    assert _calls(e)[-1] == "open -na Anki+"


def test_tick_gives_up_after_window(land_env):
    e = land_env
    mod, cfg = e["mod"], e["cfg"]
    e["cdp"].pages = MAIN_PAGES
    mod.land(cfg, e["id"], echo=lambda *a: None, probe=lambda p: "reviewer")
    t0 = mod.load_pending(cfg)["first"]
    assert mod.tick(cfg, echo=lambda *a: None, now=t0 + 7201, probe=lambda p: "decklist") == "gave-up"
    assert mod.load_pending(cfg) is None
    landed = load_ticket(cfg.tickets_dir, e["id"])["fix"]["applied"]["landed"]
    assert landed["state"] == "gave-up"
    assert not any(c.startswith("osascript") for c in _calls(e))


def test_tick_sees_user_restart(land_env, monkeypatch):
    e = land_env
    mod, cfg = e["mod"], e["cfg"]
    e["cdp"].pages = MAIN_PAGES
    mod.land(cfg, e["id"], echo=lambda *a: None, probe=lambda p: "reviewer")
    t0 = mod.load_pending(cfg)["first"]
    monkeypatch.setattr(mod, "app_started_at", lambda c: t0 + 100)
    assert mod.tick(cfg, echo=lambda *a: None, now=t0 + 400, probe=lambda p: "decklist") == "loaded"
    assert not any(c.startswith("osascript") for c in _calls(e))


def test_land_never_force_kills_a_hung_app(land_env):
    e = land_env
    (e["fake"] / "hang").touch()
    e["cdp"].pages = MAIN_PAGES
    state = e["mod"].land(e["cfg"], e["id"], echo=lambda *a: None, probe=lambda p: "decklist")
    assert state == "failed"
    calls = _calls(e)
    assert not any(c.startswith("open") for c in calls)
    assert all("kill" not in c for c in calls)
    landed = load_ticket(e["cfg"].tickets_dir, e["id"])["fix"]["applied"]["landed"]
    assert "not force-killing" in landed["reason"]


def test_land_when_app_not_running(land_env):
    e = land_env
    (e["fake"] / "running").unlink()
    state = e["mod"].land(e["cfg"], e["id"], echo=lambda *a: None, probe=lambda p: "decklist")
    assert state == "not-running"
    assert not any(c.startswith(("osascript", "open")) for c in _calls(e))
