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
    echo '{"type":"result","subtype":"success","is_error":false,"num_turns":7,"total_cost_usd":0.42,"session_id":"fake-session-1","result":"Done.\n\n```ankifix-result\n{\"status\": \"fixed\", \"tests_green\": true, \"tests\": [\"node tests/reviewer_click.cjs\"], \"reproduced\": true, \"pushed\": true, \"summary\": \"Root cause X; fixed Y; added test Z.\"}\n```\n"}'
    ;;
  deck)
    echo "- set: {id: C1-Q04, Answer: fixed}" >> cards/overrides.yaml
    printf 'warn something\nRESULT: PASS (0 failures, 1 warnings)\n' > preflight_report.txt
    echo '{"type":"assistant","message":{"content":[{"type":"tool_use","name":"Bash","input":{"command":"/x/fb-anki/bin/python build_deck.py"}}]}}'
    echo '{"type":"result","subtype":"success","is_error":false,"num_turns":5,"total_cost_usd":0.1,"result":"```ankifix-result\n{\"status\": \"fixed\", \"tests_green\": true, \"tests\": [\"build_deck.py\"], \"card_ids\": [\"C1-Q04\"], \"apply_command\": \"cd ~/dev/anki && out/pyenv/bin/python apply_highyield.py --repatch=C1-Q04\", \"summary\": \"Answer typo fixed via overrides.\"}\n```"}'
    ;;
  nofix)
    echo '{"type":"result","subtype":"error_max_turns","is_error":true,"num_turns":60,"result":"ran out"}'
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
    ("editor crashes", "Micro-Cloze", "deck"),
    ("something weird", "Basic", "app"),
])
def test_classify(note, notetype, expected):
    t = {"note": note, "reviewer": {"notetype": notetype}, "capture": None}
    kind, reason = classify(t, Config())
    assert kind == expected, reason


def test_classify_override_and_config_prefixes():
    t = {"note": "editor crash", "reviewer": {"notetype": "Genetics-Basic"}}
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
    assert fix["log_path"] == "fix.log" and fix["started"] and fix["finished"]
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
