#!/usr/bin/env python3
"""Run a live test inside the REAL Anki app (offscreen, throwaway profile).

    cd ~/dev/anki && out/pyenv/bin/python \
        ~/dev/anki-design/tests/live/run_in_app.py tests/live/test_smoke.py

What it does, in order:
  1. makes a fresh temp base folder with prefs21.db and one profile
     ("User 1") whose collection holds a fixture deck tree
     (Parent::A, Parent::B, Parent::C, Solo) and a few new cards;
  2. builds <base>/addons21/anki-design as a folder of symlinks to this
     add-on's code (config/meta.json is NOT linked, so a test that writes
     add-on config never touches the checkout or the user's settings);
  3. starts the app the way the Anki+ launcher does (tools/run.py in the
     fork) with QT_QPA_PLATFORM=offscreen, `-b <base>`, a private
     single-instance key (so it never talks to the user's running Anki+),
     no remote-debugging port and no fixed API port;
  4. the add-on's live_test.py runs the script's run(t) in the app, writes
     the result JSON, and closes the app;
  5. prints PASS/FAIL per check, exits 0 only if every check passed, and
     always deletes the temp base (unless --keep).

Must run under the fork's pyenv python (it imports anki to build the
fixture). Exit codes: 0 pass, 1 a check failed or the test errored,
2 timeout / no result, 3 usage error.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
ADDON_DEFAULT = os.path.dirname(os.path.dirname(HERE))
ANKI_SRC_DEFAULT = os.environ.get("ANKI_SRC") or os.path.expanduser("~/dev/anki")
ADDON_NAME = "anki-design"
PROFILE = "User 1"
FIXTURE_DECKS = ["Parent", "Parent::A", "Parent::B", "Parent::C", "Solo"]

# Never linked into the throwaway add-on folder.
SKIP = {"meta.json", "tests", "out", "__pycache__", "user_files", "docs"}


def _fork_path(anki_src: str) -> None:
    for p in ("pylib", "out/pylib", "qt", "out/qt"):
        full = os.path.join(anki_src, p)
        if full not in sys.path:
            sys.path.insert(0, full)


def make_fixture(base: str, anki_src: str) -> None:
    _fork_path(anki_src)
    from anki.collection import Collection

    prof_dir = os.path.join(base, PROFILE)
    os.makedirs(prof_dir)
    col = Collection(os.path.join(prof_dir, "collection.anki2"))
    try:
        ids = {name: col.decks.id(name) for name in FIXTURE_DECKS}
        model = col.models.by_name("Basic")
        for i, deck in enumerate(["Parent::A", "Parent::B", "Parent::C", "Solo"]):
            for j in range(2):
                note = col.new_note(model)
                note["Front"] = f"{deck} front {j}"
                note["Back"] = f"{deck} back {j}"
                col.add_note(note, ids[deck])
        col.decks.select(ids["Parent"])
    finally:
        col.close()

    # prefs21.db the way ProfileManager writes it, with the first-run
    # language dialog already answered.
    from aqt.profiles import ProfileManager

    pm = ProfileManager(base)
    pm.setupMeta()
    pm.meta["firstRun"] = False
    pm.meta["defaultLang"] = "en_US"
    # No network update prompts or add-on update checks mid-test.
    pm.meta["check_for_updates"] = False
    pm.meta["last_addon_update_check"] = int(time.time())
    pm.create(PROFILE)
    pm.db.execute(
        "update profiles set data = ? where name = '_global'", pm._pickle(pm.meta)
    )
    pm.db.commit()
    pm.db.close()


def link_addon(base: str, addon_dir: str) -> str:
    dest = os.path.join(base, "addons21", ADDON_NAME)
    os.makedirs(dest)
    for entry in sorted(os.listdir(addon_dir)):
        if entry.startswith(".") or entry in SKIP:
            continue
        os.symlink(os.path.join(addon_dir, entry), os.path.join(dest, entry))
    return dest


def needs_display(script: str) -> bool:
    """A script that sets `NEEDS_DISPLAY = True` at module level runs on the
    real display (QT_QPA_PLATFORM=cocoa), and only when ANKI_LIVE_DISPLAY=1."""
    try:
        with open(script) as fh:
            return any(line.strip() == "NEEDS_DISPLAY = True" for line in fh)
    except OSError:
        return False


def child_env(script: str, result: str, base: str) -> dict:
    env = dict(os.environ)
    for k in (
        "QTWEBENGINE_REMOTE_DEBUGGING",
        "QTWEBENGINE_CHROMIUM_FLAGS",
        "ANKI_API_PORT",
        "ANKI_BASE",
        "ANKIDEV",
        "AD_SHOWCASE",
    ):
        env.pop(k, None)
    display = needs_display(script) and os.environ.get("ANKI_LIVE_DISPLAY") == "1"
    env.update(
        QT_QPA_PLATFORM="cocoa" if display else "offscreen",
        ANKI_SINGLE_INSTANCE_KEY=f"anki-live-{uuid.uuid4().hex}",
        ANKI_DESIGN_LIVE_TEST=script,
        ANKI_DESIGN_LIVE_RESULT=result,
        PYTHONPYCACHEPREFIX=os.path.join(base, ".pycache"),
        PYTHONWARNINGS="ignore",
        SKIP_RUN="",
    )
    return env


def report(result: dict, name: str) -> int:
    failed = 0
    for c in result.get("checks", []):
        tag = "PASS" if c.get("ok") else "FAIL"
        if not c.get("ok"):
            failed += 1
        line = f"{tag} {name}: {c.get('name')}"
        if c.get("detail"):
            line += f" -- {c['detail']}"
        print(line)
    for n in result.get("notes", []):
        print(f"NOTE {name}: {n}")
    if result.get("error"):
        failed += 1
        print(f"FAIL {name}: test raised\n{result['error']}")
    if not result.get("checks") and not result.get("error"):
        failed += 1
        print(f"FAIL {name}: the test recorded no checks")
    return failed


def run_one(script: str, args) -> int:
    name = os.path.basename(script)
    if needs_display(script) and os.environ.get("ANKI_LIVE_DISPLAY") != "1":
        print(f"SKIP {name}: real-display test, set ANKI_LIVE_DISPLAY=1 to run it")
        return 0
    base = tempfile.mkdtemp(prefix="anki-live-")
    result_path = os.path.join(base, "result.json")
    log_path = os.path.join(base, "app.log")
    proc = None
    t0 = time.monotonic()
    try:
        make_fixture(base, args.anki_src)
        link_addon(base, args.addon_dir)
        cmd = [sys.executable, "tools/run.py", "-b", base]
        with open(log_path, "wb") as log:
            proc = subprocess.Popen(
                cmd,
                cwd=args.anki_src,
                env=child_env(script, result_path, base),
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            deadline = t0 + args.timeout
            while proc.poll() is None and time.monotonic() < deadline:
                time.sleep(0.25)
            timed_out = proc.poll() is None
            if timed_out:
                _stop(proc)
        elapsed = time.monotonic() - t0
        result = None
        if os.path.exists(result_path):
            with open(result_path) as fh:
                result = json.load(fh)
        if result is None:
            why = f"timed out after {args.timeout}s" if timed_out else (
                f"app exited with code {proc.returncode} before writing a result"
            )
            print(f"FAIL {name}: {why}")
            _tail(log_path)
            return 2
        failed = report(result, name)
        verdict = "FAILED" if failed else "PASSED"
        print(f"{verdict} {name}: {len(result.get('checks', []))} checks, "
              f"{failed} failed, app runtime {elapsed:.1f}s")
        if failed and args.verbose:
            _tail(log_path)
        return 1 if failed else 0
    finally:
        if proc is not None and proc.poll() is None:
            _stop(proc)
        if args.keep:
            print(f"kept base folder: {base}")
        else:
            shutil.rmtree(base, ignore_errors=True)


def _stop(proc: subprocess.Popen) -> None:
    """Stop OUR child app (its own session), never anything else."""
    try:
        os.killpg(proc.pid, signal.SIGTERM)
        proc.wait(timeout=10)
    except Exception:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait(timeout=5)
        except Exception:
            pass


def _tail(path: str, n: int = 40) -> None:
    try:
        with open(path, errors="replace") as fh:
            lines = fh.read().splitlines()[-n:]
    except Exception:
        return
    print("---- app log (tail) ----")
    for line in lines:
        print(line)
    print("------------------------")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("scripts", nargs="+", help="live test script(s) with run(t)")
    ap.add_argument("--addon-dir", default=ADDON_DEFAULT,
                    help="add-on source to load (default: this checkout)")
    ap.add_argument("--anki-src", default=ANKI_SRC_DEFAULT,
                    help="the Anki fork (default ~/dev/anki or $ANKI_SRC)")
    ap.add_argument("--timeout", type=float, default=120.0,
                    help="seconds per test before it counts as a failure")
    ap.add_argument("--keep", action="store_true", help="keep the temp base")
    ap.add_argument("-v", "--verbose", action="store_true",
                    help="print the app log tail on failure")
    args = ap.parse_args(argv)
    args.addon_dir = os.path.abspath(args.addon_dir)
    worst = 0
    for s in args.scripts:
        path = os.path.abspath(s)
        if not os.path.isfile(path):
            path2 = os.path.join(args.addon_dir, s)
            if os.path.isfile(path2):
                path = path2
            else:
                print(f"FAIL {s}: no such file")
                worst = max(worst, 3)
                continue
        worst = max(worst, run_one(path, args))
    return worst


if __name__ == "__main__":
    sys.exit(main())
