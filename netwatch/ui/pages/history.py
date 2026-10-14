"""Outage History — the primary screen (follows the reference design)."""

from __future__ import annotations

import time
from datetime import timedelta
from typing import TYPE_CHECKING, Optional

from gi.repository import Adw, Gio, GLib, Gtk, Pango

from netwatch.models import DOWN_STATES, ConnState
from netwatch.stats import DayStats, HistoryData, Segment
from netwatch.timefmt import (
    day_start_of,
    format_duration,
    format_duration_long,
    format_percent,
    format_timestamp,
)
from netwatch.ui import dialogs
from netwatch.ui.background import Generation, run_async
from netwatch.ui.event_detail import show_event_detail
from netwatch.ui.widgets.calendar import CalendarHeatmap
from netwatch.ui.widgets.common import (
    MetricTile,
    StateDot,
    flow_append,
    flow_box,
    label,
    legend_item,
    responsive_flow,
    section_header,
)
from netwatch.ui.widgets.timeline import TimelineBar

if TYPE_CHECKING:
    from netwatch.ui.application import NetWatchApplication

PERIODS = [("24h", "24 Hours"), ("7d", "7 Days"), ("30d", "30 Days"), ("all", "All Time")]
STATE_FILTERS = [
    ("all", "All states", None),
    ("outages", "Outages only", DOWN_STATES),
    ("online", "Online", {ConnState.ONLINE}),
    ("no_internet", "No Internet", {ConnState.NO_INTERNET}),
    ("not_connected", "Not Connected", {ConnState.NOT_CONNECTED}),
    ("unmonitored", "Not monitored (app not running, paused, unknown)",
     {ConnState.NOT_RUNNING, ConnState.PAUSED, ConnState.UNKNOWN}),
]
PAGE_SIZE = 60
JUMP_CONTEXT = 10  # rows shown above an event that is jumped to


class EventRow(Gtk.ListBoxRow):
    def __init__(self, seg: Segment, outage, now: float):
        super().__init__()
        self.seg = seg
        box = Gtk.Box(spacing=14, margin_start=4, margin_end=8)
        dot_holder = Gtk.Box(width_request=20, halign=Gtk.Align.CENTER)
        dot_holder.append(StateDot(seg.state))
        box.append(dot_holder)
        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, hexpand=True)
        end = "now" if seg.ongoing else format_timestamp(seg.end, now)
        title = label(f"{format_timestamp(seg.start, now)} – {end}", ("nw-event-title",))
        title.set_ellipsize(Pango.EllipsizeMode.END)
        text.append(title)
        sub = seg.state.short_label
        if seg.targets:
            targets = seg.targets.split(", ")
            sub += " · " + (targets[0] if seg.state == ConnState.ONLINE else ", ".join(targets[:3]))
        subtitle = label(sub, ("dim-label", "nw-event-sub"))
        subtitle.set_ellipsize(Pango.EllipsizeMode.END)
        text.append(subtitle)
        detail = ""
        local = seg.local
        if local and local.describe():
            detail = local.describe()
        elif seg.synthetic:
            detail = "No monitoring records for this period"
        elif seg.state in (ConnState.UNKNOWN, ConnState.PAUSED) and seg.reason:
            detail = seg.reason
        if detail:
            d = label(detail, ("dim-label", "nw-event-detail"))
            d.set_opacity(0.75)
            d.set_ellipsize(Pango.EllipsizeMode.END)
            text.append(d)
        if outage is not None:
            badge = f"Outage #{outage.id}"
            if not outage.start_observed:
                badge += " · start not observed"
            elif not outage.end_observed and not outage.ongoing:
                badge += " · recovery not observed"
            b = label(badge, ("nw-badge",))
            b.set_halign(Gtk.Align.START)
            b.set_ellipsize(Pango.EllipsizeMode.END)
            b.set_margin_top(3)
            text.append(b)
        box.append(text)
        self.duration = label(format_duration(seg.duration), ("nw-duration",), xalign=1.0)
        self.duration.set_valign(Gtk.Align.CENTER)
        self.duration.set_width_chars(7)
        box.append(self.duration)
        self.set_child(box)
        a11y = f"{seg.state.label}, {format_timestamp(seg.start, now)} to {end}, {format_duration(seg.duration, precise=True)}"
        if outage is not None:
            a11y += f", outage {outage.id}"
        self.update_property([Gtk.AccessibleProperty.LABEL], [a11y])


class HistoryPage(Gtk.Box):
    title = "Outage History"
    icon = "document-open-recent-symbolic"

    def __init__(self, app: "NetWatchApplication"):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.app = app
        self.period = "all"
        self.data: Optional[HistoryData] = None
        self.events: list[Segment] = []
        self.filtered: list[Segment] = []
        self.win_start = 0  # index in self.filtered of the first rendered row
        self.rendered = 0  # index in self.filtered after the last rendered row
        self.rows: list[EventRow] = []
        self.day_filter: Optional[DayStats] = None
        self.gen = Generation()
        self.dirty = True
        self.visible_page = False
        self._build_toolbar()
        self._build_content()
        self._install_actions()

    # ------------------------------------------------------------ construction
    def _build_toolbar(self) -> None:
        bar = Gtk.Box(spacing=12)
        bar.add_css_class("nw-toolbar")
        seg = Gtk.Box()
        seg.add_css_class("nw-segmented")
        self.period_buttons: dict[str, Gtk.ToggleButton] = {}
        group = None
        for key, text in PERIODS:
            btn = Gtk.ToggleButton(label=text)
            if group:
                btn.set_group(group)
            group = group or btn
            btn.connect("toggled", self._on_period_toggled, key)
            seg.append(btn)
            self.period_buttons[key] = btn
        self.period_buttons[self.period].set_active(True)
        scroller = Gtk.ScrolledWindow(vscrollbar_policy=Gtk.PolicyType.NEVER, hexpand=True,
                                      propagate_natural_height=True)
        scroller.set_child(seg)
        seg.set_halign(Gtk.Align.START)
        bar.append(scroller)
        self.spinner = Gtk.Spinner()
        bar.append(self.spinner)
        export_menu = Gio.Menu()
        export_menu.append("Export as HTML…", "history.export::html")
        export_menu.append("Export as CSV…", "history.export::csv")
        export_menu.append("Export as Text…", "history.export::txt")
        export = Gtk.MenuButton(icon_name="document-send-symbolic", menu_model=export_menu,
                                tooltip_text="Export or share a report for this period (Ctrl+S)")
        export.add_css_class("nw-icon-button")
        export.update_property([Gtk.AccessibleProperty.LABEL], ["Export report"])
        bar.append(export)
        delete_menu = Gio.Menu()
        delete_menu.append("Delete Selected Event…", "history.delete-selected")
        delete_menu.append("Delete History in This Period…", "history.delete-period")
        delete_menu.append("Clear All History…", "history.clear-all")
        delete = Gtk.MenuButton(icon_name="user-trash-symbolic", menu_model=delete_menu,
                                tooltip_text="Delete history")
        delete.add_css_class("nw-icon-button")
        delete.update_property([Gtk.AccessibleProperty.LABEL], ["Delete history"])
        bar.append(delete)
        self.append(bar)

    def _build_content(self) -> None:
        self.scroller = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True)
        self.scroller.connect("edge-reached", self._on_edge)
        clamp = Adw.Clamp(maximum_size=1240, tightening_threshold=900)
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        content.add_css_class("nw-page-content")
        clamp.set_child(content)
        self.scroller.set_child(clamp)
        self.append(self.scroller)

        # Period summary
        head, self.coverage_label = section_header("Period Summary", "")
        head.set_margin_top(6)
        content.append(head)
        self.banner = Adw.Banner(revealed=False)
        content.append(self.banner)
        self.metrics = flow_box(1, 4, 24)
        self.metrics.set_margin_top(18)
        self.tile_uptime = MetricTile("emblem-ok-symbolic", "Uptime", "nw-good")
        self.tile_down = MetricTile("dialog-warning-symbolic", "Total downtime", "nw-warn")
        self.tile_outages = MetricTile("window-close-symbolic", "Outages")
        self.tile_longest = MetricTile("alarm-symbolic", "Longest outage")
        for tile in (self.tile_uptime, self.tile_down, self.tile_outages, self.tile_longest):
            flow_append(self.metrics, tile)
        self.metrics_bin = responsive_flow(self.metrics, {"wide": 4, "large": 2, "medium": 2, "small": 2, "tiny": 1})
        content.append(self.metrics_bin)

        self.empty = Adw.StatusPage(
            icon_name="network-wireless-symbolic",
            title="No history yet",
            description="NetWatch records connectivity while it is monitoring. Start monitoring from the Overview "
                        "page and this screen will fill in with real measurements.",
        )
        start_btn = Gtk.Button(label="Start Monitoring", halign=Gtk.Align.CENTER)
        start_btn.add_css_class("pill")
        start_btn.add_css_class("suggested-action")
        start_btn.set_action_name("app.start")
        self.empty.set_child(start_btn)
        self.empty.set_visible(False)
        content.append(self.empty)

        # Calendar
        self.calendar_section = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.calendar_section.add_css_class("nw-section")
        head, _ = section_header("Outage Calendar")
        self.calendar_section.append(head)
        cal_scroll = Gtk.ScrolledWindow(vscrollbar_policy=Gtk.PolicyType.NEVER, propagate_natural_height=True)
        self.calendar = CalendarHeatmap()
        self.calendar.on_select = self._on_day_selected
        self.calendar.set_halign(Gtk.Align.START)
        cal_scroll.set_child(self.calendar)
        self.calendar_section.append(cal_scroll)
        legend = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, max_children_per_line=6, column_spacing=16)
        for css, text in (("online", "Online"), ("minor", "Brief outages"), ("no-internet", "No Internet"),
                          ("major", "Major outage"), ("not-connected", "Not Connected"),
                          ("unmonitored-day", "Not monitored")):
            item = legend_item(css, text)
            legend.append(item)
            item.get_parent().set_focusable(False)
        self.calendar_section.append(legend)
        self.calendar_section.append(label("Hover a day for details, click it to show that day's events.",
                                           ("dim-label", "caption"), wrap=True))
        content.append(self.calendar_section)

        # Timeline
        self.timeline_section = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        self.timeline_section.add_css_class("nw-section")
        head, _ = section_header("Event Timeline")
        self.timeline_section.append(head)
        self.timeline = TimelineBar(bar_height=60)
        self.timeline.on_activate = self._on_timeline_activate
        self.timeline_section.append(self.timeline)
        legend = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, max_children_per_line=5, column_spacing=22,
                             row_spacing=6, homogeneous=False)
        for css, text in (("online", "Online"), ("no-internet", "No Internet (network connected)"),
                          ("not-connected", "Not Connected"), ("unknown", "Unknown"),
                          ("unmonitored", "App Not Running / Paused")):
            item = legend_item(css, text)
            legend.append(item)
            item.get_parent().set_focusable(False)
        self.timeline_section.append(legend)
        content.append(self.timeline_section)

        # Event log
        self.log_section = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.log_section.add_css_class("nw-section")
        head_row = Gtk.Box(spacing=12)
        head, _ = section_header("Event Log")
        head.set_hexpand(True)
        head_row.append(head)
        self.count_label = label("", ("dim-label",))
        head_row.append(self.count_label)
        self.log_section.append(head_row)
        filters = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, max_children_per_line=4, column_spacing=8,
                              row_spacing=8, homogeneous=False)
        self.search = Gtk.SearchEntry(placeholder_text="Search events", width_chars=22)
        self.search.connect("search-changed", lambda *_: self._apply_filters())
        self.search.update_property([Gtk.AccessibleProperty.LABEL], ["Search events"])
        self.state_filter = Gtk.DropDown.new_from_strings([f[1] for f in STATE_FILTERS])
        self.state_filter.set_tooltip_text("Filter by state")
        self.state_filter.connect("notify::selected", lambda *_: self._apply_filters())
        self.target_filter = Gtk.DropDown.new_from_strings(["All targets"])
        self.target_filter.set_tooltip_text("Filter by target")
        self.target_filter.connect("notify::selected", lambda *_: self._apply_filters())
        self.target_options: list[Optional[str]] = [None]
        self.day_chip = Gtk.Box(spacing=4, valign=Gtk.Align.CENTER)
        self.day_chip.add_css_class("nw-filter-chip")
        self.day_chip_label = label("")
        self.day_chip.append(self.day_chip_label)
        clear = Gtk.Button(icon_name="window-close-symbolic", tooltip_text="Show all days")
        clear.add_css_class("flat")
        clear.add_css_class("circular")
        clear.connect("clicked", lambda *_: self._on_day_selected(None, from_calendar=False))
        self.day_chip.append(clear)
        self.day_chip.set_visible(False)
        for w in (self.search, self.state_filter, self.target_filter, self.day_chip):
            filters.append(w)
            w.get_parent().set_focusable(False)
        self.log_section.append(filters)
        self.newer_button = Gtk.Button(label="Show Newer Events", halign=Gtk.Align.CENTER, margin_bottom=4)
        self.newer_button.add_css_class("pill")
        self.newer_button.connect("clicked", lambda *_: self._render_newer())
        self.newer_button.set_visible(False)
        self.log_section.append(self.newer_button)
        self.listbox = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE)
        self.listbox.add_css_class("nw-event-list")
        self.listbox.connect("row-activated", self._on_row_activated)
        self.listbox.update_property([Gtk.AccessibleProperty.LABEL], ["Event log"])
        self.log_section.append(self.listbox)
        self.log_empty = label("", ("dim-label",), xalign=0.5, wrap=True)
        self.log_empty.set_margin_top(18)
        self.log_section.append(self.log_empty)
        self.more_button = Gtk.Button(label="Show More", halign=Gtk.Align.CENTER, margin_top=8)
        self.more_button.add_css_class("pill")
        self.more_button.connect("clicked", lambda *_: self._render_more())
        self.log_section.append(self.more_button)
        content.append(self.log_section)

    def _install_actions(self) -> None:
        group = Gio.SimpleActionGroup()
        export = Gio.SimpleAction.new("export", GLib.VariantType.new("s"))
        export.connect("activate", lambda _a, fmt: self.export(fmt.get_string()))
        group.add_action(export)
        for name, cb in (("delete-selected", self._delete_selected), ("delete-period", self._delete_period),
                         ("clear-all", self._clear_all)):
            act = Gio.SimpleAction.new(name, None)
            act.connect("activate", lambda *_a, f=cb: f())
            group.add_action(act)
        self.insert_action_group("history", group)

    # ------------------------------------------------------------ data
    def set_page_visible(self, visible: bool) -> None:
        self.visible_page = visible
        if visible and self.dirty:
            self.refresh()

    def mark_dirty(self) -> None:
        self.dirty = True
        if self.visible_page:
            self.refresh(soft=True)

    def periodic(self) -> None:
        if self.visible_page:
            self.refresh(soft=True)

    def _on_period_toggled(self, btn: Gtk.ToggleButton, key: str) -> None:
        if btn.get_active() and key != self.period:
            self.period = key
            self.day_filter = None
            self.calendar.clear_selection()
            self.refresh()

    def set_period(self, key: str) -> None:
        self.period_buttons[key].set_active(True)

    def custom_range(self) -> Optional[tuple[float, float]]:
        return None

    def refresh(self, soft: bool = False) -> None:
        token = self.gen.next()
        period = self.period
        self.dirty = False
        if not soft:
            self.spinner.start()
        now = time.time()
        run_async(
            lambda: (self.app.stats.history(period, now), self.app.repo.targets_by_id()),
            lambda result: self._apply(token, result, now, soft),
            lambda exc: self._error(token, exc),
        )

    def _error(self, token: int, exc: BaseException) -> None:
        if self.gen.current(token):
            self.spinner.stop()
            self.app.window.toast(f"Could not load history: {exc}")

    def _apply(self, token: int, result, now: float, soft: bool) -> None:
        if not self.gen.current(token):
            return  # a newer request superseded this one
        data, targets = result
        self.spinner.stop()
        prev_keys = [s.key for s in self.events]
        self.data = data
        s = data.summary
        has = s.has_data
        self.empty.set_visible(not has)
        for w in (self.metrics_bin, self.calendar_section, self.timeline_section, self.log_section):
            w.set_visible(has)
        if s.range_duration > 0:
            text = (f"Monitored for {format_duration_long(s.monitored)} out of "
                    f"{format_duration_long(s.range_duration)} ({format_percent(s.coverage_pct, 1)})")
            extra = []
            if s.unknown >= 60:
                extra.append(f"{format_duration_long(s.unknown)} unknown")
            if s.paused >= 60:
                extra.append(f"{format_duration_long(s.paused)} paused")
            if extra:
                text += " · " + ", ".join(extra)
            self.coverage_label.set_label(text)
        if s.uptime_pct is None:
            self.tile_uptime.set("—", "no monitored time", muted=True)
            self.tile_down.set("—", muted=True)
        else:
            self.tile_uptime.set(format_percent(s.uptime_pct), "of monitored time")
            self.tile_down.set(format_duration(s.downtime), "of monitored time offline",
                               suffix=f"({format_percent(s.downtime_pct, 1)})")
        self.tile_outages.set(str(s.outage_count), "distinct outages")
        lo = s.longest_outage
        if lo is None:
            self.tile_longest.set("—", "no outages", muted=True)
        else:
            note = "ongoing" if lo.ongoing else ("completed" if lo.end_observed else "recovery not observed")
            if not lo.start_observed:
                note += ", start not observed"
            self.tile_longest.set(format_duration(lo.duration_in_range), note)
        self.calendar.set_days(data.days)
        self.timeline.set_data(data.segments, s.range_start, s.range_end, now)
        # targets filter options
        addresses = sorted({t.address for t in targets.values()})
        if addresses != self.target_options[1:]:
            current = self.target_options[self.target_filter.get_selected()] if self.target_options else None
            self.target_options = [None, *addresses]
            self.target_filter.set_model(Gtk.StringList.new(["All targets", *addresses]))
            if current in self.target_options:
                self.target_filter.set_selected(self.target_options.index(current))
        self.events = list(reversed(data.segments))
        new_keys = [s.key for s in self.events]
        if soft and new_keys == prev_keys and self.rows:
            by_key = {seg.key: seg for seg in self.events}
            for row in self.rows:  # only durations of ongoing periods change
                seg = by_key.get(row.seg.key)
                if seg is not None:
                    row.seg = seg
                    row.duration.set_label(format_duration(seg.duration))
        else:
            self._apply_filters(keep_selection=True)

    # ------------------------------------------------------------ event log
    def _matches(self, seg: Segment, states, target: Optional[str], text: str) -> bool:
        if states is not None and seg.state not in states:
            return False
        if target is not None and (not seg.targets or target not in seg.targets.split(", ")):
            return False
        if self.day_filter is not None:
            d = self.day_filter
            ds, de = day_start_of(d.day), day_start_of(d.day + timedelta(days=1))
            if seg.end <= ds or seg.start >= de:
                return False
        if text:
            local = seg.local
            hay = " ".join(filter(None, [
                seg.state.label, seg.targets, seg.reason, local.describe() if local else "",
                format_timestamp(seg.start), format_timestamp(seg.end),
                f"outage #{seg.outage_id}" if seg.outage_id else "",
            ])).lower()
            if text not in hay:
                return False
        return True

    def _apply_filters(self, keep_selection: bool = False) -> None:
        selected = self.listbox.get_selected_row()
        selected_key = selected.seg.key if (keep_selection and selected) else None
        states = STATE_FILTERS[self.state_filter.get_selected()][2]
        target = self.target_options[self.target_filter.get_selected()] if self.target_options else None
        text = self.search.get_text().strip().lower()
        self.filtered = [s for s in self.events if self._matches(s, states, target, text)]
        self._reset_window(0)
        total = len(self.events)
        shown = len(self.filtered)
        self.count_label.set_label(f"{shown} of {total} events" if shown != total else f"{total} events")
        if not self.filtered:
            self.log_empty.set_label("No events match the current filters." if self.events else "No events recorded.")
        self.log_empty.set_visible(not self.filtered)
        if selected_key:
            self._select_key(selected_key, scroll=False)

    def _make_row(self, seg: Segment, now: float) -> EventRow:
        outage = self.data.outages_by_id.get(seg.outage_id) if seg.outage_id else None
        return EventRow(seg, outage, now)

    def _reset_window(self, start: int) -> None:
        """Render a page of rows starting at ``start`` (only a window of the list is ever built)."""
        self.listbox.remove_all()
        self.rows = []
        self.win_start = self.rendered = max(0, min(start, len(self.filtered)))
        self._render_more()

    def _update_pager(self) -> None:
        remaining = len(self.filtered) - self.rendered
        self.more_button.set_visible(remaining > 0)
        self.more_button.set_label(f"Show More ({remaining} remaining)")
        self.newer_button.set_visible(self.win_start > 0)
        self.newer_button.set_label(f"Show Newer Events ({self.win_start})")

    def _render_more(self) -> None:
        if self.data is None:
            return
        now = time.time()
        end = min(len(self.filtered), self.rendered + PAGE_SIZE)
        for seg in self.filtered[self.rendered:end]:
            row = self._make_row(seg, now)
            self.listbox.append(row)
            self.rows.append(row)
        self.rendered = end
        self._update_pager()

    def _render_newer(self) -> None:
        if self.data is None or self.win_start == 0:
            return
        now = time.time()
        start = max(0, self.win_start - PAGE_SIZE)
        for i, seg in enumerate(self.filtered[start:self.win_start]):
            row = self._make_row(seg, now)
            self.listbox.insert(row, i)
            self.rows.insert(i, row)
        self.win_start = start
        self._update_pager()

    def _on_edge(self, scroller, pos) -> None:
        if pos == Gtk.PositionType.BOTTOM and self.rendered < len(self.filtered):
            self._render_more()

    def _select_key(self, key: str, scroll: bool = True) -> bool:
        idx = next((i for i, s in enumerate(self.filtered) if s.key == key), None)
        if idx is None:
            return False
        if not (self.win_start <= idx < self.rendered):
            if self.rendered <= idx < self.rendered + PAGE_SIZE:
                self._render_more()  # just past the end: extend
            else:
                self._reset_window(idx - JUMP_CONTEXT)  # far away: jump the window, never build thousands of rows
        row = self.rows[idx - self.win_start]
        self.listbox.select_row(row)
        if scroll:
            GLib.idle_add(lambda: (row.grab_focus(), GLib.SOURCE_REMOVE)[1])
        return True

    def _on_timeline_activate(self, seg: Segment) -> None:
        if not self._select_key(seg.key):
            # make sure the event is visible: reset filters, then select
            self.search.set_text("")
            self.state_filter.set_selected(0)
            self.target_filter.set_selected(0)
            self._on_day_selected(None, from_calendar=False)
            self._select_key(seg.key)

    def _on_day_selected(self, day: Optional[DayStats], from_calendar: bool = True) -> None:
        self.day_filter = day
        if not from_calendar:
            self.calendar.clear_selection()
        if day is not None:
            self.day_chip_label.set_label(day.day.strftime("%a, %b ") + str(day.day.day))
        self.day_chip.set_visible(day is not None)
        self._apply_filters()

    def _on_row_activated(self, listbox, row: EventRow) -> None:
        self.timeline.select(row.seg)
        show_event_detail(self.app, self, row.seg)

    # ------------------------------------------------------------ export / delete
    def export(self, fmt: Optional[str] = None) -> None:
        self.app.window.start_export(period=self.period, fmt=fmt)

    def _delete_selected(self) -> None:
        row = self.listbox.get_selected_row()
        if row is None:
            dialogs.message(self, "No event selected", "Select an event in the Event Log first.")
            return
        seg = row.seg
        if seg.synthetic or seg.ongoing or seg.state_id is None:
            dialogs.message(self, "Cannot delete this event",
                            "Only completed, recorded events can be deleted. Periods when NetWatch was not "
                            "running have no records, and the current state is still being recorded.")
            return

        def do() -> None:
            run_async(lambda: self.app.repo.delete_state(seg.state_id), lambda _r: self._after_delete("Event deleted"))

        dialogs.confirm(
            self, "Delete this event?",
            f"The {seg.state.label} period from {format_timestamp(seg.start)} to {format_timestamp(seg.end)} and its "
            "measurements will be permanently removed. This cannot be undone.",
            "Delete", do,
        )

    def _delete_period(self) -> None:
        if self.data is None:
            return
        s = self.data.summary
        start, end = s.range_start, s.range_end

        def do() -> None:
            run_async(lambda: self.app.repo.delete_range(start, end),
                      lambda r: self._after_delete(f"Deleted {r['states']} events and {r['outages']} outages"))

        dialogs.confirm(
            self, "Delete history in this period?",
            f"All completed records between {format_timestamp(start)} and {format_timestamp(end)} will be permanently "
            "deleted, including measurements and outages that lie entirely inside the period. The current state "
            "is kept. Consider exporting a report or a backup first.",
            "Delete Period", do,
        )

    def _clear_all(self) -> None:
        def do() -> None:
            keep = self.app.engine.session_id

            def work():
                self.app.repo.delete_all(keep, time.time())

            run_async(work, lambda _r: self._after_delete("All history cleared"))

        dialogs.confirm(
            self, "Clear all history?",
            "Every recorded event, outage, measurement and monitoring session will be permanently deleted. "
            "Monitoring continues and starts a fresh record now. This cannot be undone.",
            "Clear All History", do,
        )

    def _after_delete(self, message: str) -> None:
        self.app.window.toast(message)
        self.app.history_changed()
