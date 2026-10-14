"""Lightweight Cairo charts: latency lines and packet-loss bars.

Missing observations are never drawn as zero: line series break across
buckets without successful measurements, failed checks are marked separately,
and buckets without any checks are left empty.
"""

from __future__ import annotations

import bisect
import math
from typing import Callable, Optional

from gi.repository import Gdk, Gtk, PangoCairo

from netwatch.timefmt import format_axis_label, format_clock, format_timestamp
from netwatch.ui import palette


def nice_ceiling(value: float) -> float:
    if value <= 0:
        return 1.0
    exp = 10 ** math.floor(math.log10(value))
    for m in (1, 2, 2.5, 5, 10):
        if value <= m * exp:
            return m * exp
    return 10 * exp


class _ChartBase(Gtk.DrawingArea):
    PAD_L, PAD_R, PAD_T, PAD_B = 52, 14, 12, 26

    def __init__(self, height: int = 220, unit: str = ""):
        super().__init__()
        self.unit = unit
        self.start = 0.0
        self.end = 1.0
        self.hover_t: Optional[float] = None
        self.on_inspect: Optional[Callable[[str], None]] = None
        self.set_content_height(height)
        self.set_hexpand(True)
        self.set_focusable(True)
        self.set_draw_func(self._draw)
        motion = Gtk.EventControllerMotion()
        motion.connect("motion", self._on_motion)
        motion.connect("leave", self._on_leave)
        self.add_controller(motion)
        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self._on_key)
        self.add_controller(keys)

    def _plot_rect(self, width: int, height: int) -> tuple[float, float, float, float]:
        return self.PAD_L, self.PAD_T, max(10, width - self.PAD_L - self.PAD_R), max(10, height - self.PAD_T - self.PAD_B)

    def _x(self, t: float, px: float, pw: float) -> float:
        return px + (t - self.start) / max(1e-9, self.end - self.start) * pw

    def _text(self, cr, text: str, x: float, y: float, color, align: str = "left", alpha: float = 1.0) -> None:
        layout = self.create_pango_layout(text)
        tw, th = layout.get_pixel_size()
        if align == "right":
            x -= tw
        elif align == "center":
            x -= tw / 2
        cr.set_source_rgba(*color, alpha)
        cr.move_to(x, y - th / 2)
        PangoCairo.show_layout(cr, layout)

    def _axes(self, cr, width: int, height: int, ymax: float, fmt: Callable[[float], str]) -> None:
        c = palette.colors()
        px, py, pw, ph = self._plot_rect(width, height)
        cr.set_line_width(1)
        for i in range(5):
            y = py + ph - ph * i / 4
            cr.set_source_rgba(*c["grid"], 0.07 if i else 0.18)
            cr.move_to(px, int(y) + 0.5)
            cr.line_to(px + pw, int(y) + 0.5)
            cr.stroke()
            self._text(cr, fmt(ymax * i / 4), px - 8, y, c["muted"], "right")
        span = self.end - self.start
        n = 5 if pw > 420 else 3
        for i in range(n):
            t = self.start + span * i / (n - 1)
            align = "left" if i == 0 else "right" if i == n - 1 else "center"
            self._text(cr, format_axis_label(t, span), self._x(t, px, pw), py + ph + 14, c["muted"], align)

    def _on_motion(self, ctrl, x, y) -> None:
        px, py, pw, ph = self._plot_rect(self.get_width(), self.get_height())
        if px <= x <= px + pw:
            self.hover_t = self.start + (x - px) / pw * (self.end - self.start)
        else:
            self.hover_t = None
        self._emit_inspect()
        self.queue_draw()

    def _on_leave(self, ctrl) -> None:
        self.hover_t = None
        self._emit_inspect()
        self.queue_draw()

    def _on_key(self, ctrl, keyval, keycode, state) -> bool:
        step = (self.end - self.start) / 60
        if keyval in (Gdk.KEY_Left, Gdk.KEY_Right):
            if self.hover_t is None:
                self.hover_t = self.end - step / 2
            self.hover_t += -step if keyval == Gdk.KEY_Left else step
            self.hover_t = max(self.start, min(self.end, self.hover_t))
            self._emit_inspect()
            self.queue_draw()
            return True
        return False

    def _emit_inspect(self) -> None:
        if self.on_inspect:
            text = self.inspect_text(self.hover_t) if self.hover_t is not None else ""
            self.on_inspect(text)
            if text:
                self.update_property([Gtk.AccessibleProperty.LABEL], [text])

    def inspect_text(self, t: float) -> str:
        return ""


class LatencyChart(_ChartBase):
    """One line per target (bucket averages) with a min–max band and failure ticks."""

    def __init__(self, height: int = 240):
        super().__init__(height, "ms")
        self.series: list[dict] = []
        self.bucket = 60.0
        self.max_gap = 150.0
        self.update_property([Gtk.AccessibleProperty.LABEL], ["Latency over time"])

    def set_data(self, start: float, end: float, bucket: float, series: list[dict], interval: float = 0) -> None:
        """series: [{name, color, points: [{t, n, ok, avg, min, max}]}]; ``interval`` is the check interval."""
        self.start, self.end, self.bucket = start, end, bucket
        # a gap is "missing data" only if longer than both the bucket and the check cadence allow
        self.max_gap = 2.5 * max(bucket, interval)
        self.series = series
        for s in series:
            s["_ts"] = [p["t"] for p in s["points"]]
        total = sum(len(s["points"]) for s in series)
        self.update_property([Gtk.AccessibleProperty.DESCRIPTION],
                             [f"Latency chart with {len(series)} targets and {total} data points. "
                              "Use left and right arrow keys to inspect values."])
        self.queue_draw()

    def _draw(self, area, cr, width: int, height: int) -> None:
        c = palette.colors()
        values = [p["max"] or p["avg"] for s in self.series for p in s["points"] if p["avg"] is not None]
        ymax = nice_ceiling(max(values) * 1.1) if values else 100.0
        self._axes(cr, width, height, ymax, lambda v: f"{v:.0f} ms")
        px, py, pw, ph = self._plot_rect(width, height)
        if not values and not any(s["points"] for s in self.series):
            self._text(cr, "No measurements in this period", px + pw / 2, py + ph / 2, c["muted"], "center")
            return

        def y_of(v: float) -> float:
            return py + ph - min(1.0, v / ymax) * ph

        gap = self.max_gap
        for s in self.series:
            color = s["color"]
            pts = s["points"]
            # min-max band
            cr.set_source_rgba(*color, 0.12)
            for p in pts:
                if p["avg"] is None or p["min"] is None:
                    continue
                x = self._x(p["t"], px, pw)
                bw = max(1.0, self.bucket / (self.end - self.start) * pw)
                cr.rectangle(x - bw / 2, y_of(p["max"]), bw, max(1.0, y_of(p["min"]) - y_of(p["max"])))
            cr.fill()
            # average line, broken across missing data
            cr.set_source_rgba(*color, 0.95)
            cr.set_line_width(1.8)
            prev_t = None
            for p in pts:
                if p["avg"] is None:
                    prev_t = None
                    continue
                x, y = self._x(p["t"], px, pw), y_of(p["avg"])
                if prev_t is None or p["t"] - prev_t > gap:
                    cr.stroke()
                    cr.move_to(x, y)
                    cr.arc(x, y, 1.2, 0, 2 * math.pi)
                    cr.move_to(x, y)
                else:
                    cr.line_to(x, y)
                prev_t = p["t"]
            cr.stroke()
        # failed checks (any target) as red ticks along the bottom
        cr.set_source_rgba(*c["error"], 0.85)
        for s in self.series:
            for p in s["points"]:
                if p["ok"] < p["n"]:
                    x = self._x(p["t"], px, pw)
                    cr.rectangle(x - 1, py + ph - 6, 2, 6)
        cr.fill()
        if self.hover_t is not None:
            x = self._x(self.hover_t, px, pw)
            cr.set_source_rgba(*c["fg"], 0.5)
            cr.set_line_width(1)
            cr.move_to(int(x) + 0.5, py)
            cr.line_to(int(x) + 0.5, py + ph)
            cr.stroke()
            for s in self.series:
                p = self._nearest(s)
                if p and p["avg"] is not None:
                    cr.set_source_rgba(*s["color"], 1)
                    cr.arc(self._x(p["t"], px, pw), y_of(p["avg"]), 3.5, 0, 2 * math.pi)
                    cr.fill()

    def _nearest(self, s: dict) -> Optional[dict]:
        if self.hover_t is None or not s["points"]:
            return None
        ts = s["_ts"]
        i = bisect.bisect_left(ts, self.hover_t)
        best = min((j for j in (i - 1, i) if 0 <= j < len(ts)), key=lambda j: abs(ts[j] - self.hover_t))
        return s["points"][best] if abs(ts[best] - self.hover_t) <= self.max_gap / 2 else None

    def inspect_text(self, t: float) -> str:
        parts = []
        for s in self.series:
            p = self._nearest(s)
            if p is None:
                parts.append(f"{s['name']}: no data")
            elif p["avg"] is None:
                parts.append(f"{s['name']}: all {p['n']} checks failed")
            else:
                fail = f", {p['n'] - p['ok']} failed" if p["ok"] < p["n"] else ""
                parts.append(f"{s['name']}: {p['avg']:.1f} ms (min {p['min']:.1f}, max {p['max']:.1f}{fail})")
        span = self.end - self.start
        when = format_clock(t, seconds=span <= 7200) if span <= 2 * 86400 else format_timestamp(t)
        return f"{when} — " + " · ".join(parts)


class LossChart(_ChartBase):
    """Packet loss per time bucket across all targets."""

    def __init__(self, height: int = 150):
        super().__init__(height, "%")
        self.buckets: list[dict] = []
        self._ts: list[float] = []
        self.bucket = 60.0
        self.update_property([Gtk.AccessibleProperty.LABEL], ["Packet loss over time"])

    def set_data(self, start: float, end: float, bucket: float, buckets: list[dict]) -> None:
        self.start, self.end, self.bucket = start, end, bucket
        self.buckets = sorted(buckets, key=lambda b: b["t"])
        self._ts = [b["t"] for b in self.buckets]
        lossy = sum(1 for b in self.buckets if b["ok"] < b["n"])
        self.update_property([Gtk.AccessibleProperty.DESCRIPTION],
                             [f"Packet loss chart, {len(self.buckets)} intervals with data, {lossy} with loss."])
        self.queue_draw()

    def _draw(self, area, cr, width: int, height: int) -> None:
        c = palette.colors()
        self._axes(cr, width, height, 100.0, lambda v: f"{v:.0f}%")
        px, py, pw, ph = self._plot_rect(width, height)
        if not self.buckets:
            self._text(cr, "No measurements in this period", px + pw / 2, py + ph / 2, c["muted"], "center")
            return
        bw = max(1.0, self.bucket / (self.end - self.start) * pw - 1)
        for b in self.buckets:
            x = self._x(b["t"], px, pw) - bw / 2
            loss = (b["n"] - b["ok"]) / b["n"] * 100 if b["n"] else 0
            # a faint baseline shows "measured, no loss" vs. empty "no data"
            cr.set_source_rgba(*c["online"], 0.35)
            cr.rectangle(x, py + ph - 2, bw, 2)
            cr.fill()
            if loss > 0:
                cr.set_source_rgba(*(c["no_internet"] if loss < 100 else c["error"]), 0.9)
                h = max(2.0, loss / 100 * ph)
                cr.rectangle(x, py + ph - h, bw, h)
                cr.fill()
        if self.hover_t is not None:
            x = self._x(self.hover_t, px, pw)
            cr.set_source_rgba(*c["fg"], 0.5)
            cr.move_to(int(x) + 0.5, py)
            cr.line_to(int(x) + 0.5, py + ph)
            cr.stroke()

    def inspect_text(self, t: float) -> str:
        if not self._ts:
            return ""
        i = bisect.bisect_left(self._ts, t)
        cands = [j for j in (i - 1, i) if 0 <= j < len(self._ts)]
        j = min(cands, key=lambda k: abs(self._ts[k] - t))
        span = self.end - self.start
        when = format_clock(t, seconds=span <= 7200) if span <= 2 * 86400 else format_timestamp(t)
        if abs(self._ts[j] - t) > self.bucket:
            return f"{when} — no checks recorded"
        b = self.buckets[j]
        loss = (b["n"] - b["ok"]) / b["n"] * 100
        return f"{when} — {loss:.1f}% loss ({b['n'] - b['ok']} of {b['n']} checks failed)"
