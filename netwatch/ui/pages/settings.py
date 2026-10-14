"""Settings page with explicit Save/Revert, validation and a targets editor."""

from __future__ import annotations

import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Optional

from gi.repository import Adw, Gio, GLib, Gtk

from netwatch.models import Target
from netwatch.settings import CHECK_TYPES, validate, validate_target
from netwatch.storage.repository import DuplicateTargetError
from netwatch.ui import dialogs
from netwatch.ui.background import run_async
from netwatch.ui.export import open_path, reports_dir
from netwatch.ui.widgets.common import label

if TYPE_CHECKING:
    from netwatch.ui.application import NetWatchApplication

NOTIFY_MODES = [("always", "Always"), ("hidden", "Only while the window is hidden"), ("never", "Never")]
THEMES = [("system", "Follow system"), ("light", "Light"), ("dark", "Dark")]
DENSITIES = [("comfortable", "Comfortable"), ("compact", "Compact")]


class SettingsPage(Gtk.Box):
    title = "Settings"
    icon = "emblem-system-symbolic"

    def __init__(self, app: "NetWatchApplication"):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.app = app
        self.fields: dict[str, tuple[Gtk.Widget, Callable[[], Any], Callable[[Any], None]]] = {}
        self.extra: dict[str, Any] = {}
        self._loading = False
        self.page = Adw.PreferencesPage(vexpand=True)
        self.append(self.page)
        self._build_monitoring()
        self._build_targets()
        self._build_alerts()
        self._build_notifications()
        self._build_appearance()
        self._build_background()
        self._build_data()
        # unsaved changes bar
        self.action_bar = Gtk.ActionBar(revealed=False)
        self.status = label("You have unsaved changes.", wrap=True)
        self.status.set_hexpand(True)
        self.action_bar.pack_start(self.status)
        self.revert_btn = Gtk.Button(label="Revert")
        self.revert_btn.connect("clicked", lambda *_: self.load())
        self.save_btn = Gtk.Button(label="Save")
        self.save_btn.add_css_class("suggested-action")
        self.save_btn.connect("clicked", lambda *_: self.save())
        self.action_bar.pack_end(self.save_btn)
        self.action_bar.pack_end(self.revert_btn)
        self.append(self.action_bar)
        self.load()

    # ------------------------------------------------------------ field helpers
    def _spin(self, group: Adw.PreferencesGroup, key: str, title: str, lo: int, hi: int, subtitle: str = "",
              step: int = 1) -> Adw.SpinRow:
        row = Adw.SpinRow.new_with_range(lo, hi, step)
        row.set_title(title)
        if subtitle:
            row.set_subtitle(subtitle)
        row.connect("notify::value", lambda *_: self._changed())
        group.add(row)
        self.fields[key] = (row, lambda: int(row.get_value()), lambda v: row.set_value(v))
        return row

    def _switch(self, group: Adw.PreferencesGroup, key: str, title: str, subtitle: str = "") -> Adw.SwitchRow:
        row = Adw.SwitchRow(title=title, subtitle=subtitle)
        row.connect("notify::active", lambda *_: self._changed())
        group.add(row)
        self.fields[key] = (row, row.get_active, row.set_active)
        return row

    def _combo(self, group: Adw.PreferencesGroup, key: str, title: str, options: list[tuple[str, str]],
               subtitle: str = "") -> Adw.ComboRow:
        row = Adw.ComboRow(title=title, subtitle=subtitle, model=Gtk.StringList.new([o[1] for o in options]))
        row.connect("notify::selected", lambda *_: self._changed())
        group.add(row)
        keys = [o[0] for o in options]
        self.fields[key] = (row, lambda: keys[row.get_selected()],
                            lambda v: row.set_selected(keys.index(v) if v in keys else 0))
        return row

    # ------------------------------------------------------------ groups
    def _build_monitoring(self) -> None:
        g = Adw.PreferencesGroup(title="Monitoring",
                                 description="Checks run in the background. A failure is confirmed only after the "
                                             "failure threshold is reached on every target.")
        self._spin(g, "interval_seconds", "Check interval", 1, 3600, "Seconds between check rounds")
        self._spin(g, "timeout_seconds", "Timeout", 1, 60, "Seconds to wait for each reply; must be shorter than "
                                                          "the interval")
        self._spin(g, "failure_threshold", "Failure threshold", 1, 20,
                   "Consecutive failed rounds before an outage is declared")
        self._spin(g, "recovery_threshold", "Recovery threshold", 1, 20,
                   "Consecutive successful rounds before recovery is confirmed")
        self._spin(g, "latency_warning_ms", "High latency threshold", 1, 10000,
                   "Milliseconds; latency above this is shown as degraded on the Performance page", step=10)
        self._spin(g, "loss_warning_pct", "Packet loss threshold", 1, 100, "Percent of failed checks considered "
                                                                          "degraded")
        self._switch(g, "start_monitoring_on_launch", "Start monitoring when NetWatch starts")
        self.page.add(g)

    def _build_targets(self) -> None:
        self.targets_group = Adw.PreferencesGroup(
            title="Targets",
            description="Connectivity is ONLINE while at least one enabled target responds. Changes to targets are "
                        "applied immediately.",
        )
        add = Gtk.Button(icon_name="list-add-symbolic", tooltip_text="Add target", valign=Gtk.Align.CENTER)
        add.add_css_class("flat")
        add.connect("clicked", lambda *_: self._edit_target(None))
        self.targets_group.set_header_suffix(add)
        self.target_rows: list[Gtk.Widget] = []
        self.page.add(self.targets_group)

    def _build_alerts(self) -> None:
        player = self.app.sound_player
        desc = (f"Sounds play through {player.backend_name}." if player.available else
                "No sound player was found (pw-play, paplay or aplay). Alerts stay silent; monitoring and "
                "notifications are unaffected.")
        g = Adw.PreferencesGroup(title="Sound Alerts", description=desc)
        self._switch(g, "sound_enabled", "Sound alerts")
        vol = self._spin(g, "sound_volume", "Volume", 0, 100, "Percent", step=5)
        test = Gtk.Button(label="Test", valign=Gtk.Align.CENTER)
        test.connect("clicked", lambda *_: self.app.sound_player.play("outage", int(vol.get_value())))
        test.set_sensitive(player.available)
        vol.add_suffix(test)
        self._switch(g, "sound_repeat", "Repeat during outages", "Soft heartbeat while the outage lasts")
        self._spin(g, "sound_repeat_seconds", "Repeat every", 3, 3600, "Seconds")
        self._switch(g, "sound_recovery", "Recovery sound")
        self._switch(g, "resume_alert_on_unmute", "Resume alert when unmuting during an outage")
        self._switch(g, "start_muted", "Start muted")
        self.page.add(g)

    def _build_notifications(self) -> None:
        g = Adw.PreferencesGroup(title="Desktop Notifications")
        self._combo(g, "notifications_mode", "Show notifications", NOTIFY_MODES)
        self._switch(g, "notify_outage", "When an outage is detected")
        self._switch(g, "notify_recovery", "When connectivity recovers")
        self._switch(g, "notify_errors", "When a monitoring error occurs")
        self.page.add(g)

    def _build_appearance(self) -> None:
        g = Adw.PreferencesGroup(title="Appearance")
        self._combo(g, "theme", "Style", THEMES)
        self._combo(g, "density", "Spacing", DENSITIES)
        self.page.add(g)

    def _build_background(self) -> None:
        tray = self.app.tray
        if tray is not None and tray.available:
            tray_sub = "A status icon is shown by your desktop's StatusNotifier host."
        else:
            tray_sub = ("No StatusNotifier host detected. On GNOME install the “AppIndicator and KStatusNotifierItem "
                        "Support” extension. Without it, reopen NetWatch from the app launcher — launching it again "
                        "shows the running instance.")
        g = Adw.PreferencesGroup(title="Background")
        self._switch(g, "hide_on_close", "Keep monitoring when the window is closed",
                     "Closing the window hides it; use Quit (Ctrl+Q) to stop NetWatch")
        self._switch(g, "tray_enabled", "Show status icon", tray_sub)
        self._switch(g, "launch_at_login", "Launch at login", "Starts hidden and monitoring (XDG autostart)")
        self.page.add(g)

    def _build_data(self) -> None:
        g = Adw.PreferencesGroup(title="Data")
        self.db_row = Adw.ActionRow(title="Database location")
        self.db_row.set_subtitle_selectable(True)
        move = Gtk.Button(label="Move…", valign=Gtk.Align.CENTER)
        move.connect("clicked", lambda *_: self._move_database())
        self.db_row.add_suffix(move)
        g.add(self.db_row)
        self.reports_row = Adw.ActionRow(title="Reports folder")
        choose = Gtk.Button(label="Choose…", valign=Gtk.Align.CENTER)
        choose.connect("clicked", lambda *_: self._choose_reports_dir())
        self.reports_row.add_suffix(choose)
        g.add(self.reports_row)
        self._spin(g, "retention_days", "Keep raw measurements for", 0, 3650,
                   "Days (0 = forever). Outage and state history is always kept.")
        backup_row = Adw.ActionRow(title="Backup and restore", subtitle="Consistent copy made with SQLite's backup API")
        backup = Gtk.Button(label="Back Up…", valign=Gtk.Align.CENTER)
        backup.connect("clicked", lambda *_: self._backup())
        restore = Gtk.Button(label="Restore…", valign=Gtk.Align.CENTER)
        restore.add_css_class("destructive-action")
        restore.connect("clicked", lambda *_: self._restore())
        backup_row.add_suffix(backup)
        backup_row.add_suffix(restore)
        g.add(backup_row)
        folder_row = Adw.ActionRow(title="Data folder")
        open_btn = Gtk.Button(label="Open", valign=Gtk.Align.CENTER)
        open_btn.connect("clicked", lambda *_: open_path(self.app.window, self.app.db.path.parent))
        folder_row.add_suffix(open_btn)
        g.add(folder_row)
        self.page.add(g)

    def set_page_visible(self, visible: bool) -> None:
        if visible and not self.has_unsaved():
            self.load()

    # ------------------------------------------------------------ load / save
    def load(self) -> None:
        self._loading = True
        values = self.app.settings.all()
        for key, (_w, _get, set_) in self.fields.items():
            set_(values[key])
        self.extra = {}
        self._loading = False
        self.db_row.set_subtitle(str(self.app.db.path))
        self.reports_row.set_subtitle(str(reports_dir(self.app)))
        self.reload_targets()
        self._changed()

    def pending(self) -> dict:
        current = self.app.settings.all()
        changes = {k: get() for k, (_w, get, _s) in self.fields.items() if get() != current[k]}
        changes.update({k: v for k, v in self.extra.items() if v != current.get(k)})
        return changes

    def has_unsaved(self) -> bool:
        return bool(self.pending())

    def _changed(self) -> None:
        if self._loading:
            return
        changes = self.pending()
        merged = dict(self.app.settings.all(), **changes)
        errors = validate(merged)
        bad = {e.key for e in errors}
        for key, (w, _g, _s) in self.fields.items():
            (w.add_css_class if key in bad else w.remove_css_class)("error")
        self.action_bar.set_revealed(bool(changes) or bool(errors))
        self.save_btn.set_sensitive(bool(changes) and not errors)
        if errors:
            self.status.set_label(errors[0].message)
            self.status.add_css_class("error")
        else:
            self.status.set_label(f"{len(changes)} unsaved change{'s' if len(changes) != 1 else ''}.")
            self.status.remove_css_class("error")

    def save(self) -> bool:
        changes = self.pending()
        if not changes:
            return True
        errors = self.app.settings.update(changes)
        if errors:
            self.status.set_label(errors[0].message)
            return False
        self.extra = {}
        self.app.apply_settings(set(changes))
        self.app.window.toast("Settings saved")
        self._changed()
        return True

    # ------------------------------------------------------------ targets
    def reload_targets(self) -> None:
        for row in self.target_rows:
            self.targets_group.remove(row)
        self.target_rows = []
        for t in self.app.repo.list_targets():
            row = Adw.ActionRow(title=t.name, subtitle=f"{t.address} · {CHECK_TYPES.get(t.check_type, t.check_type)}")
            sw = Gtk.Switch(active=t.enabled, valign=Gtk.Align.CENTER, tooltip_text="Enabled")
            sw.update_property([Gtk.AccessibleProperty.LABEL], [f"Enable {t.name}"])
            sw.connect("notify::active", lambda s, _p, target=t: self._toggle_target(target, s))
            edit = Gtk.Button(icon_name="document-edit-symbolic", valign=Gtk.Align.CENTER, tooltip_text="Edit")
            edit.add_css_class("flat")
            edit.connect("clicked", lambda _b, target=t: self._edit_target(target))
            remove = Gtk.Button(icon_name="user-trash-symbolic", valign=Gtk.Align.CENTER, tooltip_text="Remove")
            remove.add_css_class("flat")
            remove.connect("clicked", lambda _b, target=t: self._remove_target(target))
            row.add_suffix(sw)
            row.add_suffix(edit)
            row.add_suffix(remove)
            self.targets_group.add(row)
            self.target_rows.append(row)

    def _enabled_count(self, excluding: Optional[int] = None) -> int:
        return sum(1 for t in self.app.repo.list_targets(enabled_only=True) if t.id != excluding)

    def _toggle_target(self, target: Target, switch: Gtk.Switch) -> None:
        if not switch.get_active() and self._enabled_count(target.id) == 0:
            self.app.window.toast("At least one target must stay enabled.")
            switch.set_active(True)
            return
        target.enabled = switch.get_active()
        self.app.repo.update_target(target)
        self.app.targets_changed()

    def _remove_target(self, target: Target) -> None:
        if target.enabled and self._enabled_count(target.id) == 0:
            self.app.window.toast("Add or enable another target before removing the last one.")
            return

        def do() -> None:
            self.app.repo.delete_target(target.id)
            self.app.targets_changed()

        dialogs.confirm(self, f"Remove {target.name}?",
                        "The target stops being monitored. Its past measurements stay in the history.", "Remove", do)

    def _edit_target(self, target: Optional[Target]) -> None:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, margin_top=12, margin_bottom=12,
                      margin_start=12, margin_end=12)
        group = Adw.PreferencesGroup()
        name = Adw.EntryRow(title="Name", text=target.name if target else "")
        address = Adw.EntryRow(title="Address (IP, host name or URL)", text=target.address if target else "")
        kinds = list(CHECK_TYPES.items())
        kind = Adw.ComboRow(title="Check type", model=Gtk.StringList.new([k[1] for k in kinds]))
        if target:
            kind.set_selected([k[0] for k in kinds].index(target.check_type))
        for r in (name, address, kind):
            group.add(r)
        box.append(group)
        error = label("", ("error",), wrap=True)
        box.append(error)
        buttons = Gtk.Box(spacing=8, halign=Gtk.Align.END)
        cancel = Gtk.Button(label="Cancel")
        ok = Gtk.Button(label="Save" if target else "Add")
        ok.add_css_class("suggested-action")
        buttons.append(cancel)
        buttons.append(ok)
        box.append(buttons)
        close = dialogs.present_content(self, "Edit Target" if target else "Add Target", box, 460, 340)
        cancel.connect("clicked", lambda *_: close())

        def submit(*_a) -> None:
            n, a, k = name.get_text().strip(), address.get_text().strip(), kinds[kind.get_selected()][0]
            problems = validate_target(n, a, k)
            if problems:
                error.set_label(" ".join(problems))
                return
            try:
                if target:
                    self.app.repo.update_target(Target(target.id, n, a, k, target.enabled, target.position))
                else:
                    self.app.repo.add_target(n, a, k)
            except DuplicateTargetError as exc:
                error.set_label(str(exc))
                return
            close()
            self.app.targets_changed()

        ok.connect("clicked", submit)
        address.connect("entry-activated", submit)
        name.grab_focus()

    # ------------------------------------------------------------ data
    def _choose_reports_dir(self) -> None:
        dlg = Gtk.FileDialog(title="Choose Reports Folder", modal=True,
                             initial_folder=Gio.File.new_for_path(str(reports_dir(self.app).parent)))

        def done(d, res) -> None:
            try:
                f = d.select_folder_finish(res)
            except GLib.Error:
                return
            if f and f.get_path():
                self.extra["reports_dir"] = f.get_path()
                self.reports_row.set_subtitle(f.get_path() + "  (unsaved)")
                self._changed()

        dlg.select_folder(self.app.window, None, done)

    def _backup(self) -> None:
        stamp = time.strftime("%Y-%m-%d_%H%M")
        dlg = Gtk.FileDialog(title="Back Up Database", modal=True, initial_name=f"netwatch-backup-{stamp}.db")

        def done(d, res) -> None:
            try:
                f = d.save_finish(res)
            except GLib.Error:
                return
            path = f.get_path()
            run_async(lambda: self.app.db.backup_to(path),
                      lambda _r: self.app.window.toast(f"Backup saved to {Path(path).name}"),
                      lambda exc: dialogs.message(self, "Backup failed", str(exc)))

        dlg.save(self.app.window, None, done)

    def _restore(self) -> None:
        dlg = Gtk.FileDialog(title="Restore Database", modal=True)

        def done(d, res) -> None:
            try:
                f = d.open_finish(res)
            except GLib.Error:
                return
            path = f.get_path()
            dialogs.confirm(
                self, "Replace all history with this backup?",
                f"The current database will be replaced by {Path(path).name}. Monitoring restarts afterwards. "
                "Consider backing up the current data first.",
                "Restore", lambda: self.app.restore_database(path),
            )

        dlg.open(self.app.window, None, done)

    def _move_database(self) -> None:
        dlg = Gtk.FileDialog(title="Choose New Database Folder", modal=True)

        def done(d, res) -> None:
            try:
                f = d.select_folder_finish(res)
            except GLib.Error:
                return
            target = Path(f.get_path()) / "netwatch.db"
            if target.exists():
                dialogs.message(self, "A database already exists there",
                                f"{target} already exists. Choose another folder, or use Restore to load it.")
                return
            dialogs.confirm(self, "Move the database?",
                            f"NetWatch will copy its data to {target} and use it from now on. The old file is left "
                            "in place so you can delete it yourself.", "Move", lambda: self.app.relocate_database(target),
                            destructive=False)

        dlg.select_folder(self.app.window, None, done)
