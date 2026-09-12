# Anki+ macOS launcher

The Dock app for this fork. `Anki+.app` is a thin wrapper: its executable
(`anki-plus-launcher.sh`) sets PATH, cds to the repo checkout at
`~/dev/anki`, runs `./run` (incremental build + launch), and reports
failures via notification with details in `~/Library/Logs/AnkiDev.log`.

Contents:
- `anki-plus-icon.svg` — icon source (dark navy squircle, glowing blue
  brain with gyri/neural-network hemispheres, fanned A+ flashcards).
  Render at 1024px and rebuild the icns with `sips`/`iconutil` if edited.
- `AnkiPlus.icns` — compiled icon, all macOS sizes.
- `Info.plist` — bundle metadata (name "Anki+", id com.aidanjones.anki-dev).
- `anki-plus-launcher.sh` — the wrapper executable.
- `install.sh` — recreates `/Applications/Anki+.app` from these files.

This folder lives under `extra/`, which Anki's formatters and checks
ignore by convention.
