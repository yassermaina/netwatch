"""About & Diagnostics: versions, integrations, data location and a live connectivity test."""

from __future__ import annotations

import platform
import sqlite3
import sys
from typing import TYPE_CHECKING

import gi
from gi.repository import Adw, Gdk, GLib, Gtk

from netwatch import APP_ID, APP_NAME, __version__
from netwatch.autostart import autostart_enabled
from netwatch.paths import state_dir
from netwatch.ui.background import run_async
from netwatch.ui.widgets.common import label

if TYPE_CHECKING:
    from netwatch.ui.application import NetWatchApplication


def _row(title: str, value: str, ok: bool | None = None) -> Adw.ActionRow:
    row = Adw.ActionRow(title=title)
    if ok is not None:
        icon = Gtk.Image.new_from_icon_name("emblem-ok-symbolic" if ok else "dialog-warning-symbolic")
        icon.add_css_class("success" if ok else "warning")
        icon.set_tooltip_text("Available" if ok else "Unavailable")
        row.add_prefix(icon)
    v = label(value, ("dim-label",), xalign=1.0, wrap=True, selectable=True)
    v.set_max_width_chars(50)
    row.add_suffix(v)
    return row


class AboutPage(Gtk.Box):
    title = "About & Diagnostics"
    icon = "help-about-symbolic"

    def __init__(self, app: "NetWatchApplication"):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.app = app
        self.page = Adw.PreferencesPage(vexpand=True)
        self.append(self.page)
        self.groups: list[Adw.PreferencesGroup] = []
        self.report_text = ""

    def set_page_visible(self, visible: bool) -> None:
        if visible:
            self.rebuild()

    def _group(self, title: str, description: str = "") -> Adw.PreferencesGroup:
        g = Adw.PreferencesGroup(title=title, description=description)
        self.page.add(g)
        self.groups.append(g)
        return g

    def diagnostics(self) -> list[tuple[str, list[tuple[str, str, bool | None]]]]:
        app = self.app
        icmp = app.adapters["icmp"]
        ping_ok, ping_info = icmp.availability()
        try:
            backend = app.probe.describe_backend()
        except Exception as exc:  # pragma: no cover - defensive
            backend = f"error: {exc}"
        snap = app.engine.snapshot
        local = snap.local
        counts = app.repo.counts()
        try:
            size = app.db.path.stat().st_size
            for suffix in ("-wal", "-shm"):
                p = app.db.path.with_name(app.db.path.name + suffix)
                if p.exists():
                    size += p.stat().st_size
        except OSError:
            size = 0
        tray = app.tray
        return [
            ("Application", [
                ("Version", f"{APP_NAME} {__version__}", None),
                ("Application ID", APP_ID, None),
                ("Monitoring", f"{snap.lifecycle.label} · {snap.state.label}", None),
                ("Session", str(snap.session_id) if snap.session_id else "none", None),
            ]),
            ("Runtime", [
                ("Python", platform.python_version(), None),
                ("GTK", f"{Gtk.get_major_version()}.{Gtk.get_minor_version()}.{Gtk.get_micro_version()}", None),
                ("Libadwaita", f"{Adw.get_major_version()}.{Adw.get_minor_version()}.{Adw.get_micro_version()}", None),
                ("PyGObject", gi.__version__, None),
                ("SQLite", sqlite3.sqlite_version, None),
                ("System", f"{platform.system()} {platform.release()} · {platform.machine()}", None),
            ]),
            ("Monitoring backend", [
                ("ICMP (ping)", ping_info, ping_ok),
                ("Local network status", backend, "NetworkManager" in backend),
                ("Current interface", (local.describe() or str(local.connected)) if local else "not probed yet", None),
                ("Last classification", app.engine.classifier.last_reason or "—", None),
            ]),
            ("Desktop integration", [
                ("Sound alerts", app.sound_player.backend_name if app.sound_player.available else
                 "no player found (install pipewire-utils, pulseaudio-utils or alsa-utils)", app.sound_player.available),
                ("Notifications", "via GApplication (desktop notification portal / service)", True),
                ("Status icon", tray.status_text() if tray else "disabled in settings",
                 bool(tray and tray.available)),
                ("Launch at login", "enabled" if autostart_enabled() else "disabled", None),
            ]),
            ("Data", [
                ("Database", str(app.db.path), None),
                ("Size", GLib.format_size(size), None),
                ("Schema version", str(app.db.schema_version()), None),
                ("Records", f"{counts['monitoring_sessions']} sessions · {counts['connection_states']} states · "
                            f"{counts['outages']} outages · {counts['measurements']} measurements", None),
                ("Log file", str(state_dir() / "netwatch.log"), None),
            ]),
        ]

    def rebuild(self) -> None:
        for g in self.groups:
            self.page.remove(g)
        self.groups = []
        lines = []
        for title, rows in self.diagnostics():
            g = self._group(title)
            lines.append(f"[{title}]")
            for name, value, ok in rows:
                g.add(_row(name, value, ok))
                lines.append(f"{name}: {value}")
        self.report_text = "\n".join(lines) + f"\nPython executable: {sys.executable}\n"
        test = self._group("Connectivity test", "Runs one check against every enabled target now, without "
                                                "recording it in the history.")
        self.test_rows = test
        btns = Gtk.Box(spacing=8, halign=Gtk.Align.END)
        run = Gtk.Button(label="Run Test")
        run.add_css_class("suggested-action")
        run.connect("clicked", lambda *_: self._run_test(test, run))
        copy = Gtk.Button(label="Copy Diagnostics")
        copy.connect("clicked", lambda *_: self._copy())
        about = Gtk.Button(label="About NetWatch")
        about.set_action_name("app.about")
        for b in (about, copy, run):
            btns.append(b)
        test.set_header_suffix(btns)

    def _run_test(self, group: Adw.PreferencesGroup, button: Gtk.Button) -> None:
        button.set_sensitive(False)
        spinner = Gtk.Spinner(spinning=True, margin_top=8, margin_bottom=8)
        group.add(spinner)
        app = self.app

        def work():
            local = app.probe.probe()
            timeout = float(app.settings.get("timeout_seconds"))
            results = []
            for t in app.repo.list_targets(enabled_only=True):
                adapter = app.adapters.get(t.check_type)
                results.append((t, adapter.check(t, timeout) if adapter else None))
            return local, results

        def done(res) -> None:
            group.remove(spinner)
            button.set_sensitive(True)
            local, results = res
            group.add(_row("Local network", (local.describe() or local.detail or "unknown") +
                           f" (connected: {local.connected})", local.connected))
            for t, r in results:
                if r is None:
                    group.add(_row(t.display, "no adapter", False))
                elif r.success:
                    group.add(_row(t.display, f"{r.latency_ms:.1f} ms" if r.latency_ms is not None else "OK", True))
                else:
                    group.add(_row(t.display, f"{r.error.label}: {r.detail}", False))

        run_async(work, done, lambda exc: (group.remove(spinner), button.set_sensitive(True),
                                           group.add(_row("Error", str(exc), False))))

    def _copy(self) -> None:
        Gdk.Display.get_default().get_clipboard().set(self.report_text)
        self.app.window.toast("Diagnostics copied to the clipboard")
