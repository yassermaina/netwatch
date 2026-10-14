"""Horizontal event timeline bar (Cairo).

Rendering aggregates segments into pixel columns so very long histories stay
fast, while hover/click always resolve to the exact stored segment.
"""

from __future__ import annotations

import bisect
from typing import Callable, Optional

from gi.repository import Gdk, Gtk, PangoCairo

from netwatch.models import DOWN_STATES, ConnState
from netwatch.stats import Segment
from netwatch.timefmt import format_axis_label, format_duration, format_timestamp
from netwatch.ui import palette

UNMONITORED = (ConnState.NOT_RUNNING, ConnState.PAUSED)


def column_states(segments: list[Segment], start: float, end: float, columns: int) -> list[Optional[ConnState]]:
    """Representative state per pixel column.

    Any downtime inside a column wins (so a 20 s outage in a 30-day view is
    still visible as a thin line, as in the reference design); otherwise the
    state covering most of the column is used.
    """
    span = max(1e-9, end - start)
    width = span / columns
    out: list[Optional[ConnState]] = [None] * columns
    acc: list[dict] = [dict() for _ in range(columns)]
    for seg in segments:
        s, e = max(seg.start, start), min(seg.end, end)
        if e <= s:
            continue
        c0 = max(0, min(columns - 1, int((s - start) / width)))
        c1 = max(0, min(columns - 1, int((e - start) / width - 1e-9)))
        for c in range(c0, c1 + 1):
            cs = start + c * width
            overlap = min(e, cs + width) - max(s, cs)
            if overlap > 0:
                acc[c][seg.state] = acc[c].get(seg.state, 0.0) + overlap
    for c, d in enumerate(acc):
        if not d:
            continue
        downs = {k: v for k, v in d.items() if k in DOWN_STATES}
        if downs:
            out[c] = max(downs, key=downs.get)
        else:
            out[c] = max(d, key=d.get)
    return out


class TimelineBar(Gtk.DrawingArea):
    def __init__(self, bar_height: int = 56, show_axis: bool = True, compact: bool = False):
        super().__init__()
        self.bar_height = bar_height
        self.show_axis = show_axis
        self.compact = compact
        self.segments: list[Segment] = []
        self._starts: list[float] = []
        self.start = 0.0
        self.end = 1.0
        self.now: Optional[float] = None
        self.hover_x: Optional[float] = None
        self.selected_key: Optional[str] = None
        self.cursor: Optional[int] = None
        self.on_activate: Optional[Callable[[Segment], None]] = None
        self.set_content_height(bar_height + (24 if show_axis else 0))
        self.set_hexpand(True)
        self.set_focusable(True)
        self.set_draw_func(self._draw)
        self.set_has_tooltip(True)
        self.connect("query-tooltip", self._on_tooltip)
        motion = Gtk.EventControllerMotion()
        motion.connect("motion", self._on_motion)
        motion.connect("leave", self._on_leave)
        self.add_controller(motion)
        click = Gtk.GestureClick()
        click.connect("released", self._on_click)
        self.add_controller(click)
        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self._on_key)
        self.add_controller(keys)
        self.update_property([Gtk.AccessibleProperty.LABEL], ["Event timeline"])

    # ------------------------------------------------------------ data
    def set_data(self, segments: list[Segment], start: float, end: float, now: Optional[float] = None) -> None:
        self.segments = segments
        self._starts = [s.start for s in segments]
        self.start, self.end = start, max(end, start + 1)
        self.now = now
        self.cursor = None
        downs = sum(1 for s in segments if s.state in DOWN_STATES)
        self.update_property(
            [Gtk.AccessibleProperty.DESCRIPTION],
            [
                f"{len(segments)} periods from {format_timestamp(start)} to {format_timestamp(end)}, "
                f"{downs} without connectivity. Use left and right arrow keys to inspect periods, Enter to select."
            ],
        )
        self.queue_draw()

    def segment_at_time(self, t: float) -> Optional[Segment]:
        i = bisect.bisect_right(self._starts, t) - 1
        if 0 <= i < len(self.segments) and self.segments[i].start <= t <= self.segments[i].end:
            return self.segments[i]
        return None

    def _time_at(self, x: float) -> float:
        w = max(1, self.get_width())
        return self.start + (x / w) * (self.end - self.start)

    # ------------------------------------------------------------ drawing
    def _draw(self, area, cr, width: int, height: int) -> None:
        c = palette.colors()
        bh = self.bar_height
        radius = 8 if not self.compact else 5
        palette.rounded_rect(cr, 0, 0, width, bh, radius)
        cr.clip()
        cols = column_states(self.segments, self.start, self.end, max(1, width))
        run_start, run_state = 0, cols[0] if cols else None
        for x in range(1, len(cols) + 1):
            st = cols[x] if x < len(cols) else object()
            if st != run_state:
                self._fill_run(cr, run_state, run_start, x, bh, c)
                run_start, run_state = x, st
        if self.selected_key:
            for seg in self.segments:
                if seg.key == self.selected_key:
                    x0 = (seg.start - self.start) / (self.end - self.start) * width
                    x1 = (seg.end - self.start) / (self.end - self.start) * width
                    cr.set_source_rgba(*c["accent"], 0.9)
                    cr.set_line_width(2.5)
                    cr.rectangle(x0 + 1.25, 1.25, max(2, x1 - x0) - 2.5, bh - 2.5)
                    cr.stroke()
                    break
        if self.hover_x is not None:
            cr.set_source_rgba(*c["fg"], 0.85)
            cr.rectangle(int(self.hover_x), 0, 1.5, bh)
            cr.fill()
        cr.reset_clip()
        if self.has_focus() and self.cursor is not None and 0 <= self.cursor < len(self.segments):
            seg = self.segments[self.cursor]
            x0 = (seg.start - self.start) / (self.end - self.start) * width
            cr.set_source_rgba(*c["accent"], 1)
            cr.rectangle(max(0, x0 - 1), bh + 1, 3, 4)
            cr.fill()
        if self.show_axis:
            self._draw_axis(cr, width, bh, c)

    def _fill_run(self, cr, state, x0: int, x1: int, bh: int, c: dict) -> None:
        if state is None or state in UNMONITORED:
            palette.hatch(cr, x0, 0, x1 - x0, bh, alpha=1.0 if state else 0.6)
            return
        cr.set_source_rgb(*palette.state_color(state))
        cr.rectangle(x0, 0, x1 - x0, bh)
        cr.fill()

    def _draw_axis(self, cr, width: int, bh: int, c: dict) -> None:
        span = self.end - self.start
        n = 5 if width > 520 else 3 if width > 260 else 2
        for i in range(n):
            frac = i / (n - 1)
            t = self.start + frac * span
            text = (
                "now"
                if i == n - 1 and self.now is not None and abs(self.end - self.now) < 120
                else format_axis_label(t, span)
            )
            layout = self.create_pango_layout(text)
            tw, th = layout.get_pixel_size()
            x = frac * width - (tw if i == n - 1 else 0 if i == 0 else tw / 2)
            cr.set_source_rgba(*c["muted"], 1)
            cr.move_to(max(0, min(width - tw, x)), bh + 6)
            PangoCairo.show_layout(cr, layout)

    # ------------------------------------------------------------ interaction
    def describe(self, seg: Segment) -> str:
        end = "now" if seg.ongoing else format_timestamp(seg.end, seconds=seg.duration < 600)
        text = (
            f"{seg.state.label}\n{format_timestamp(seg.start, seconds=seg.duration < 600)} – {end}\n"
            f"Duration: {format_duration(seg.duration, precise=True)}"
        )
        if seg.targets and seg.state == ConnState.ONLINE:
            text += f"\nResponding: {seg.targets}"
        if seg.reason and seg.state != ConnState.ONLINE:
            text += f"\n{seg.reason}"
        return text

    def _on_tooltip(self, widget, x, y, keyboard, tooltip) -> bool:
        if keyboard and self.cursor is not None and 0 <= self.cursor < len(self.segments):
            tooltip.set_text(self.describe(self.segments[self.cursor]))
            return True
        if y > self.bar_height:
            return False
        seg = self.segment_at_time(self._time_at(x))
        if seg is None:
            tooltip.set_text("No records")
            return True
        tooltip.set_text(self.describe(seg))
        return True

    def _on_motion(self, ctrl, x, y) -> None:
        self.hover_x = x if y <= self.bar_height else None
        self.queue_draw()

    def _on_leave(self, ctrl) -> None:
        self.hover_x = None
        self.queue_draw()

    def _on_click(self, gesture, n_press, x, y) -> None:
        self.grab_focus()
        seg = self.segment_at_time(self._time_at(x))
        if seg is not None:
            self.select(seg)
            if self.on_activate:
                self.on_activate(seg)

    def select(self, seg: Optional[Segment]) -> None:
        self.selected_key = seg.key if seg else None
        if seg is not None and seg in self.segments:
            self.cursor = self.segments.index(seg)
        self.queue_draw()

    def _on_key(self, ctrl, keyval, keycode, state) -> bool:
        if not self.segments:
            return False
        if keyval in (Gdk.KEY_Left, Gdk.KEY_Right, Gdk.KEY_Home, Gdk.KEY_End):
            if self.cursor is None:
                self.cursor = len(self.segments) - 1
            elif keyval == Gdk.KEY_Left:
                self.cursor = max(0, self.cursor - 1)
            elif keyval == Gdk.KEY_Right:
                self.cursor = min(len(self.segments) - 1, self.cursor + 1)
            elif keyval == Gdk.KEY_Home:
                self.cursor = 0
            else:
                self.cursor = len(self.segments) - 1
            seg = self.segments[self.cursor]
            self.update_property([Gtk.AccessibleProperty.LABEL], [self.describe(seg).replace("\n", ". ")])
            self.set_tooltip_text(None)
            self.trigger_tooltip_query()
            self.queue_draw()
            return True
        if keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter, Gdk.KEY_space) and self.cursor is not None:
            seg = self.segments[self.cursor]
            self.select(seg)
            if self.on_activate:
                self.on_activate(seg)
            return True
        return False
