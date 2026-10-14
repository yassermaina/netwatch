"""Small reusable widgets."""

from __future__ import annotations

from typing import Optional

from gi.repository import GLib, Gtk, Pango

from netwatch.models import ConnState
from netwatch.ui.palette import css_class


def label(
    text: str = "", classes: tuple[str, ...] = (), xalign: float = 0.0, wrap: bool = False, selectable: bool = False
) -> Gtk.Label:
    lbl = Gtk.Label(label=text, xalign=xalign)
    for c in classes:
        lbl.add_css_class(c)
    if wrap:
        lbl.set_wrap(True)
        lbl.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
    lbl.set_selectable(selectable)
    return lbl


def section_header(title: str, subtitle: Optional[str] = None) -> tuple[Gtk.Box, Optional[Gtk.Label]]:
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
    t = label(title, ("nw-section-title",))
    t.set_accessible_role(Gtk.AccessibleRole.HEADING) if hasattr(t, "set_accessible_role") else None
    box.append(t)
    sub = None
    if subtitle is not None:
        sub = label(subtitle, ("dim-label", "nw-subtitle"), wrap=True)
        box.append(sub)
    return box, sub


class StateDot(Gtk.Box):
    """Coloured state indicator; a dash for unmonitored periods (as in the reference)."""

    def __init__(self, state: ConnState = ConnState.UNKNOWN):
        super().__init__(valign=Gtk.Align.CENTER, halign=Gtk.Align.CENTER)
        self.add_css_class("nw-dot")
        self._cls: Optional[str] = None
        self.set_state(state)

    def set_state(self, state: ConnState) -> None:
        if self._cls:
            self.remove_css_class(self._cls)
        self._cls = css_class(state)
        self.add_css_class(self._cls)
        self.set_tooltip_text(state.label)


class MetricTile(Gtk.Box):
    """Icon + label above a large value (Period Summary style)."""

    def __init__(self, icon: str, title: str, value_class: Optional[str] = None, small: bool = False):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self.set_hexpand(True)
        head = Gtk.Box(spacing=6)
        img = Gtk.Image.new_from_icon_name(icon)
        img.add_css_class("dim-label")
        head.append(img)
        self.title = label(title, ("dim-label", "nw-metric-label"))
        self.title.set_ellipsize(Pango.EllipsizeMode.END)
        self.title.set_tooltip_text(title)
        head.append(self.title)
        self.append(head)
        self.value = label("—", ("nw-metric-value",))
        self.value.set_ellipsize(Pango.EllipsizeMode.END)
        if small:
            self.value.add_css_class("small")
        if value_class:
            self.value.add_css_class(value_class)
        self.append(self.value)
        self.note = label("", ("dim-label", "caption"))
        self.note.set_ellipsize(Pango.EllipsizeMode.END)
        self.note.set_visible(False)
        self.append(self.note)
        self._title_text = title

    def set(self, value: str, note: str = "", muted: bool = False, suffix: str = "") -> None:
        """``suffix`` is shown in a smaller size after the value, e.g. a percentage."""
        if suffix:
            self.value.set_markup(
                f"{GLib.markup_escape_text(value)} <span size='72%'>{GLib.markup_escape_text(suffix)}</span>"
            )
            value = f"{value} {suffix}"
        else:
            self.value.set_label(value)
        self.note.set_label(note)
        self.note.set_tooltip_text(note or None)
        self.value.set_tooltip_text(value)
        self.note.set_visible(bool(note))
        if muted:
            self.value.add_css_class("nw-muted-value")
        else:
            self.value.remove_css_class("nw-muted-value")
        self.update_property([Gtk.AccessibleProperty.LABEL], [f"{self._title_text}: {value}. {note}".strip()])


def legend_item(state_or_css: str, text: str) -> Gtk.Box:
    box = Gtk.Box(spacing=7)
    dot = Gtk.Box(valign=Gtk.Align.CENTER)
    dot.add_css_class("nw-dot")
    dot.add_css_class(state_or_css)
    box.append(dot)
    box.append(label(text, ("nw-legend",)))
    return box


def flow_box(min_per_line: int = 1, max_per_line: int = 4, spacing: int = 18) -> Gtk.FlowBox:
    fb = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, homogeneous=True)
    fb.set_min_children_per_line(min_per_line)
    fb.set_max_children_per_line(max_per_line)
    fb.set_column_spacing(spacing)
    fb.set_row_spacing(spacing)
    return fb


def flow_append(fb: Gtk.FlowBox, widget: Gtk.Widget) -> None:
    fb.append(widget)
    child = widget.get_parent()
    if child is not None:
        child.set_focusable(False)


# Column counts per window-width tier, applied by window breakpoints:
# "wide" > 1100sp, "large" <= 1100sp, "medium" <= 860sp, "small" <= 720sp (sidebar collapsed), "tiny" <= 460sp.
TIERS = ("wide", "large", "medium", "small", "tiny")


def responsive_flow(fb: Gtk.FlowBox, spec: dict) -> Gtk.FlowBox:
    """Tag a FlowBox with per-tier column counts; MainWindow installs the breakpoints."""
    fb.responsive_spec = spec
    fb.set_min_children_per_line(spec["wide"])
    fb.set_max_children_per_line(spec["wide"])
    return fb
