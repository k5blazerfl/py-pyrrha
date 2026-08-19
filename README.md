# py-pyrrha

[![License: GPL v3](https://img.shields.io/badge/License-GPLv3-blue.svg)](LICENSE)

> **Legacy build.** This is the original PyGObject/Pithos-derived Pyrrha, kept
> functional and renamed `py-pyrrha`. It is superseded by the ground-up,
> HeDE-native **Pyrrha** rewrite (GPL-2.0-or-later core; Pandora streaming
> isolated as a separate GPLv3 plugin). This tree stays GPLv3 as a derivative
> of Pithos and is no longer the primary line of development.

A skinnable Qt (PySide6) desktop audio player with classic Winamp 2.x fidelity —
playing both Pandora radio and your local music library.

Pyrrha began as a GTK→Qt port of [Pithos](https://pithos.github.io) and still
rides on its battle-tested internals: the Pandora API client, the GStreamer
playback pipeline (ReplayGain/limiter/equalizer), GSettings and libsecret. Those
GLib-based subsystems are driven from the Qt event loop by pumping the GLib main
context from a `QTimer`. Everything above that core — the local-file support and
the Winamp-faithful skin engine — is new to Pyrrha and has no analog in Pithos.

Three interchangeable UIs share one player:

- **Pandora** — a modern Qt window (the Pithos-descended interface).
- **Pyrrha** — a modern skinned shell.
- **WinAMP 2.x** — a pixel-faithful classic mode with a full `.wsz` skin engine:
  shaped skins (`region.txt`), bitmap fonts, per-region cursors, a 10-band EQ
  window, a playlist editor, windowshade, and a multi-monitor tear-off overlay.

## Status

- Two sources, one player: Pandora streaming **and** local-file playback
  (drag-and-drop, folder scan, GStreamer tag/art discovery, a managed queue).
- Full Pandora player: login, stations, gapless playback, ratings, album art, preferences.
- All **10** of Pithos' plugins ported: MPRIS (with media keys, hide-on-close and
  live playlists), media keys, notifications, system-tray icon, 10-band equalizer,
  Last.fm scrobbling, screensaver inhibit, screensaver pause, ReplayGain volume
  normalization, and journald logging — with an in-app plugin manager.
- Standalone: its own `io.github.k5blazerfl.Pyrrha` GSettings/keyring/desktop
  identity, a one-time migration from an existing Pithos config, and a vendored
  backend (no dependency on the Pithos package at runtime).

## Quick start

```sh
python3 -m venv --system-site-packages .venv-qt
.venv-qt/bin/pip install -e .   # installs Pyrrha + PySide6, creates the `pyrrha` command
./install.sh                    # installs the GSettings schema, .desktop file & icon (user-local)
.venv-qt/bin/pyrrha             # run (also: ./pyrrha-run, or python -m pyrrha)
.venv-qt/bin/pyrrha --test      # offline mock, no account needed
```

Requires PyGObject with GStreamer 1.x (base + good plugins for AAC) and libsecret
introspection from the system (hence the `--system-site-packages` venv). See
[`pyrrha/README.md`](pyrrha/README.md) for architecture and full details.

## License

GPL-3.0. Pyrrha is a derivative work of Pithos; original copyright and
attribution notices are preserved throughout the source.
