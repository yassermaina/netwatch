"""Calendar heat map of daily reliability (GitHub-style, Monday first)."""

from __future__ import annotations

import calendar as pycal
from datetime import date
from typing import Callable, Optional

from gi.repository import Gdk, Gtk, PangoCairo

from netwatch.stats import DayStats
from netwatch.timefmt import format_duration, format_duration_long
from netwatch.ui import palette

CATEGORY_TEXT = {
    "online": "Online all monitored time",
    "minor": "Mostly online, brief outages",
    "no_internet": "No-internet outages",
    "major": "Major outage time (≥10%)",
    "not_connected": "Mostly not connected",
    "unmonitored": "Not monitored",
    "outside": "Outside the selected range",
}


class CalendarHeatmap(Gtk.DrawingArea):
    CELL = 18
    GAP = 4
    LABEL_W = 46
    TOP = 2

    def __init__(self):
        super().__init__()
        self.days: list[DayStats] = []
        self.weeks = 0
        self.selected: Optional[date] = None
        self.hover: Optional[int] = None
        self.cursor: Optional[int] = None
        self.on_select: Optional[Callable[[Optional[DayStats]], None]] = None
        self.set_focusable(True)
        self.set_draw_func(self._draw)
        self.set_has_tooltip(True)
        self.connect("query-tooltip", self._on_tooltip)
        motion = Gtk.EventControllerMotion()
        motion.connect("motion", self._on_motion)
        motion.connect("leave", lambda *_: self._set_hover(None))
        self.add_controller(motion)
        click = Gtk.GestureClick()
        click.connect("released", self._on_click)
        self.add_controller(click)
        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self._on_key)
        self.add_controller(keys)
        self.update_property([Gtk.AccessibleProperty.LABEL], ["Outage calendar"])
        # Monday / Wednesday / Friday labels in the user's locale (as in the reference)
        self._weekday_labels = {0: pycal.day_abbr[0], 2: pycal.day_abbr[2], 4: pycal.day_abbr[4]}

    def set_days(self, days: list[DayStats]) -> None:
        self.days = days
        self.weeks = (len(days) + 6) // 7
        step = self.CELL + self.GAP
        self.set_content_width(self.LABEL_W + max(1, self.weeks) * step)
        self.set_content_height(self.TOP + 7 * step + 22)
        self.cursor = None
        if self.selected and not any(d.day == self.selected for d in days):
            self.selected = None
        counts: dict[str, int] = {}
        for d in days:
            if d.in_range:
                counts[d.category] = counts.get(d.category, 0) + 1
        summary = ", ".join(f"{n} {CATEGORY_TEXT[k].lower()}" for k, n in counts.items())
        self.update_property(
            [Gtk.AccessibleProperty.DESCRIPTION],
            [
                f"{sum(counts.values())} days: {summary}. Use arrow keys to move between days and Enter to show "
                "that day's events."
            ],
        )
        self.queue_draw()

    # ------------------------------------------------------------ geometry
    def _cell_rect(self, index: int) -> tuple[float, float]:
        step = self.CELL + self.GAP
        week, dow = divmod(index, 7)
        return self.LABEL_W + week * step, self.TOP + dow * step

    def _index_at(self, x: float, y: float) -> Optional[int]:
        step = self.CELL + self.GAP
        if x < self.LABEL_W:
            return None
        week, dow = int((x - self.LABEL_W) // step), int((y - self.TOP) // step)
        if not (0 <= dow < 7) or week < 0:
            return None
        if (x - self.LABEL_W) % step > self.CELL or (y - self.TOP) % step > self.CELL:
            return None
        i = week * 7 + dow
        return i if i < len(self.days) else None

    # ------------------------------------------------------------ drawing
    def _draw(self, area, cr, width: int, height: int) -> None:
        c = palette.colors()
        step = self.CELL + self.GAP
        for dow, text in self._weekday_labels.items():
            layout = self.create_pango_layout(text)
            tw, th = layout.get_pixel_size()
            cr.set_source_rgba(*c["muted"], 1)
            cr.move_to(self.LABEL_W - tw - 8, self.TOP + dow * step + (self.CELL - th) / 2)
            PangoCairo.show_layout(cr, layout)
        last_month = None
        last_label_x = -100.0
        for i, d in enumerate(self.days):
            x, y = self._cell_rect(i)
            cat = d.category
            palette.rounded_rect(cr, x, y, self.CELL, self.CELL, 4)
            if cat == "outside":
                cr.set_source_rgba(*c["outside"], 0.035)
                cr.fill()
            elif cat == "unmonitored":
                cr.save()
                cr.clip()
                palette.hatch(cr, x, y, self.CELL, self.CELL, spacing=4)
                cr.restore()
            else:
                cr.set_source_rgb(*c[cat])
                cr.fill()
            if (self.selected and d.day == self.selected) or i == self.hover or (self.has_focus() and i == self.cursor):
                palette.rounded_rect(cr, x - 1.5, y - 1.5, self.CELL + 3, self.CELL + 3, 5)
                cr.set_source_rgba(*c["accent"] if self.selected == d.day or i == self.cursor else c["fg"], 0.95)
                cr.set_line_width(2)
                cr.stroke()
            if i % 7 == 0:
                # month label under the first week that contains day 1..7 of a new month
                month_day = next(
                    (self.days[j].day for j in range(i, min(i + 7, len(self.days))) if self.days[j].day.day == 1), None
                )
                if month_day is None and last_month is None and d.in_range:
                    month_day = d.day
                if month_day is not None and month_day.month != last_month and x - last_label_x > 34:
                    layout = self.create_pango_layout(month_day.strftime("%b"))
                    cr.set_source_rgba(*c["muted"], 1)
                    cr.move_to(x, self.TOP + 7 * step + 2)
                    PangoCairo.show_layout(cr, layout)
                    last_month = month_day.month
                    last_label_x = x

    # ------------------------------------------------------------ interaction
    def describe(self, d: DayStats) -> str:
        head = d.day.strftime("%A, %b ") + f"{d.day.day}, {d.day.year}"
        if d.category == "outside":
            return f"{head}\nOutside the selected range"
        if d.monitored <= 0:
            return f"{head}\nNot monitored"
        lines = [
            head,
            f"Monitored {format_duration_long(d.monitored)} ({d.coverage_pct:.0f}% of the day)",
            f"{d.outage_count} outage{'s' if d.outage_count != 1 else ''} · "
            f"{format_duration(d.downtime, precise=True)} downtime",
            CATEGORY_TEXT[d.category],
        ]
        if d.networks:
            lines.append("Networks: " + ", ".join(d.networks))
        return "\n".join(lines)

    def _on_tooltip(self, widget, x, y, keyboard, tooltip) -> bool:
        i = self.cursor if keyboard else self._index_at(x, y)
        if i is None or i >= len(self.days):
            return False
        tooltip.set_text(self.describe(self.days[i]))
        return True

    def _set_hover(self, i: Optional[int]) -> None:
        if i != self.hover:
            self.hover = i
            self.queue_draw()

    def _on_motion(self, ctrl, x, y) -> None:
        self._set_hover(self._index_at(x, y))

    def _toggle(self, i: int) -> None:
        d = self.days[i]
        if d.category == "outside":
            return
        if self.selected == d.day:
            self.selected = None
            result = None
        else:
            self.selected = d.day
            result = d
        self.queue_draw()
        if self.on_select:
            self.on_select(result)

    def _on_click(self, gesture, n, x, y) -> None:
        self.grab_focus()
        i = self._index_at(x, y)
        if i is not None:
            self.cursor = i
            self._toggle(i)

    def clear_selection(self) -> None:
        self.selected = None
        self.queue_draw()

    def _on_key(self, ctrl, keyval, keycode, state) -> bool:
        if not self.days:
            return False
        moves = {Gdk.KEY_Left: -7, Gdk.KEY_Right: 7, Gdk.KEY_Up: -1, Gdk.KEY_Down: 1}
        if keyval in moves:
            if self.cursor is None:
                self.cursor = max((i for i, d in enumerate(self.days) if d.in_range), default=0)
            else:
                self.cursor = max(0, min(len(self.days) - 1, self.cursor + moves[keyval]))
            self.update_property(
                [Gtk.AccessibleProperty.LABEL], [self.describe(self.days[self.cursor]).replace("\n", ". ")]
            )
            self.trigger_tooltip_query()
            self.queue_draw()
            return True
        if keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter, Gdk.KEY_space) and self.cursor is not None:
            self._toggle(self.cursor)
            return True
        return False
