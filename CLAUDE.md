# Notes for Claude: NetWatch (CachyOS port)

## Where work happens
- This repo lives at `~/Workhub/netwatch` (a workflow project in `~/Workhub`; repo `yassermaina/netwatch`). Work only here, on branch `cachyos`.
- `main` is the original Fedora/Debian code: do not commit to it.

## Target
- Primary target: CachyOS (Arch, pacman, Hyprland + Noctalia). CachyOS first; Fedora/Debian support is not maintained on this branch. Prefer detection (`shutil.which`, XDG, D-Bus) over hard-coded paths where it is cheap.
- Stack: Python 3.10+, GTK4, libadwaita, SQLite, own StatusNotifierItem tray. Runtime packages: `python python-gobject python-cairo gtk4 libadwaita iputils networkmanager iw pipewire`. Tests: `python-pytest xorg-server-xvfb dbus`. Install helpers: `python-pip python-setuptools`.

## Rules
- Never push `main`, and never push, open PRs, release or bump versions without explicit approval. Do not push `cachyos` unless asked.
- Run `python -m pytest -q` before every commit (about 45 s; GUI scenarios run on a private Xvfb + D-Bus, never on the live compositor). Report failures honestly.
- Snapper snapshot before installing packages: `sudo snapper -c root create -d "<reason>"`.
- One small commit per logical change. Commit trailer: `Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>`.
- Never run as root and never use `setcap` for the app. Ping is checked, not changed: `net.ipv4.ping_group_range` / `getcap /usr/bin/ping`. Do not change sysctl without recording it in `~/CLAUDE.md` first.
- The real database `~/.local/share/netwatch/netwatch.db` is the owner's history: never delete or reset it. For tests use `--db` or `XDG_DATA_HOME`.
- Autostart is off by default; XDG autostart is ignored in this Hyprland session (see README).

## Install, run, stop
- User install: `make install PREFIX=$HOME/.local`; update: `netwatch-update --yes ~/Workhub/netwatch` (no sudo for `~/.local`; use `./update.sh --prefix ~/.local` if nothing is installed yet). Updating stops a running NetWatch and `--no-restart` leaves it stopped.
- Run: `netwatch` (window) or `netwatch --background`; `netwatch --diagnostics` is headless. Stop: `pkill -x netwatch` (it closes its session cleanly on SIGTERM).
- Tray check: `busctl --user get-property org.kde.StatusNotifierWatcher /StatusNotifierWatcher org.kde.StatusNotifierWatcher RegisteredStatusNotifierItems`.

## Testing notes
- `tests/test_gui.py` removes `WAYLAND_DISPLAY` and friends and forces X11 so the portals of the live desktop do not attach to the real compositor.
- `tests/test_update_command.py` runs a real install into a temporary prefix with `PYTHONNOUSERSITE=1`, a failing `sudo` stub and a `pgrep` stub: it must never touch the installed copy or a running NetWatch.
