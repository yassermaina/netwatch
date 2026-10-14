"""Overview dashboard: live status, controls and today's numbers."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Optional

from gi.repository import Adw, Gtk, Pango

from netwatch.models import ConnState, EngineSnapshot, Lifecycle
from netwatch.timefmt import format_clock, format_duration, format_duration_long, local_day_start
from netwatch.ui.background import Generation, run_async
from netwatch.ui.palette import css_class
from netwatch.ui.widgets.common import MetricTile, StateDot, flow_append, flow_box, label, responsive_flow, section_header
from netwatch.ui.widgets.timeline import TimelineBar

if TYPE_CHECKING:
    from netwatch.ui.application import NetWatchApplication

STATE_ICONS = {
    ConnState.ONLINE: "network-wireless-signal-excellent-symbolic",
    ConnState.NO_INTERNET: "network-wireless-no-route-symbolic",
    ConnState.NOT_CONNECTED: "network-offline-symbolic",
    ConnState.UNKNOWN: "dialog-question-symbolic",
    ConnState.PAUSED: "media-playback-pause-symbolic",
    ConnState.NOT_RUNNING: "media-playback-stop-symbolic",
}


def ago(ts: Optional[float], now: float) -> str:
    if ts is None:
        return "never"
    d = max(0, now - ts)
    return "just now" if d < 2 else f"{format_duration(d)} ago"


class OverviewPage(Gtk.Box):
    title = "Overview"
    icon = "go-home-symbolic"

    def __init__(self, app: "NetWatchApplication"):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.app = app
        self.snapshot = EngineSnapshot()
        self.visible_page = False
        self.gen = Generation()
        self._updating_spin = False
        scroller = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True)
        clamp = Adw.Clamp(maximum_size=1240, tightening_threshold=900)
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        content.add_css_class("nw-page-content")
        clamp.set_child(content)
        scroller.set_child(clamp)
        self.append(scroller)

        # status card
        self.card = Gtk.Box(spacing=20)
        self.card.add_css_class("nw-status-card")
        self.status_icon = Gtk.Image.new_from_icon_name(STATE_ICONS[ConnState.UNKNOWN])
        self.status_icon.add_css_class("nw-status-icon")
        self.status_icon.set_valign(Gtk.Align.CENTER)
        self.card.append(self.status_icon)
        texts = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, hexpand=True, valign=Gtk.Align.CENTER)
        row = Gtk.Box(spacing=10)
        self.status_title = label("Not monitoring", ("nw-status-title",))
        self.status_title.set_ellipsize(Pango.EllipsizeMode.END)
        row.append(self.status_title)
        self.lifecycle_pill = label("Stopped", ("nw-pill",))
        self.lifecycle_pill.set_valign(Gtk.Align.CENTER)
        row.append(self.lifecycle_pill)
        self.outage_pill = label("", ("nw-pill", "outage"))
        self.outage_pill.set_valign(Gtk.Align.CENTER)
        self.outage_pill.set_visible(False)
        row.append(self.outage_pill)
        texts.append(row)
        self.status_sub = label("", ("dim-label",), wrap=True)
        texts.append(self.status_sub)
        self.status_iface = label("", ("dim-label", "caption"), wrap=True)
        texts.append(self.status_iface)
        self.card.append(texts)
        content.append(self.card)

        # controls
        controls = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, column_spacing=8, row_spacing=8,
                               max_children_per_line=12, homogeneous=False, margin_top=14)
        self.btn_start = self._button("Start", "media-playback-start-symbolic", "app.start", "suggested-action")
        self.btn_pause = self._button("Pause", "media-playback-pause-symbolic", "app.toggle-pause",
                                      tooltip="Pause monitoring (Space)")
        self.btn_stop = self._button("Stop", "media-playback-stop-symbolic", "app.stop",
                                     tooltip="Stop and close the monitoring session")
        self.btn_mute = Gtk.ToggleButton(tooltip_text="Mute sound alerts (M)")
        self.btn_mute.set_child(Adw.ButtonContent(icon_name="audio-volume-high-symbolic", label="Mute"))
        self.btn_mute.set_action_name("app.mute")
        spin_box = Gtk.Box(spacing=6, valign=Gtk.Align.CENTER, margin_start=8)
        spin_box.append(label("Check every"))
        self.interval = Gtk.SpinButton.new_with_range(1, 3600, 1)
        self.interval.set_tooltip_text("Check interval in seconds")
        self.interval.update_property([Gtk.AccessibleProperty.LABEL], ["Check interval in seconds"])
        self.interval.connect("value-changed", self._on_interval)
        spin_box.append(self.interval)
        spin_box.append(label("s"))
        history = self._button("History", "document-open-recent-symbolic", "win.open-history", tooltip="Ctrl+O")
        reports = self._button("Reports", "x-office-document-symbolic", "win.open-reports")
        for w in (self.btn_start, self.btn_pause, self.btn_stop, self.btn_mute, spin_box, history, reports):
            flow_append(controls, w)
        content.append(controls)

        # metrics
        head, _ = section_header("Now")
        head.add_css_class("nw-section")
        content.append(head)
        self.metrics = flow_box(2, 6, 18)
        self.metrics.set_margin_top(12)
        self.m_latency = MetricTile("network-transmit-receive-symbolic", "Latency", small=True)
        self.m_loss = MetricTile("network-error-symbolic", "Packet loss (5 min)", small=True)
        self.m_last = MetricTile("emblem-ok-symbolic", "Last successful check", small=True)
        self.m_session = MetricTile("alarm-symbolic", "Monitoring for", small=True)
        self.m_outages = MetricTile("window-close-symbolic", "Outages today", small=True)
        self.m_down = MetricTile("dialog-warning-symbolic", "Downtime today", "nw-warn", small=True)
        for tile in (self.m_latency, self.m_loss, self.m_last, self.m_session, self.m_outages, self.m_down):
            card = Gtk.Box()
            card.add_css_class("nw-card")
            card.append(tile)
            flow_append(self.metrics, card)
        content.append(responsive_flow(self.metrics, {"wide": 3, "large": 3, "medium": 3, "small": 2, "tiny": 2}))

        # live timeline
        head, self.live_caption = section_header("Last hour", "")
        head.add_css_class("nw-section")
        content.append(head)
        self.live = TimelineBar(bar_height=30, compact=True)
        self.live.set_margin_top(10)
        self.live.on_activate = lambda seg: self.app.window.show_history_segment(seg)
        content.append(self.live)

        # targets
        head, self.targets_caption = section_header("Monitoring targets", "")
        head.add_css_class("nw-section")
        content.append(head)
        self.targets_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE, margin_top=10)
        self.targets_list.add_css_class("boxed-list")
        content.append(self.targets_list)
        edit = Gtk.Button(label="Edit Targets…", halign=Gtk.Align.START, margin_top=10)
        edit.set_action_name("win.preferences")
        content.append(edit)
        self.target_rows: dict[int, tuple[Adw.ActionRow, StateDot, Gtk.Label]] = {}
        self.reload_targets()

    def _button(self, text: str, icon: str, action: str, css: Optional[str] = None,
                tooltip: Optional[str] = None) -> Gtk.Button:
        b = Gtk.Button(action_name=action, tooltip_text=tooltip)
        b.set_child(Adw.ButtonContent(icon_name=icon, label=text))
        if css:
            b.add_css_class(css)
        return b

    # ------------------------------------------------------------ updates
    def set_page_visible(self, visible: bool) -> None:
        self.visible_page = visible
        if visible:
            self.refresh_stats()

    def reload_targets(self) -> None:
        self.targets_list.remove_all()
        self.target_rows.clear()
        targets = self.app.repo.list_targets()
        for t in targets:
            row = Adw.ActionRow(title=t.name, subtitle=f"{t.address} · {t.check_type.upper()}"
                                + ("" if t.enabled else " · disabled"))
            dot = StateDot(ConnState.UNKNOWN)
            row.add_prefix(dot)
            value = label("—", ("nw-num", "dim-label"), xalign=1.0)
            row.add_suffix(value)
            self.targets_list.append(row)
            self.target_rows[t.id] = (row, dot, value)
        enabled = sum(1 for t in targets if t.enabled)
        self.targets_caption.set_label(f"{enabled} of {len(targets)} targets enabled. Connectivity is ONLINE while "
                                       "at least one target responds.")
        self._updating_spin = True
        self.interval.set_value(self.app.settings.get("interval_seconds"))
        self._updating_spin = False

    def on_snapshot(self, snap: EngineSnapshot) -> None:
        self.snapshot = snap
        self.update_status()
        for r in snap.last_results:
            entry = self.target_rows.get(r["target_id"])
            if not entry:
                continue
            _row, dot, value = entry
            if r["success"]:
                dot.set_state(ConnState.ONLINE)
                value.set_label(f"{r['latency_ms']:.1f} ms" if r["latency_ms"] is not None else "OK")
            else:
                dot.set_state(ConnState.NO_INTERNET)
                value.set_label(r["error"] or "failed")
        if self.visible_page:
            self.refresh_stats()

    def on_mute_changed(self, muted: bool) -> None:
        self.btn_mute.set_child(Adw.ButtonContent(
            icon_name="audio-volume-muted-symbolic" if muted else "audio-volume-high-symbolic",
            label="Unmute" if muted else "Mute"))

    def update_status(self) -> None:
        snap, now = self.snapshot, time.time()
        lc = snap.lifecycle
        for cls in ("online", "no-internet", "not-connected", "unknown", "unmonitored"):
            self.card.remove_css_class(cls)
        state = snap.state if lc in (Lifecycle.RUNNING, Lifecycle.PAUSED) else ConnState.NOT_RUNNING
        if lc == Lifecycle.PAUSED:
            state = ConnState.PAUSED
        self.card.add_css_class(css_class(state))
        self.status_icon.set_from_icon_name(STATE_ICONS[state])
        titles = {ConnState.UNKNOWN: "Checking…", ConnState.PAUSED: "Monitoring paused",
                  ConnState.NOT_RUNNING: "Not monitoring"}
        title = titles.get(state, state.label)
        if lc == Lifecycle.ERROR:
            title = "Monitoring error"
        self.status_title.set_label(title)
        for cls in ("running", "paused", "error"):
            self.lifecycle_pill.remove_css_class(cls)
        self.lifecycle_pill.set_label(lc.label)
        if lc in (Lifecycle.RUNNING, Lifecycle.PAUSED, Lifecycle.ERROR):
            self.lifecycle_pill.add_css_class(lc.value)
        self.outage_pill.set_visible(snap.outage_id is not None)
        self.outage_pill.set_label(f"Outage #{snap.outage_id}" if snap.outage_id else "")
        parts = []
        if lc == Lifecycle.ERROR:
            parts.append(snap.message)
        elif lc == Lifecycle.STOPPED:
            parts.append("Press Start to begin monitoring. Nothing is recorded while stopped.")
        elif lc == Lifecycle.PAUSED:
            parts.append(f"Paused since {format_clock(snap.state_since)}" if snap.state_since else "Paused")
            parts.append("This period is recorded as unmonitored.")
        else:
            if snap.state_since and state != ConnState.UNKNOWN:
                parts.append(f"Since {format_clock(snap.state_since)} ({format_duration(now - snap.state_since)})")
            if state == ConnState.UNKNOWN and self.app.engine.classifier.last_reason:
                parts.append(self.app.engine.classifier.last_reason)
            parts.append(f"last check {ago(snap.last_check_at, now)}")
            if snap.pending_state is not None and snap.pending_state != snap.state:
                parts.append(f"verifying possible change to {snap.pending_state.short_label}")
        self.status_sub.set_label(" · ".join(p for p in parts if p))
        local = snap.local
        if local is not None and lc == Lifecycle.RUNNING:
            desc = local.describe() or ("No active connection" if local.connected is False else "Network status unknown")
            src = {"networkmanager": "NetworkManager", "sysfs": "kernel interfaces"}.get(local.source, local.source)
            self.status_iface.set_label(f"{desc} · reported by {src}")
            self.status_iface.set_visible(True)
        else:
            self.status_iface.set_visible(False)
        self.update_property([Gtk.AccessibleProperty.LABEL], [f"Connection status: {title}"])
        running = lc in (Lifecycle.RUNNING, Lifecycle.PAUSED)
        self.btn_start.set_visible(not running)
        self.btn_pause.set_visible(running)
        self.btn_stop.set_visible(running)
        self.btn_pause.set_child(Adw.ButtonContent(
            icon_name="media-playback-start-symbolic" if lc == Lifecycle.PAUSED else "media-playback-pause-symbolic",
            label="Resume" if lc == Lifecycle.PAUSED else "Pause"))
        self.btn_pause.set_tooltip_text("Resume monitoring (Space)" if lc == Lifecycle.PAUSED else
                                        "Pause monitoring (Space)")
        # cheap, snapshot-driven tiles
        if snap.last_latency_ms is not None and snap.last_success_at and now - snap.last_success_at < 120:
            self.m_latency.set(f"{snap.last_latency_ms:.1f} ms", "best responding target")
        elif snap.last_latency_ms is not None:
            self.m_latency.set(f"{snap.last_latency_ms:.1f} ms", "most recent reply", muted=True)
        else:
            self.m_latency.set("—", "no replies yet", muted=True)
        last_ok = snap.last_success_at or self._last_success_db
        self.m_last.set(ago(last_ok, now) if last_ok else "never",
                        format_clock(last_ok, seconds=True) if last_ok else "", muted=last_ok is None)
        if snap.session_started and running:
            self.m_session.set(format_duration_long(now - snap.session_started),
                               f"since {format_clock(snap.session_started)}")
        else:
            self.m_session.set("—", "not monitoring", muted=True)

    _last_success_db: Optional[float] = None

    def tick_clock(self) -> None:
        if self.visible_page:
            self.update_status()

    def refresh_stats(self) -> None:
        token = self.gen.next()
        now = time.time()

        def work():
            stats = self.app.stats
            today = stats.summary(local_day_start(now), now, now)
            hour_segments = stats.segments(now - 3600, now, now)
            quality = stats.recent_quality(now)
            last_ok = self.app.repo.last_success()
            return today, hour_segments, quality, last_ok

        run_async(work, lambda r: self._apply_stats(token, r, now))

    def _apply_stats(self, token: int, result, now: float) -> None:
        if not self.gen.current(token):
            return
        today, hour_segments, quality, last_ok = result
        self._last_success_db = last_ok
        self.m_outages.set(str(today.outage_count),
                           f"monitored {format_duration_long(today.monitored)} today" if today.monitored else
                           "no monitored time today", muted=today.monitored == 0)
        if today.monitored:
            self.m_down.set(format_duration(today.downtime, precise=True),
                            f"{today.downtime_pct:.2f}% of monitored time")
        else:
            self.m_down.set("—", "no monitored time today", muted=True)
        if quality["loss_pct"] is None:
            self.m_loss.set("—", "no checks in the last 5 minutes", muted=True)
        else:
            self.m_loss.set(f"{quality['loss_pct']:.1f}%", f"{quality['checks']} checks")
        self.live.set_data(hour_segments, now - 3600, now, now)
        online = sum(s.duration for s in hour_segments if s.state == ConnState.ONLINE)
        mon = sum(s.duration for s in hour_segments if s.state.is_monitored)
        self.live_caption.set_label(
            f"Online {online / mon * 100:.1f}% of {format_duration_long(mon)} monitored in the last hour" if mon else
            "Nothing monitored in the last hour"
        )
        self.update_status()

    def _on_interval(self, spin: Gtk.SpinButton) -> None:
        if self._updating_spin:
            return
        value = int(spin.get_value())
        errors = self.app.settings.update({"interval_seconds": value})
        if errors:
            self.app.window.toast(errors[0].message)
            self._updating_spin = True
            spin.set_value(self.app.settings.get("interval_seconds"))
            self._updating_spin = False
