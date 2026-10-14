"""Reports page: choose a range and format, preview, export."""

from __future__ import annotations

import time
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Optional

from gi.repository import Adw, Gio, GLib, Gtk

from netwatch import reports
from netwatch.timefmt import day_start_of, format_timestamp
from netwatch.ui.background import Generation, run_async
from netwatch.ui.export import open_path, reports_dir, start_export
from netwatch.ui.widgets.common import label

if TYPE_CHECKING:
    from netwatch.ui.application import NetWatchApplication

PERIODS = [("24h", "Last 24 hours"), ("7d", "Last 7 days"), ("30d", "Last 30 days"),
           ("all", "All retained history"), ("custom", "Custom date range")]
FORMATS = list(reports.FORMATS.items())


class DateRow(Adw.ActionRow):
    def __init__(self, title: str, initial: datetime, on_change):
        super().__init__(title=title)
        self.value = initial.date()
        self.button = Gtk.MenuButton(valign=Gtk.Align.CENTER)
        self.calendar = Gtk.Calendar()
        self.calendar.select_day(GLib.DateTime.new_local(initial.year, initial.month, initial.day, 0, 0, 0))
        self.calendar.connect("day-selected", self._picked)
        pop = Gtk.Popover()
        pop.set_child(self.calendar)
        self.button.set_popover(pop)
        self.add_suffix(self.button)
        self.set_activatable_widget(self.button)
        self.on_change = on_change
        self._sync()

    def _picked(self, cal) -> None:
        d = cal.get_date()
        self.value = datetime(d.get_year(), d.get_month(), d.get_day_of_month()).date()
        self._sync()
        self.button.popdown()
        self.on_change()

    def _sync(self) -> None:
        self.button.set_label(self.value.strftime("%a, %b %d, %Y"))


class ReportsPage(Gtk.Box):
    title = "Reports"
    icon = "x-office-document-symbolic"

    def __init__(self, app: "NetWatchApplication"):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.app = app
        self.gen = Generation()
        page = Adw.PreferencesPage()
        self.append(page)
        page.set_vexpand(True)

        group = Adw.PreferencesGroup(
            title="Report",
            description="Reports include coverage, uptime, downtime, outages, the event timeline, monitoring "
                        "interruptions and the limitations of the collected data.",
        )
        self.period_row = Adw.ComboRow(title="Time range", model=Gtk.StringList.new([p[1] for p in PERIODS]))
        self.period_row.set_selected(1)
        self.period_row.connect("notify::selected", lambda *_: self._period_changed())
        group.add(self.period_row)
        today = datetime.now()
        self.from_row = DateRow("From", today - timedelta(days=7), self._preview)
        self.to_row = DateRow("To (inclusive)", today, self._preview)
        group.add(self.from_row)
        group.add(self.to_row)
        self.format_row = Adw.ComboRow(title="Format", model=Gtk.StringList.new([f[1] for f in FORMATS]))
        self.format_row.set_selected(2)
        group.add(self.format_row)
        page.add(group)

        actions = Adw.PreferencesGroup()
        box = Gtk.Box(spacing=10, halign=Gtk.Align.END)
        self.error_label = label("", ("error",), wrap=True)
        self.error_label.set_hexpand(True)
        box.append(self.error_label)
        export = Gtk.Button(label="Export Report…")
        export.add_css_class("suggested-action")
        export.add_css_class("pill")
        export.set_tooltip_text("Ctrl+S")
        export.connect("clicked", lambda *_: self.export())
        box.append(export)
        actions.add(box)
        page.add(actions)

        preview = Adw.PreferencesGroup(title="Preview", description="Plain-text summary of the selected range.")
        self.preview_view = Gtk.TextView(editable=False, monospace=True, wrap_mode=Gtk.WrapMode.WORD_CHAR,
                                         top_margin=12, bottom_margin=12, left_margin=12, right_margin=12)
        self.preview_view.update_property([Gtk.AccessibleProperty.LABEL], ["Report preview"])
        sc = Gtk.ScrolledWindow(min_content_height=320, max_content_height=480, propagate_natural_height=True)
        sc.set_child(self.preview_view)
        sc.add_css_class("card")
        preview.add(sc)
        page.add(preview)

        folder = Adw.PreferencesGroup(title="Saved reports")
        self.folder_row = Adw.ActionRow(title="Reports folder")
        open_btn = Gtk.Button(label="Open Folder", valign=Gtk.Align.CENTER)
        open_btn.connect("clicked", lambda *_: self._open_folder())
        self.folder_row.add_suffix(open_btn)
        folder.add(self.folder_row)
        self.recent_group = Adw.PreferencesGroup(title="Recent reports")
        page.add(folder)
        page.add(self.recent_group)
        self.recent_rows: list[Gtk.Widget] = []
        self._period_changed()

    # ------------------------------------------------------------ state
    def set_page_visible(self, visible: bool) -> None:
        if visible:
            self._preview()
            self._list_recent()

    def selection(self) -> tuple[str, Optional[tuple[float, float]]]:
        key = PERIODS[self.period_row.get_selected()][0]
        if key != "custom":
            return key, None
        start = day_start_of(self.from_row.value)
        end = min(day_start_of(self.to_row.value + timedelta(days=1)), time.time())
        return "custom", (start, end)

    def _period_changed(self) -> None:
        custom = PERIODS[self.period_row.get_selected()][0] == "custom"
        self.from_row.set_visible(custom)
        self.to_row.set_visible(custom)
        self._preview()

    def _validate(self) -> Optional[str]:
        key, custom = self.selection()
        if custom and custom[1] <= custom[0]:
            return "The end date must be on or after the start date, and not entirely in the future."
        return None

    def _preview(self) -> None:
        err = self._validate()
        self.error_label.set_label(err or "")
        if err:
            return
        key, custom = self.selection()
        token = self.gen.next()

        def work():
            data = reports.build_report(self.app.repo, self.app.stats, self.app.settings, key, custom)
            text = reports.render_txt(data)
            lines = text.splitlines()
            return "\n".join(lines[:400]) + ("\n…" if len(lines) > 400 else "")

        run_async(work, lambda text: self.gen.current(token) and self.preview_view.get_buffer().set_text(text))

    def export(self) -> None:
        err = self._validate()
        if err:
            self.error_label.set_label(err)
            return
        key, custom = self.selection()
        fmt = FORMATS[self.format_row.get_selected()][0]
        start_export(self.app, self.app.window, key, custom, fmt=fmt, on_saved=lambda _p: self._list_recent())

    def _open_folder(self) -> None:
        folder = reports_dir(self.app)
        folder.mkdir(parents=True, exist_ok=True)
        open_path(self.app.window, folder)

    def _list_recent(self) -> None:
        folder = reports_dir(self.app)
        self.folder_row.set_subtitle(str(folder))
        for row in self.recent_rows:
            self.recent_group.remove(row)
        self.recent_rows = []
        try:
            files = sorted((p for p in folder.glob("netwatch-*") if p.suffix in (".txt", ".csv", ".html")),
                           key=lambda p: p.stat().st_mtime, reverse=True)[:8]
        except OSError:
            files = []
        if not files:
            row = Adw.ActionRow(title="No reports saved yet")
            self.recent_group.add(row)
            self.recent_rows.append(row)
            return
        for p in files:
            row = Adw.ActionRow(title=p.name, subtitle=format_timestamp(p.stat().st_mtime))
            btn = Gtk.Button(icon_name="document-open-symbolic", valign=Gtk.Align.CENTER, tooltip_text="Open")
            btn.add_css_class("flat")
            btn.connect("clicked", lambda _b, path=p: open_path(self.app.window, path))
            row.add_suffix(btn)
            self.recent_group.add(row)
            self.recent_rows.append(row)


__all__ = ["ReportsPage", "Gio"]
