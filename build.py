#!/usr/bin/env python3
"""Build a submittable .ankiaddon package.

An .ankiaddon is a zip of the add-on's files *at the root* (so __init__.py is
at the top level, not inside a folder). AnkiWeb supplies its own manifest, but
keeping name/conflicts in manifest.json is harmless and useful offline.

Usage:  python3 build.py   ->   dist/anki-design.ankiaddon
"""

import fnmatch
import os
import sys
import zipfile

ROOT = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(ROOT, "dist")
OUT = os.path.join(OUT_DIR, "anki-design.ankiaddon")

# A submittable .ankiaddon should be source + web assets only — nothing
# resembling a media library. If a future data directory slips past
# EXCLUDE_DIRS the way "user_files" once did, this catches it loudly
# instead of silently shipping a multi-hundred-MB package.
MAX_OUT_BYTES = 20 * 1024 * 1024

# Anything matching these (path-relative-to-root) is never shipped.
# "user_files" is the one that actually matters: it's where the curated
# nature-video library lives (hundreds of MB), and it's Anki's per-addon
# data directory — the shipped .ankiaddon must never contain it, both
# because of size and because AnkiWeb would then overwrite a user's own
# curated library on every update. THIS list is the only thing enforcing
# that; Anki itself has no special handling of a directory named
# "user_files" inside an .ankiaddon.
EXCLUDE_DIRS = {".git", "dist", "__pycache__", "scripts", ".context", "out", "docs", "user_files"}
EXCLUDE_FILES = {
    "meta.json",
    ".DS_Store",
    ".gitignore",
    ".devmode",
    "build.py",
    "Makefile",
    "CLAUDE.md",
    "SUBMISSION.md",
}
EXCLUDE_GLOBS = ["*.pyc"]


def _excluded(rel: str) -> bool:
    parts = rel.split(os.sep)
    if any(p in EXCLUDE_DIRS for p in parts):
        return True
    base = os.path.basename(rel)
    if base in EXCLUDE_FILES:
        return True
    return any(fnmatch.fnmatch(base, g) for g in EXCLUDE_GLOBS)


def main() -> None:
    os.makedirs(OUT_DIR, exist_ok=True)
    if os.path.exists(OUT):
        os.remove(OUT)

    shipped = []
    with zipfile.ZipFile(OUT, "w", zipfile.ZIP_DEFLATED) as z:
        for dirpath, dirnames, filenames in os.walk(ROOT):
            dirnames[:] = [
                d for d in dirnames
                if d not in EXCLUDE_DIRS and not d.startswith(".git")
            ]
            for fn in filenames:
                full = os.path.join(dirpath, fn)
                rel = os.path.relpath(full, ROOT)
                if _excluded(rel):
                    continue
                z.write(full, rel)
                shipped.append(rel)

    print(f"wrote {OUT}")
    for f in sorted(shipped):
        print(f"  {f}")

    size = os.path.getsize(OUT)
    if size > MAX_OUT_BYTES:
        # Remove the oversized artifact before exiting — a failed build
        # shouldn't leave anything behind at OUT for a subsequent step
        # (packaging, CI upload, a human skimming dist/) to mistake for a
        # good build just because the path exists.
        try:
            os.remove(OUT)
        except OSError:
            pass
        print(
            f"\nERROR: {OUT} is {size / 1024 / 1024:.1f}MB, over the "
            f"{MAX_OUT_BYTES / 1024 / 1024:.0f}MB sanity limit for a "
            f"submittable add-on. Something that shouldn't ship (a data "
            f"directory like user_files/, a media dump, etc.) is almost "
            f"certainly being walked — check the file list above and add "
            f"it to EXCLUDE_DIRS.",
            file=sys.stderr,
        )
        sys.exit(1)


if __name__ == "__main__":
    main()
