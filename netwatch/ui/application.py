"""Adw.Application: single-process owner of the database, engine, alerts, tray and window."""

from __future__ import annotations

import logging
import signal
import time
from pathlib import Path
from typing import Optional

from gi.repository import Adw, Gdk, Gio, GLib, Gtk

from netwatch import APP_ID, APP_NAME, __version__
from netwatch.alerts import AlertManager, SoundPlayer
from netwatch.autostart import set_autostart
from netwatch.models import DOWN_STATES, ConnState, Lifecycle, Transition
from netwatch.monitor.adapters import build_adapters
from netwatch.monitor.engine import MonitorEngine
from netwatch.monitor.netstate import LocalNetworkProbe
from netwatch.monitor.process import ProcessRunner
from netwatch.notify import Notifier
from netwatch.paths import write_bootstrap, read_bootstrap
from netwatch.settings import DEFAULT_TARGETS, SettingsStore
from netwatch.stats import ReliabilityService
from netwatch.storage.db import Database, DatabaseError
from netwatch.storage.repository import Repository
from netwatch.timefmt import format_duration, format_timestamp
from netwatch.tray import MenuItem, StatusIcon
from netwatch.ui import background, dialogs

log = logging.getLogger(__name__)

PKG_DIR = Path(__file__).resolve().parent
SOURCE_DATA = PKG_DIR.parent.parent / "data"

SHORTCUTS_UI = """
<interface>
  <object class="GtkShortcutsWindow" id="shortcuts">
    <property name="modal">1</property>
    <child>
      <object class="GtkShortcutsSection">
        <property name="section-name">main</property>
        <child>
          <object class="GtkShortcutsGroup">
            <property name="title">Monitoring</property>
            <child><object class="GtkShortcutsShortcut"><property name="title">Pause / resume monitoring</property><property name="accelerator">space</property></object></child>
            <child><object class="GtkShortcutsShortcut"><property name="title">Mute / unmute alerts</property><property name="accelerator">m</property></object></child>
          </object>
        </child>
        <child>
          <object class="GtkShortcutsGroup">
            <property name="title">Navigation</property>
            <child><object class="GtkShortcutsShortcut"><property name="title">Outage history</property><property name="accelerator">&lt;Control&gt;o</property></object></child>
            <child><object class="GtkShortcutsShortcut"><property name="title">Settings</property><property name="accelerator">&lt;Control&gt;comma</property></object></child>
            <child><object class="GtkShortcutsShortcut"><property name="title">Go to page 1–6</property><property name="accelerator">&lt;Control&gt;1...&lt;Control&gt;6</property></object></child>
            <child><object class="GtkShortcutsShortcut"><property name="title">Back / close dialog</property><property name="accelerator">Escape</property></object></child>
          </object>
        </child>
        <child>
          <object class="GtkShortcutsGroup">
            <property name="title">General</property>
            <child><object class="GtkShortcutsShortcut"><property name="title">Export report</property><property name="accelerator">&lt;Control&gt;s</property></object></child>
            <child><object class="GtkShortcutsShortcut"><property name="title">Keyboard shortcuts</property><property name="accelerator">&lt;Control&gt;question</property></object></child>
            <child><object class="GtkShortcutsShortcut"><property name="title">Quit (stops monitoring)</property><property name="accelerator">&lt;Control&gt;q</property></object></child>
          </object>
        </child>
      </object>
    </child>
  </object>
</interface>
"""


class NetWatchApplication(Adw.Application):
    def __init__(self, db_path: Path, background_mode: bool = False, non_unique: bool = False):
        flags = Gio.ApplicationFlags.HANDLES_COMMAND_LINE
        if non_unique:  # tests / tools: do not talk to a running instance
            flags |= Gio.ApplicationFlags.NON_UNIQUE
        super().__init__(application_id=APP_ID, flags=flags)
        self.db_path = db_path
        self.start_hidden = background_mode
        self.window = None
        self.tray: Optional[StatusIcon] = None
        self.engine: Optional[MonitorEngine] = None
        self._dark_provider: Optional[Gtk.CssProvider] = None
        self._outage_started: Optional[float] = None
        self._last_error_notice = 0.0
        self._presented = False
        self._timers: list[int] = []
        self.startup_error: Optional[str] = None

    # ------------------------------------------------------------ startup
    def do_startup(self) -> None:
        Adw.Application.do_startup(self)
        GLib.set_application_name(APP_NAME)
        Gtk.Window.set_default_icon_name(APP_ID)
        self._load_css()
        try:
            self._init_backend()
        except (DatabaseError, OSError) as exc:
            log.exception("cannot open database")
            self.startup_error = str(exc)
            return
        self._install_actions()
        for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            GLib.unix_signal_add(GLib.PRIORITY_HIGH, sig, self._on_signal, sig)
        from netwatch.ui.window import MainWindow  # noqa: PLC0415 (needs CSS/icons first)

        self.window = MainWindow(self)
        self.apply_settings({"theme", "density", "muted"}, initial=True)
        self._timers += [
            GLib.timeout_add_seconds(1, self._tick_1s),
            GLib.timeout_add_seconds(30, self._tick_30s),
            GLib.timeout_add_seconds(6 * 3600, self._prune),
        ]
        GLib.timeout_add_seconds(60, self._prune_once)  # one-shot; removes itself
        if self.recovered:
            last = max(r["ended_at"] for r in self.recovered)
            self.window.show_banner(
                f"NetWatch stopped unexpectedly last time (last check {format_timestamp(last)}). "
                "The time since then is shown as App Not Running.", "Dismiss", lambda: None)
        if self.settings.get("start_monitoring_on_launch"):
            self.engine.start()
        else:
            self.window.show_banner("Monitoring is stopped.", "Start", self.start_monitoring)
        self._update_tray()

    def _init_backend(self) -> None:
        self.db = Database(self.db_path)
        self.db.migrate()
        self.repo = Repository(self.db)
        self.settings = SettingsStore(self.db)
        self.stats = ReliabilityService(self.repo)
        self.repo.ensure_default_targets(DEFAULT_TARGETS)
        self.recovered = self.repo.recover_interrupted_sessions()
        self.repo.log_event("app_started", f"{APP_NAME} {__version__} started")
        self.runner = ProcessRunner()
        self.adapters = build_adapters(self.runner)
        self.probe = LocalNetworkProbe(self.runner)
        self.engine = MonitorEngine(self.repo, self.settings, self.adapters, self.probe, self.runner)
        self.engine.add_listener(lambda kind, payload: GLib.idle_add(self._on_engine_event, kind, payload))
        self.sound_player = SoundPlayer()
        if self.settings.get("start_muted"):
            self.settings.set("muted", True)
        self.alerts = AlertManager(self.sound_player, self.settings.get, time.time)
        self.notifier = Notifier(self, self.settings.get, lambda: bool(self.window and self.window.get_visible()))
        if self.settings.get("tray_enabled"):
            self._start_tray()

    def _load_css(self) -> None:
        display = Gdk.Display.get_default()
        if display is None:
            return
        provider = Gtk.CssProvider()
        provider.load_from_path(str(PKG_DIR / "style.css"))
        Gtk.StyleContext.add_provider_for_display(display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        icons = SOURCE_DATA / "icons"
        if icons.is_dir():  # running from a source checkout
            Gtk.IconTheme.get_for_display(display).add_search_path(str(icons))
        manager = Adw.StyleManager.get_default()
        manager.connect("notify::dark", lambda *_: self._sync_dark_css())
        self._sync_dark_css()

    def _sync_dark_css(self) -> None:
        display = Gdk.Display.get_default()
        dark = Adw.StyleManager.get_default().get_dark()
        if dark and self._dark_provider is None:
            self._dark_provider = Gtk.CssProvider()
            self._dark_provider.load_from_path(str(PKG_DIR / "style-dark.css"))
            Gtk.StyleContext.add_provider_for_display(display, self._dark_provider,
                                                      Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION + 1)
        elif not dark and self._dark_provider is not None:
            Gtk.StyleContext.remove_provider_for_display(display, self._dark_provider)
            self._dark_provider = None
        if self.window:
            stack = [self.window]
            while stack:  # Cairo-drawn widgets cache their output; redraw them with the new palette
                w = stack.pop()
                if isinstance(w, Gtk.DrawingArea):
                    w.queue_draw()
                child = w.get_first_child()
                while child is not None:
                    stack.append(child)
                    child = child.get_next_sibling()

    def _start_tray(self) -> None:
        icons = SOURCE_DATA / "icons" if (SOURCE_DATA / "icons").is_dir() else None
        self.tray = StatusIcon(self.show_window, icons)
        self.tray.start()

    # ------------------------------------------------------------ command line / activation
    def do_command_line(self, command_line: Gio.ApplicationCommandLine) -> int:
        if self.startup_error:
            self._show_fatal(self.startup_error)
            return 1
        args = command_line.get_arguments()[1:]
        if "--background" in args and not self._presented:
            log.info("started in background mode")
            if not self.settings.get("start_monitoring_on_launch") and not self.engine.is_active:
                self.engine.start()  # launching at login means "monitor"
            return 0
        self.show_window()
        return 0

    def do_activate(self) -> None:
        self.show_window()

    def _show_fatal(self, message: str) -> None:
        win = Adw.ApplicationWindow(application=self, title=APP_NAME, default_width=520, default_height=360)
        page = Adw.StatusPage(icon_name="dialog-error-symbolic", title="NetWatch could not open its database",
                              description=GLib.markup_escape_text(f"{message}\n\nDatabase: {self.db_path}"))
        tv = Adw.ToolbarView()
        tv.add_top_bar(Adw.HeaderBar())
        tv.set_content(page)
        win.set_content(tv)
        win.present()

    def show_window(self) -> None:
        if self.window is None:
            return
        self._presented = True
        self.window.present()

    # ------------------------------------------------------------ actions
    def _install_actions(self) -> None:
        def add(name: str, cb, accels: Optional[list[str]] = None) -> None:
            act = Gio.SimpleAction.new(name, None)
            act.connect("activate", lambda *_: cb())
            self.add_action(act)
            if accels:
                self.set_accels_for_action(f"app.{name}", accels)

        add("quit", self.request_quit, ["<Control>q"])
        add("show-window", self.show_window)
        add("show-history", lambda: (self.show_window(), self.window.navigate("history")))
        add("start", self.start_monitoring)
        add("stop", self.stop_monitoring)
        add("toggle-pause", self.toggle_pause)
        add("about", self.show_about)
        add("shortcuts", self.show_shortcuts, ["<Control>question"])
        mute = Gio.SimpleAction.new_stateful("mute", None, GLib.Variant("b", bool(self.settings.get("muted"))))
        mute.connect("change-state", lambda a, v: self.set_muted(v.get_boolean()))
        mute.connect("activate", lambda a, _p: self.set_muted(not a.get_state().get_boolean()))
        self.add_action(mute)
        self.set_accels_for_action("win.export", ["<Control>s"])
        self.set_accels_for_action("win.open-history", ["<Control>o"])
        self.set_accels_for_action("win.preferences", ["<Control>comma"])
        for i in range(1, 7):
            self.set_accels_for_action(f"win.page-{i}", [f"<Control>{i}"])

    def start_monitoring(self) -> None:
        if self.engine.lifecycle == Lifecycle.PAUSED:
            self.engine.resume()
        elif not self.engine.is_active:
            self.engine.start()
        self.window.show_banner(None)

    def stop_monitoring(self) -> None:
        if not self.engine.is_active:
            return
        background.run_async(lambda: self.engine.stop(reason="user"), lambda _r: self.history_changed())

    def toggle_pause(self) -> None:
        lc = self.engine.lifecycle
        if lc == Lifecycle.RUNNING:
            self.engine.pause()
            self.window.toast("Monitoring paused — this time is recorded as unmonitored")
        elif lc == Lifecycle.PAUSED:
            self.engine.resume()
            self.window.toast("Monitoring resumed")
        elif lc in (Lifecycle.STOPPED, Lifecycle.ERROR):
            self.start_monitoring()

    def set_muted(self, muted: bool) -> None:
        self.settings.set("muted", muted)
        self.alerts.set_muted(muted)
        action = self.lookup_action("mute")
        if action:
            action.set_state(GLib.Variant("b", muted))
        if self.window:
            self.window.set_muted_indicator(muted)
        self._update_tray()

    def request_quit(self) -> None:
        if self.window and self.window.pages["settings"].has_unsaved():
            self.show_window()
            self.window.navigate("settings")
            dialogs.choose(
                self.window, "Quit with unsaved settings?", "Your settings changes have not been saved.",
                [("cancel", "Cancel", ""), ("discard", "Discard and Quit", "destructive"),
                 ("save", "Save and Quit", "suggested")],
                lambda rid: self._quit_after(rid), default="save")
            return
        self.quit()

    def _quit_after(self, rid: str) -> None:
        if rid == "save" and not self.window.pages["settings"].save():
            return
        if rid in ("save", "discard"):
            self.quit()

    def on_window_close_request(self) -> bool:
        """Return True to keep the window (hidden)."""
        if self.settings.get("hide_on_close") and self.engine.is_active:
            self.window.set_visible(False)
            self._presented = False
            if not self.settings.get("background_notice_shown"):
                self.notifier.notify(
                    "info", "NetWatch is still monitoring",
                    "The window was closed but monitoring continues in the background. "
                    + ("Use the status icon to reopen it or quit." if self.tray and self.tray.available else
                       "Launch NetWatch again to reopen the window, or choose Quit to stop."),
                    force=True)
                self.settings.set("background_notice_shown", True)
            return True
        return False

    def show_about(self) -> None:
        kwargs = dict(application_name=APP_NAME, application_icon=APP_ID, version=__version__,
                      developer_name="NetWatch contributors", license_type=Gtk.License.MIT_X11,
                      comments="Local-first network connectivity and outage monitor for the Linux desktop.",
                      website="https://github.com/netwatch-app/netwatch")
        if hasattr(Adw, "AboutDialog"):
            Adw.AboutDialog(**kwargs).present(self.window)
        else:
            Adw.AboutWindow(transient_for=self.window, **kwargs).present()

    def show_shortcuts(self) -> None:
        builder = Gtk.Builder.new_from_string(SHORTCUTS_UI, -1)
        win = builder.get_object("shortcuts")
        win.set_transient_for(self.window)
        win.present()

    # ------------------------------------------------------------ engine events (main thread)
    def _on_engine_event(self, kind: str, payload) -> bool:
        if self.window is None:
            return GLib.SOURCE_REMOVE
        if kind == "snapshot":
            self.window.pages["overview"].on_snapshot(payload)
            self._update_tray(payload)
        elif kind == "transition":
            self._on_transition(payload)
        elif kind == "lifecycle":
            lifecycle, message = payload
            if lifecycle in (Lifecycle.PAUSED, Lifecycle.STOPPED):
                self.alerts.outage_ended(recovered=False)
                self._outage_started = None
            if lifecycle == Lifecycle.ERROR:
                self.window.show_banner(message, "Restart", self._restart_engine)
                self._notify_error(message)
            elif lifecycle == Lifecycle.STOPPED:
                self.window.show_banner("Monitoring is stopped. Nothing is being recorded.", "Start",
                                        self.start_monitoring)
            elif lifecycle == Lifecycle.RUNNING:
                self.window.show_banner(None)
            self.history_changed()
            self._update_tray()
        elif kind == "error":
            self._notify_error(str(payload))
        return GLib.SOURCE_REMOVE

    def _on_transition(self, t: Transition) -> None:
        if t.new in DOWN_STATES and t.previous not in DOWN_STATES:
            self._outage_started = t.effective_at
            self.alerts.outage_started()
            title = "Internet connection lost" if t.new == ConnState.NO_INTERNET else "Network disconnected"
            body = ("Your network is connected but no internet target responds." if t.new == ConnState.NO_INTERNET
                    else "There is no active network connection.")
            self.notifier.notify("outage", title, body)
        elif t.previous in DOWN_STATES and t.new not in DOWN_STATES:
            recovered = t.new == ConnState.ONLINE
            self.alerts.outage_ended(recovered=recovered)
            if recovered:
                dur = t.effective_at - self._outage_started if self._outage_started else None
                self.notifier.notify("recovery", "Connection restored",
                                     f"Back online after {format_duration(dur, precise=True)}." if dur else
                                     "Back online.")
            self._outage_started = None
        self.history_changed()

    def _notify_error(self, message: str) -> None:
        now = time.time()
        if now - self._last_error_notice > 600:
            self._last_error_notice = now
            self.notifier.notify("error", "NetWatch monitoring problem", message)

    def _restart_engine(self) -> None:
        def work():
            self.engine.stop(reason="restart")
        background.run_async(work, lambda _r: self.engine.start())

    # ------------------------------------------------------------ tray
    def _update_tray(self, snap=None) -> None:
        if self.tray is None or self.engine is None:
            return
        snap = snap or self.engine.snapshot
        lc = snap.lifecycle
        if lc == Lifecycle.RUNNING and snap.state == ConnState.ONLINE:
            icon, status = "online", "Online"
        elif lc == Lifecycle.RUNNING and snap.state in DOWN_STATES:
            icon, status = "offline", snap.state.label
        elif lc == Lifecycle.RUNNING:
            icon, status = "idle", "Checking…"
        else:
            icon, status = "idle", f"Monitoring {lc.label.lower()}"
        muted = bool(self.settings.get("muted"))
        toggle_label = {"running": "Pause Monitoring", "paused": "Resume Monitoring"}.get(lc.value, "Start Monitoring")
        items = [
            MenuItem(1, "Open NetWatch", self.show_window),
            MenuItem(2, separator=True),
            MenuItem(3, status, enabled=False),
            MenuItem(4, toggle_label, self.toggle_pause),
            MenuItem(5, "Mute Alerts", lambda: self.set_muted(not self.settings.get("muted")), toggle=muted),
            MenuItem(6, separator=True),
            MenuItem(7, "Quit NetWatch", self.request_quit),
        ]
        self.tray.update(icon, f"{APP_NAME}: {status}" + (" (muted)" if muted else ""), items)

    # ------------------------------------------------------------ timers
    def _tick_1s(self) -> bool:
        self.alerts.tick()
        if self.window and self.window.get_visible():
            self.window.pages["overview"].tick_clock()
            snap = self.engine.snapshot
            self.window.sidebar_status.set_label(
                f"{snap.lifecycle.label} · {snap.state.short_label if snap.lifecycle == Lifecycle.RUNNING else ''}"
                .rstrip(" ·"))
        return GLib.SOURCE_CONTINUE

    def _tick_30s(self) -> bool:
        if self.window and self.window.get_visible():
            self.window.pages["history"].periodic()
            self.window.pages["performance"].periodic()
        return GLib.SOURCE_CONTINUE

    def _prune(self) -> bool:
        days = int(self.settings.get("retention_days"))
        if days > 0:
            cutoff = time.time() - days * 86400
            background.run_async(lambda: self.repo.prune_measurements(cutoff),
                                 lambda n: n and log.info("pruned %s old measurements", n))
        return GLib.SOURCE_CONTINUE

    def _prune_once(self) -> bool:
        self._prune()
        return GLib.SOURCE_REMOVE

    # ------------------------------------------------------------ settings / data changes
    def apply_settings(self, keys: set, initial: bool = False) -> None:
        s = self.settings
        if "theme" in keys:
            scheme = {"system": Adw.ColorScheme.DEFAULT, "light": Adw.ColorScheme.FORCE_LIGHT,
                      "dark": Adw.ColorScheme.FORCE_DARK}[s.get("theme")]
            Adw.StyleManager.get_default().set_color_scheme(scheme)
        if "density" in keys and self.window:
            (self.window.add_css_class if s.get("density") == "compact" else self.window.remove_css_class)("nw-compact")
        if "muted" in keys:
            self.set_muted(bool(s.get("muted")))
        if "tray_enabled" in keys:
            if s.get("tray_enabled") and self.tray is None:
                self._start_tray()
                self._update_tray()
            elif not s.get("tray_enabled") and self.tray is not None:
                self.tray.stop()
                self.tray = None
        if "launch_at_login" in keys and not initial:
            try:
                set_autostart(bool(s.get("launch_at_login")))
            except OSError as exc:
                dialogs.message(self.window, "Could not update autostart", str(exc))
        if "retention_days" in keys and not initial:
            self._prune()
        if keys & {"interval_seconds", "timeout_seconds", "failure_threshold", "recovery_threshold"} and not initial:
            self.repo.log_event("settings_changed", "Monitoring settings changed", self.engine.session_id,
                                data={k: s.get(k) for k in keys})
            self.window.pages["overview"].reload_targets()

    def targets_changed(self) -> None:
        self.engine.reload()
        self.window.pages["overview"].reload_targets()
        self.window.pages["settings"].reload_targets()
        self.history_changed()

    def history_changed(self) -> None:
        if self.window is None:
            return
        self.window.pages["history"].mark_dirty()
        if self.window.pages["overview"].visible_page:
            self.window.pages["overview"].refresh_stats()

    def _reload_after_data_swap(self, was_active: bool, message: str) -> None:
        self.settings.reload()
        if was_active:
            self.engine.start()
        self.window.pages["settings"].load()
        self.window.pages["overview"].reload_targets()
        self.apply_settings({"theme", "density", "muted"}, initial=True)
        self.history_changed()
        self.window.toast(message)

    def restore_database(self, path: str) -> None:
        was_active = self.engine.is_active

        def work():
            self.engine.stop(reason="restore")
            self.db.restore_from(path)
            self.repo.recover_interrupted_sessions()
            self.repo.log_event("restored", f"Database restored from {path}")

        background.run_async(work, lambda _r: self._reload_after_data_swap(was_active, "Backup restored"),
                             lambda exc: (dialogs.message(self.window, "Restore failed", str(exc)),
                                          was_active and self.engine.start()))

    def relocate_database(self, target: Path) -> None:
        was_active = self.engine.is_active

        def work():
            self.engine.stop(reason="relocate")
            self.db.backup_to(target)
            config = read_bootstrap()
            config["database_path"] = str(target)
            write_bootstrap(config)
            self.db.close()
            self.db.path = Path(target)
            self.db.reopen()
            self.db.migrate()

        background.run_async(work, lambda _r: self._reload_after_data_swap(was_active, f"Database moved to {target}"),
                             lambda exc: (dialogs.message(self.window, "Could not move the database", str(exc)),
                                          was_active and self.engine.start()))

    # ------------------------------------------------------------ shutdown
    def _on_signal(self, sig) -> bool:
        log.info("received signal %s, shutting down", sig)
        self.quit()
        return GLib.SOURCE_REMOVE

    def do_shutdown(self) -> None:
        for t in self._timers:
            if GLib.MainContext.default().find_source_by_id(t):
                GLib.source_remove(t)
        self._timers = []
        try:
            if self.engine is not None:
                self.engine.stop(reason="quit")
            if getattr(self, "alerts", None):
                self.alerts.shutdown()
            if self.tray is not None:
                self.tray.stop()
            if getattr(self, "repo", None):
                self.repo.log_event("app_quit", f"{APP_NAME} quit")
            if getattr(self, "db", None):
                self.db.close()
        except Exception:
            log.exception("error during shutdown")
        background.shutdown()
        Adw.Application.do_shutdown(self)
