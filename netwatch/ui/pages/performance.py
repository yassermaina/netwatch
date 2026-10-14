"""Performance: latency and packet loss over time, per-target comparison."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from gi.repository import Adw, Gtk

from netwatch.ui.background import Generation, run_async
from netwatch.ui.palette import SERIES
from netwatch.ui.widgets.charts import LatencyChart, LossChart
from netwatch.ui.widgets.common import MetricTile, flow_append, flow_box, label, responsive_flow, section_header

if TYPE_CHECKING:
    from netwatch.ui.application import NetWatchApplication

RANGES = [("15m", "15 Minutes", 900), ("1h", "1 Hour", 3600), ("24h", "24 Hours", 86400), ("7d", "7 Days", 7 * 86400)]


def _ms(v) -> str:
    return f"{v:.1f} ms" if v is not None else "—"


class PerformancePage(Gtk.Box):
    title = "Performance"
    icon = "power-profile-performance-symbolic"

    def __init__(self, app: "NetWatchApplication"):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.app = app
        self.range_key = "1h"
        self.gen = Generation()
        self.visible_page = False

        bar = Gtk.Box(spacing=12)
        bar.add_css_class("nw-toolbar")
        seg = Gtk.Box()
        seg.add_css_class("nw-segmented")
        group = None
        self.range_buttons = {}
        for key, text, _ in RANGES:
            b = Gtk.ToggleButton(label=text)
            if group:
                b.set_group(group)
            group = group or b
            b.connect("toggled", self._on_range, key)
            seg.append(b)
            self.range_buttons[key] = b
        self.range_buttons[self.range_key].set_active(True)
        sc = Gtk.ScrolledWindow(vscrollbar_policy=Gtk.PolicyType.NEVER, hexpand=True, propagate_natural_height=True)
        sc.set_child(seg)
        seg.set_halign(Gtk.Align.START)
        bar.append(sc)
        self.spinner = Gtk.Spinner()
        bar.append(self.spinner)
        self.append(bar)

        scroller = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True)
        clamp = Adw.Clamp(maximum_size=1240, tightening_threshold=900)
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        content.add_css_class("nw-page-content")
        clamp.set_child(content)
        scroller.set_child(clamp)
        self.append(scroller)

        head, self.summary_caption = section_header("Latency Summary", "")
        content.append(head)
        self.tiles = flow_box(2, 6, 18)
        self.tiles.set_margin_top(12)
        self.t_avg = MetricTile("network-transmit-receive-symbolic", "Average", small=True)
        self.t_med = MetricTile("network-transmit-receive-symbolic", "Median", small=True)
        self.t_min = MetricTile("go-down-symbolic", "Minimum", small=True)
        self.t_max = MetricTile("go-up-symbolic", "Maximum", small=True)
        self.t_loss = MetricTile("network-error-symbolic", "Packet loss", small=True)
        self.t_checks = MetricTile("view-list-symbolic", "Checks", small=True)
        for t in (self.t_avg, self.t_med, self.t_min, self.t_max, self.t_loss, self.t_checks):
            card = Gtk.Box()
            card.add_css_class("nw-card")
            card.append(t)
            flow_append(self.tiles, card)
        content.append(responsive_flow(self.tiles, {"wide": 6, "large": 3, "medium": 3, "small": 3, "tiny": 2}))

        head, _ = section_header(
            "Latency over time",
            "Average per interval with min–max band. Gaps mean no "
            "successful measurement; red ticks mark failed checks. Missing data is never drawn "
            "as zero.",
        )
        head.add_css_class("nw-section")
        content.append(head)
        frame = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, margin_top=10)
        frame.add_css_class("nw-chart-frame")
        self.latency = LatencyChart(260)
        frame.append(self.latency)
        self.legend = Gtk.FlowBox(
            selection_mode=Gtk.SelectionMode.NONE, max_children_per_line=6, column_spacing=16, homogeneous=False
        )
        frame.append(self.legend)
        self.lat_inspect = label(
            "Hover over the chart, or focus it and use the arrow keys, to inspect values.",
            ("dim-label", "caption"),
            wrap=True,
        )
        self.latency.on_inspect = lambda t: self.lat_inspect.set_label(
            t or "Hover over the chart, or focus it and use the arrow keys, to inspect values."
        )
        frame.append(self.lat_inspect)
        content.append(frame)

        head, _ = section_header("Packet loss over time", "Share of failed checks across all targets per interval.")
        head.add_css_class("nw-section")
        content.append(head)
        frame = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, margin_top=10)
        frame.add_css_class("nw-chart-frame")
        self.loss = LossChart(160)
        frame.append(self.loss)
        self.loss_inspect = label("", ("dim-label", "caption"), wrap=True)
        self.loss.on_inspect = self.loss_inspect.set_label
        frame.append(self.loss_inspect)
        content.append(frame)

        head, _ = section_header("Per-target comparison")
        head.add_css_class("nw-section")
        content.append(head)
        self.table = Gtk.Grid(column_spacing=18, row_spacing=8, margin_top=12)
        tscroll = Gtk.ScrolledWindow(vscrollbar_policy=Gtk.PolicyType.NEVER, propagate_natural_height=True)
        tscroll.set_child(self.table)
        content.append(tscroll)

    def set_page_visible(self, visible: bool) -> None:
        self.visible_page = visible
        if visible:
            self.refresh()

    def periodic(self) -> None:
        if self.visible_page:
            self.refresh(soft=True)

    def _on_range(self, btn, key: str) -> None:
        if btn.get_active() and key != self.range_key:
            self.range_key = key
            self.refresh()

    def refresh(self, soft: bool = False) -> None:
        token = self.gen.next()
        span = next(s for k, _, s in RANGES if k == self.range_key)
        now = time.time()
        if not soft:
            self.spinner.start()
        run_async(
            lambda: (self.app.stats.performance(now - span, now, buckets=180), self.app.repo.targets_by_id()),
            lambda r: self._apply(token, r),
        )

    def _apply(self, token: int, result) -> None:
        if not self.gen.current(token):
            return
        self.spinner.stop()
        perf, targets = result
        o = perf["overall"]
        muted = o["checks"] == 0
        self.t_avg.set(_ms(o["avg"]), muted=o["avg"] is None)
        self.t_med.set(_ms(o["median"]), muted=o["median"] is None)
        self.t_min.set(_ms(o["min"]), muted=o["min"] is None)
        self.t_max.set(_ms(o["max"]), f"p95 {_ms(o['p95'])}" if o["p95"] is not None else "", muted=o["max"] is None)
        self.t_loss.set(f"{o['loss_pct']:.1f}%" if o["loss_pct"] is not None else "—", muted=muted)
        self.t_checks.set(str(o["checks"]), f"{o['checks'] - o['ok']} failed" if o["checks"] else "", muted=muted)
        self.summary_caption.set_label(
            "No measurements in this range. Start monitoring to collect data."
            if muted
            else f"Based on {o['ok']} successful replies. Jitter (std. dev.) {_ms(o['jitter'])}."
        )
        series = []
        while (child := self.legend.get_first_child()) is not None:
            self.legend.remove(child)
        ordered = sorted(perf["series"].items(), key=lambda kv: targets[kv[0]].position if kv[0] in targets else 99)
        for i, (tid, pts) in enumerate(ordered):
            t = targets.get(tid)
            color = SERIES[i % len(SERIES)]
            name = t.name if t else f"target {tid}"
            series.append({"name": name, "color": color, "points": pts})
            item = Gtk.Box(spacing=6)
            swatch = Gtk.DrawingArea(content_width=12, content_height=12, valign=Gtk.Align.CENTER)
            swatch.set_draw_func(
                lambda _a, cr, w, h, c=color: (cr.set_source_rgb(*c), cr.arc(w / 2, h / 2, 5, 0, 6.3), cr.fill())
            )
            item.append(swatch)
            item.append(label(f"{name} ({t.address})" if t else name))
            self.legend.append(item)
        self.latency.set_data(
            perf["start"],
            perf["end"],
            perf["bucket"],
            series,
            interval=float(self.app.settings.get("interval_seconds")),
        )
        merged: dict[int, dict] = {}
        for pts in perf["series"].values():
            for p in pts:
                key = round(p["t"], 3)
                m = merged.setdefault(key, {"t": p["t"], "n": 0, "ok": 0})
                m["n"] += p["n"]
                m["ok"] += p["ok"]
        self.loss.set_data(perf["start"], perf["end"], perf["bucket"], list(merged.values()))
        # table
        while (child := self.table.get_first_child()) is not None:
            self.table.remove(child)
        headers = ["Target", "Average", "Median", "Min", "Max", "p95", "Loss", "Checks"]
        for col, h in enumerate(headers):
            self.table.attach(label(h, ("dim-label", "nw-table-header"), xalign=0 if col == 0 else 1), col, 0, 1, 1)
        rows = sorted(perf["per_target"].items(), key=lambda kv: targets[kv[0]].position if kv[0] in targets else 99)
        if not rows:
            self.table.attach(label("No data for this range.", ("dim-label",)), 0, 1, len(headers), 1)
        for r, (tid, p) in enumerate(rows, start=1):
            t = targets.get(tid)
            cells = [
                t.display if t else str(tid),
                _ms(p["avg"]),
                _ms(p["median"]),
                _ms(p["min"]),
                _ms(p["max"]),
                _ms(p["p95"]),
                f"{p['loss_pct']:.1f}%" if p["loss_pct"] is not None else "—",
                str(p["checks"]),
            ]
            for col, text in enumerate(cells):
                self.table.attach(label(text, ("nw-num",), xalign=0 if col == 0 else 1), col, r, 1, 1)
