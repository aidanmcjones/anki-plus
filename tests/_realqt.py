"""Shared bootstrap for tests that need the REAL aqt + PyQt6 (not fakes).

`ensure_real_aqt(__file__)` returns True when `aqt` imports here. When it
does not, it re-runs the calling test under the Anki source checkout's
pyenv (ANKI_SRC, default ~/dev/anki) with pylib/qt/out on PYTHONPATH and
exits with that run's status. With no checkout it prints SKIP and exits 0.
"""

import os
import subprocess
import sys


def ensure_real_aqt(test_file: str) -> bool:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    os.environ.setdefault("QTWEBENGINE_CHROMIUM_FLAGS", "--disable-gpu")
    try:
        import aqt.qt  # noqa: F401
        import aqt.webview  # noqa: F401
        return True
    except Exception:
        pass
    if os.environ.get("BA_REALQT_CHILD"):
        print(f"SKIP {os.path.basename(test_file)}: aqt not importable")
        sys.exit(0)
    src = os.path.expanduser(os.environ.get("ANKI_SRC", "~/dev/anki"))
    py = os.path.join(src, "out", "pyenv", "bin", "python")
    if not os.path.exists(py):
        print(f"SKIP {os.path.basename(test_file)}: no Anki pyenv at {py}")
        sys.exit(0)
    env = dict(os.environ)
    env["BA_REALQT_CHILD"] = "1"
    env["PYTHONPATH"] = os.pathsep.join(
        os.path.join(src, p) for p in ("pylib", "qt", "out/pylib", "out/qt")
    )
    sys.exit(subprocess.call([py, os.path.abspath(test_file)], cwd=src, env=env))


def spin(ms: int) -> None:
    from aqt.qt import QEventLoop, QTimer

    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec()
