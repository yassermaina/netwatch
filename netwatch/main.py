"""Command line entry point."""

from __future__ import annotations

import argparse
import logging
import logging.handlers
import sys

from netwatch import APP_NAME, __version__
from netwatch.paths import database_path, state_dir


def setup_logging(debug: bool) -> None:
    level = logging.DEBUG if debug else logging.INFO
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stderr)]
    try:
        state_dir().mkdir(parents=True, exist_ok=True)
        handlers.append(logging.handlers.RotatingFileHandler(state_dir() / "netwatch.log", maxBytes=1_000_000,
                                                             backupCount=3, encoding="utf-8"))
    except OSError:
        pass
    handlers[0].setLevel(level if debug else logging.WARNING)
    logging.basicConfig(level=level, handlers=handlers,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def print_diagnostics(db_override: str | None) -> int:
    """Headless diagnostics (no GTK required)."""
    from netwatch.monitor.adapters import build_adapters
    from netwatch.monitor.netstate import LocalNetworkProbe
    from netwatch.monitor.process import ProcessRunner
    from netwatch.settings import DEFAULT_TARGETS
    from netwatch.storage.db import Database
    from netwatch.storage.repository import Repository

    runner = ProcessRunner()
    adapters = build_adapters(runner)
    probe = LocalNetworkProbe(runner)
    print(f"{APP_NAME} {__version__}")
    print(f"Python {sys.version.split()[0]} ({sys.executable})")
    try:
        import gi

        gi.require_version("Gtk", "4.0")
        gi.require_version("Adw", "1")
        from gi.repository import Adw, Gtk

        print(f"GTK {Gtk.get_major_version()}.{Gtk.get_minor_version()}, "
              f"Libadwaita {Adw.get_major_version()}.{Adw.get_minor_version()}, PyGObject {gi.__version__}")
    except (ImportError, ValueError) as exc:
        print(f"GTK4/Libadwaita: NOT AVAILABLE ({exc})")
    print(f"ping: {adapters['icmp'].availability()[1]}")
    print(f"Local network backend: {probe.describe_backend()}")
    status = probe.probe()
    print(f"Local network: connected={status.connected} {status.describe()} ({status.detail})")
    path = database_path(db_override)
    print(f"Database: {path}")
    db = Database(path)
    db.migrate()
    repo = Repository(db)
    repo.ensure_default_targets(DEFAULT_TARGETS)
    for t in repo.list_targets(enabled_only=True):
        r = adapters[t.check_type].check(t, 2)
        result = f"{r.latency_ms:.1f} ms" if r.success else f"FAILED ({r.error.label}: {r.detail})"
        print(f"  {t.check_type:5} {t.address:25} {result}")
    db.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv if argv is None else argv)
    parser = argparse.ArgumentParser(prog="netwatch", description=f"{APP_NAME} network connectivity monitor")
    parser.add_argument("--background", action="store_true", help="start hidden and keep monitoring")
    parser.add_argument("--db", metavar="PATH", help="use an alternative database file")
    parser.add_argument("--diagnostics", action="store_true", help="print diagnostics and run one check, no GUI")
    parser.add_argument("--debug", action="store_true", help="verbose logging to stderr")
    parser.add_argument("--version", action="version", version=f"{APP_NAME} {__version__}")
    args, _unknown = parser.parse_known_args(argv[1:])
    setup_logging(args.debug)
    if args.diagnostics:
        return print_diagnostics(args.db)
    try:
        import gi

        gi.require_version("Gtk", "4.0")
        gi.require_version("Adw", "1")
        gi.require_foreign("cairo")  # charts and timelines are drawn with Cairo
        from gi.repository import Adw  # noqa: F401
    except (ImportError, ValueError) as exc:
        sys.stderr.write(
            f"{APP_NAME} needs GTK 4, Libadwaita and PyGObject with Cairo support ({exc}).\n"
            "  Fedora:        sudo dnf install python3-gobject python3-cairo gtk4 libadwaita\n"
            "  Debian/Ubuntu: sudo apt install python3-gi python3-gi-cairo gir1.2-gtk-4.0 gir1.2-adw-1\n"
        )
        return 1
    from netwatch.ui.application import NetWatchApplication

    app = NetWatchApplication(database_path(args.db), background_mode=args.background)
    return app.run([argv[0]] + (["--background"] if args.background else []))


if __name__ == "__main__":
    raise SystemExit(main())
