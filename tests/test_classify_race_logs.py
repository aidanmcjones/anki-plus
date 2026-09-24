"""Ticket 20260924-131713 regressions: off-card classification, wrong-kind
flip, human/watcher race, per-attempt evidence, Terminal delegation.

Imports ankifix/ankibug BEFORE test_ankifix (which prepends this checkout to
sys.path), so `PYTHONPATH=<other checkout> pytest tests/test_classify_race_logs.py`
really exercises that checkout's code: that is how the before/after run
against main is made.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path

import pytest

from ankibug import cli as bug_cli  # noqa: E402  (first: honor PYTHONPATH)
from ankibug import schema, store  # noqa: E402
from ankifix import cli, runner  # noqa: E402
from ankifix.classify import classify  # noqa: E402
from ankifix.config import Config  # noqa: E402
from ankifix.tickets import load_ticket, save_ticket  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_ankifix import FAKE_CLAUDE, FAKE_OSASCRIPT, env, make_ticket  # noqa: E402,F401

# the checkout under test (for the Terminal child's PYTHONPATH)
SRC = str(Path(runner.__file__).resolve().parents[1])

RESTUDY_NOTE = (
    "the restudy deck should have a title denoting what the restudy deck contains, "
    "for example: restudy: courseb hi-yield"
)
RESTUDY_REVIEWER = {"state": "answer", "card_id": None, "note_id": None, "notetype": None,
                    "deck": None, "template_ord": None}

# extra fake-claude modes: wrongkind (deck fixer refuses as an app bug),
# FAKE_DURING (a shell command run mid-run, e.g. a human's `ankibug set`),
# FAKE_DECK_SLEEP (the deck fixer takes a while)
EXTRA_MODES = r"""  wrongkind)
    echo '{"type":"result","subtype":"success","is_error":false,"num_turns":2,"result":"```ankifix-result\n{\"status\": \"wrong-kind\", \"tests_green\": false, \"tests\": [], \"reason\": \"the restudy deck title is set by the add-on\", \"summary\": \"app bug, not card content\"}\n```"}'
    ;;
  nofix)"""
PRELUDE = r"""[ -n "$FAKE_DURING" ] && bash -c "$FAKE_DURING"
[ -f build_deck.py ] && [ -n "$FAKE_DECK_SLEEP" ] && sleep "$FAKE_DECK_SLEEP"
case "$mode" in"""


@pytest.fixture
def xenv(env):
    fake = env["tmp"] / "bin" / "claude"
    script = FAKE_CLAUDE.replace("  nofix)", EXTRA_MODES, 1).replace('case "$mode" in', PRELUDE, 1)
    fake.write_text(script)
    return env


def _silent(*a):
    return None


# ------------------------------------------------------------ A: classify
def _restudy_ticket():
    return {"note": RESTUDY_NOTE, "reviewer": dict(RESTUDY_REVIEWER),
            "capture": {"console_errors": []}}


def test_restudy_ticket_classifies_app():
    kind, reason = classify(_restudy_ticket(), Config())
    assert kind == "app", reason


def test_restudy_ticket_capture_time_kind_is_app():
    # ankibug stamps kind at filing time with the same classifier; ankifix
    # then treats that kind as authoritative, so this is what routed it
    t = schema.new_ticket(RESTUDY_NOTE, source="hotkey", kind="unknown", reviewer=RESTUDY_REVIEWER)
    assert bug_cli._classify_kind(t) == "app"


@pytest.mark.parametrize("note", [
    "the deck name is wrong", "the deck shows too many cards", "rename this deck",
    "restudy deck is empty", "the filtered deck button does nothing",
])
def test_off_card_deck_word_alone_is_app(note):
    t = {"note": note, "reviewer": dict(RESTUDY_REVIEWER), "capture": {"console_errors": []}}
    kind, reason = classify(t, Config())
    assert kind == "app", reason


@pytest.mark.parametrize("note,reviewer", [
    ("the answer on this card is wrong",
     {"state": "answer", "card_id": 17, "notetype": "CourseB-E1"}),
    ("typo in the definition", {"state": "question", "card_id": 17, "notetype": "Micro-Cloze"}),
    ("the image on this card is cut off", {"state": "answer", "card_id": 17, "notetype": "CourseB-E1"}),
    ("this deck title on the card is wrong", {"state": "answer", "card_id": 17, "notetype": "CourseB-E1"}),
    # off-card but the note names card content explicitly: still deck
    ("the answer on card C2-Q05 is wrong", dict(RESTUDY_REVIEWER)),
    ("card says the wrong answer for pKa", dict(RESTUDY_REVIEWER)),
])
def test_card_content_tickets_still_classify_deck(note, reviewer):
    t = {"note": note, "reviewer": reviewer, "capture": {"console_errors": []}}
    kind, reason = classify(t, Config())
    assert kind == "deck", reason


# -------------------------------------------------------- A: wrong-kind
def _deck_ticket(e, **over):
    return make_ticket(e["cfg"], note="the answer on card C1-Q04 is wrong", kind="deck",
                       deck_build={"course_dir": str(e["course"]), "build_marker": None}, **over)


def test_wrong_kind_deck_result_flips_to_app_new_exactly_once(xenv, monkeypatch):
    cfg = xenv["cfg"]
    t = _deck_ticket(xenv)
    monkeypatch.setenv("FAKE_MODE", "wrongkind")
    seen = []
    out = runner.run_ticket(t["id"], cfg, echo=seen.append, retry_on_error=True)
    disk = load_ticket(cfg.tickets_dir, t["id"])
    assert disk["status"] == "new" and disk["kind"] == "app", disk["fix"]["summary"]
    assert disk["fix"]["kind_flip"]["from"] == "deck"
    assert "the restudy deck title is set by the add-on" in disk["fix"]["kind_flip"]["reason"]
    assert out["status"] == "new"
    assert any("flipped to kind=app" in s for s in seen)

    # ping-pong guard: forced back to deck, a second wrong-kind does not flip
    disk["kind"] = "deck"
    save_ticket(cfg.tickets_dir, disk)
    out2 = runner.run_ticket(t["id"], cfg, echo=_silent, retry_on_error=True)
    assert out2["status"] == "failed" and out2["kind"] == "deck"
    assert "not flipping again" in out2["fix"]["summary"]


def test_flipped_ticket_is_run_by_the_app_fixer_next_poll(xenv, monkeypatch):
    cfg = xenv["cfg"]
    t = _deck_ticket(xenv)
    monkeypatch.setenv("FAKE_MODE", "wrongkind")
    runner.watch(cfg, interval=0, once=True, echo=_silent)
    assert load_ticket(cfg.tickets_dir, t["id"])["status"] == "new"
    monkeypatch.setenv("FAKE_MODE", "fix")
    runner.watch(cfg, interval=0, once=True, echo=_silent)
    done = load_ticket(cfg.tickets_dir, t["id"])
    assert done["kind"] == "app" and done["status"] == "fixed", done["fix"]["summary"]
    assert done["fix"]["branch"] == cfg.branch_name(t["id"])


def test_app_fixer_wrong_kind_never_flips_back(xenv, monkeypatch):
    cfg = xenv["cfg"]
    t = make_ticket(cfg, kind="app")
    monkeypatch.setenv("FAKE_MODE", "wrongkind")
    out = runner.run_ticket(t["id"], cfg, echo=_silent)
    assert out["status"] == "failed" and out["kind"] == "app"


# ----------------------------------------------------------- B: the race
def test_recover_stale_leaves_human_set_fixing_alone(xenv):
    cfg = xenv["cfg"]
    # the incident: a finished (failed) watcher run, then a human
    # `ankibug set <id> kind=app status=fixing` while they work on it
    fix = runner.new_fix("2026-09-24T13:20:00-05:00")
    fix.update(pid=999999, attempts=1, finished="2026-09-24T13:25:00-05:00",
               summary="deck fixer: this is an app bug")
    t = make_ticket(cfg, status="failed", kind="deck", fix=fix)
    bug_root = str(cfg.tickets_dir)
    store.set_field(t["id"], "kind", "app", root=bug_root)
    store.set_field(t["id"], "status", "fixing", root=bug_root)
    assert runner.recover_stale(cfg, echo=_silent) == []
    assert load_ticket(cfg.tickets_dir, t["id"])["status"] == "fixing"
    # a human `fixing` on a ticket no run ever touched is left alone too
    t2 = make_ticket(cfg, tid="20260924-140000-never-run", status="fixing", fix=None)
    assert runner.recover_stale(cfg, echo=_silent) == []
    assert load_ticket(cfg.tickets_dir, t2["id"])["status"] == "fixing"


def test_recover_stale_still_requeues_an_orphaned_watcher_run(xenv):
    cfg = xenv["cfg"]
    fix = runner.new_fix("2026-09-24T13:20:00-05:00")
    fix.update(pid=999999, attempts=1, owner="watcher")
    t = make_ticket(cfg, status="fixing", fix=fix)
    assert runner.recover_stale(cfg, echo=_silent) == [t["id"]]
    assert load_ticket(cfg.tickets_dir, t["id"])["status"] == "new"


HUMAN_SETS_WONTFIX = (
    "{py} -c \"import json,sys; p=sys.argv[1]; t=json.load(open(p)); t['status']='wontfix'; "
    "json.dump(t, open(p,'w'))\" {path}"
)


def test_human_status_change_during_a_run_is_preserved(xenv, monkeypatch):
    cfg = xenv["cfg"]
    cfg.auto_apply = True
    t = make_ticket(cfg)
    tj = cfg.tickets_dir / t["id"] / "ticket.json"
    monkeypatch.setenv("FAKE_MODE", "fix")
    monkeypatch.setenv("FAKE_DURING", HUMAN_SETS_WONTFIX.format(py=sys.executable, path=tj))
    applied = []
    from ankifix import apply as apply_mod

    monkeypatch.setattr(apply_mod, "apply_ticket", lambda tid, c, echo=None: applied.append(tid))
    runner.watch(cfg, interval=0, once=True, echo=_silent)
    disk = load_ticket(cfg.tickets_dir, t["id"])
    assert disk["status"] == "wontfix"
    assert disk["fix"]["result_ignored"]["status"] == "fixed"
    assert disk["fix"]["finished"]  # not left looking like an orphaned run
    assert applied == []  # never auto-applied
    assert runner.recover_stale(cfg, echo=_silent) == []


def _point_store_at(monkeypatch, root):
    """ankibug's CLI has no root option: rebind every store function's
    default root (works for any version of store.py)."""
    for name in dir(store):
        fn = getattr(store, name)
        if callable(fn) and getattr(fn, "__defaults__", None) and store.DEFAULT_ROOT in fn.__defaults__:
            monkeypatch.setattr(fn, "__defaults__", tuple(
                root if d == store.DEFAULT_ROOT else d for d in fn.__defaults__))


@pytest.mark.parametrize("pairs", [
    ["kind=app", "status=in_progress"],  # the incident's exact command
    ["kind=app", "oops"],
])
def test_ankibug_set_with_one_invalid_pair_writes_nothing(tmp_path, monkeypatch, capsys, pairs):
    root = str(tmp_path)
    _point_store_at(monkeypatch, root)
    t = schema.new_ticket("the restudy deck should have a title", kind="deck")
    store.write_ticket(t, root=root)
    before = Path(store.ticket_json_path(t["id"], root)).read_text()
    assert bug_cli.main(["set", t["id"], *pairs]) == 1
    assert Path(store.ticket_json_path(t["id"], root)).read_text() == before
    assert "nothing written" in capsys.readouterr().err


def test_ankibug_set_valid_pairs_all_land_with_manual_marker(tmp_path, monkeypatch):
    root = str(tmp_path)
    _point_store_at(monkeypatch, root)
    t = schema.new_ticket("x", kind="deck")
    store.write_ticket(t, root=root)
    assert bug_cli.main(["set", t["id"], "kind=app", "status=fixing"]) == 0
    got = store.read_ticket(t["id"], root=root)
    assert got["kind"] == "app" and got["status"] == "fixing"
    assert got["manual_set"]["fields"] == ["kind", "status"]


# ------------------------------------------------------------ C: evidence
def test_attempt_logs_are_not_overwritten(xenv, monkeypatch):
    cfg = xenv["cfg"]
    t = make_ticket(cfg)
    tdir = cfg.tickets_dir / t["id"]
    (tdir / "fix.log").write_text("LEGACY ATTEMPT\n")
    monkeypatch.setenv("FAKE_MODE", "nofix")
    runner.run_ticket(t["id"], cfg, echo=_silent)
    first = load_ticket(cfg.tickets_dir, t["id"])["fix"]["log_path"]
    monkeypatch.setenv("FAKE_MODE", "fix")
    runner.run_ticket(t["id"], cfg, echo=_silent, force=True)
    out = load_ticket(cfg.tickets_dir, t["id"])
    assert first == "fix.1.log" and out["fix"]["log_path"] == "fix.2.log"
    assert "error_max_turns" in (tdir / "fix.1.log").read_text()
    assert "ankifix-result" in (tdir / "fix.2.log").read_text()
    assert (tdir / "fix.0.log").read_text() == "LEGACY ATTEMPT\n"
    assert (tdir / "fix.log").resolve().name == "fix.2.log"


def _tcc(xenv, monkeypatch):
    cfg = xenv["cfg"]
    bindir = xenv["tmp"] / "bin"
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
    monkeypatch.setenv("ANKIFIX_TICKETS_DIR", str(cfg.tickets_dir))
    monkeypatch.setenv("ANKIFIX_APP_REPO", str(cfg.app_repo))
    monkeypatch.setenv("ANKIFIX_DECK_BUILD_DIR", str(xenv["build"]))
    monkeypatch.setenv("PYTHONPATH", SRC)
    monkeypatch.setattr(runner, "probe_files_access",
                        lambda d: PermissionError(1, "Operation not permitted", str(d / "build_out.json")))
    return cfg


def test_terminal_child_output_lands_in_terminal_log(xenv, monkeypatch):
    cfg = _tcc(xenv, monkeypatch)
    t = _deck_ticket(xenv)
    seen = []
    out = runner.run_ticket(t["id"], cfg, echo=seen.append)
    assert out["status"] == "fixed"
    tlog = cfg.tickets_dir / t["id"] / "terminal.log"
    assert tlog.is_file(), "no terminal.log"
    text = tlog.read_text()
    assert "--once-from-terminal" in text
    assert f"ankifix: {t['id']} -> fixed" in text  # the child's own output
    line = next(s for s in seen if "(Terminal run" in s)
    assert "Answer typo fixed via overrides" in line  # the child's summary


def test_watch_log_lines_are_timestamped(tmp_path, monkeypatch, capsys):
    conf = tmp_path / "config.json"
    conf.write_text(json.dumps({
        "tickets_dir": str(tmp_path / "T"), "notify": False, "auto_apply": False,
        "auto_restart_app": False, "doctor_interval_s": 0,
    }))
    monkeypatch.delenv("ANKIFIX_TICKETS_DIR", raising=False)
    assert cli.main(["--watch", "--once", "--interval", "0", "--config", str(conf)]) == 0
    lines = [l for l in capsys.readouterr().out.splitlines() if l]
    assert lines and all(re.match(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d", l) for l in lines), lines


# ------------------------------------------------------- D: lock not held
def test_watch_does_not_hold_the_lock_while_terminal_child_runs(xenv, monkeypatch):
    cfg = _tcc(xenv, monkeypatch)
    monkeypatch.setenv("FAKE_DECK_SLEEP", "4")
    deck = _deck_ticket(xenv)  # created first: oldest in the queue
    app = make_ticket(cfg, tid="20260924-150000-editor-crash", note="editor crashes", kind="app")
    seen = []
    t0 = time.time()
    runner.watch(cfg, interval=0, once=True, echo=seen.append)
    assert time.time() - t0 < 4, "watch waited for the Terminal child"
    d = load_ticket(cfg.tickets_dir, deck["id"])
    assert d["status"] == "fixing" and d["fix"]["delegated"] == "terminal"
    assert load_ticket(cfg.tickets_dir, app["id"])["status"] == "fixed"  # not blocked
    with runner.ticket_lock(cfg.tickets_dir):  # free while the child runs
        pass
    # a later poll settles the delegation and logs the child's result
    deadline = time.time() + 30
    while time.time() < deadline:
        runner.watch(cfg, interval=0, once=True, echo=seen.append)
        if load_ticket(cfg.tickets_dir, deck["id"])["status"] != "fixing":
            break
        time.sleep(0.3)
    runner.watch(cfg, interval=0, once=True, echo=seen.append)  # the poll that reports it
    assert load_ticket(cfg.tickets_dir, deck["id"])["status"] == "fixed"
    assert any("(Terminal run" in s and "Answer typo fixed" in s for s in seen), seen
