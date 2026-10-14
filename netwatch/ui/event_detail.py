"""Detail view for one history event: timeline, measurements, failure reasons, recovery evidence."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from gi.repository import Adw, Gtk

from netwatch.models import DOWN_STATES, ConnState, ErrorCategory
from netwatch.reports import outage_label
from netwatch.stats import Segment
from netwatch.timefmt import format_duration, format_timestamp
from netwatch.ui.background import run_async
from netwatch.ui.dialogs import present_content
from netwatch.ui.palette import SERIES
from netwatch.ui.widgets.charts import LatencyChart
from netwatch.ui.widgets.common import StateDot, label

if TYPE_CHECKING:
    from netwatch.ui.application import NetWatchApplication


def _row(title: str, value: str) -> Adw.ActionRow:
    row = Adw.ActionRow(title=title)
    v = label(value, ("dim-label",), xalign=1.0, wrap=True, selectable=True)
    v.set_max_width_chars(48)
    row.add_suffix(v)
    return row


def load_detail(app: "NetWatchApplication", seg: Segment) -> dict:
    repo, stats = app.repo, app.stats
    start, end = seg.start, seg.end
    span = max(60.0, end - start)
    perf = stats.performance(start, end + 1, buckets=min(240, max(10, int(span / 5))))
    errors = repo.error_breakdown(start, end + 1)
    outage = stats.outages(start, end, time.time())
    outage = next((o for o in outage if o.id == seg.outage_id), None) if seg.outage_id else None
    events = repo.events_between(start - 1, end + 1)
    return {"perf": perf, "errors": errors, "outage": outage, "targets": repo.targets_by_id(), "events": events}


def show_event_detail(app: "NetWatchApplication", parent: Gtk.Widget, seg: Segment) -> None:
    page = Adw.PreferencesPage()
    head = Adw.PreferencesGroup()
    top = Gtk.Box(spacing=12)
    dot = StateDot(seg.state)
    top.append(dot)
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
    box.append(label(seg.state.label, ("title-2",)))
    end = "now" if seg.ongoing else format_timestamp(seg.end, seconds=True)
    box.append(label(f"{format_timestamp(seg.start, seconds=True)} – {end}", ("dim-label",)))
    top.append(box)
    head.add(top)
    page.add(head)

    overview = Adw.PreferencesGroup(title="Overview")
    overview.add(_row("Duration", format_duration(seg.duration, precise=True) + (" (ongoing)" if seg.ongoing else "")))
    if seg.synthetic:
        overview.add(_row("Records", "No monitoring records exist for this period; NetWatch was not running."))
    if seg.reason:
        overview.add(_row("Reason", seg.reason))
    if seg.targets:
        overview.add(_row("Responding targets" if seg.state == ConnState.ONLINE else "Failing targets", seg.targets))
    local = seg.local
    if local and local.describe():
        overview.add(_row("Network interface", local.describe()))
        if local.interface:
            overview.add(_row("Device", local.interface + (f" · via {local.source}" if local.source else "")))
    page.add(overview)

    loading = Gtk.Spinner(spinning=True, halign=Gtk.Align.CENTER, margin_top=12, margin_bottom=12)
    holder = Adw.PreferencesGroup(title="Measurements")
    holder.add(loading)
    page.add(holder)

    scroller = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
    scroller.set_child(page)
    actions = Gtk.Box(spacing=8, halign=Gtk.Align.END, margin_top=6, margin_bottom=12, margin_end=12)
    export = Gtk.Button(label="Export Event Report…")
    export.connect("clicked", lambda *_: app.window.start_export(focus=seg))
    actions.append(export)
    outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
    scroller.set_vexpand(True)
    outer.append(scroller)
    outer.append(actions)
    present_content(parent, "Event Details", outer, 680, 720)

    if seg.synthetic or seg.state == ConnState.PAUSED:
        holder.remove(loading)
        holder.add(_row("Measurements", "None — nothing was measured during this period."))
        return

    def fill(data: dict) -> None:
        holder.remove(loading)
        perf, targets = data["perf"], data["targets"]
        overall = perf["overall"]
        if overall["checks"] == 0:
            holder.add(_row("Measurements", "No raw measurements are stored for this period (they may have been "
                                            "pruned by the retention policy)."))
        else:
            holder.add(_row("Checks", f"{overall['checks']} ({overall['checks'] - overall['ok']} failed)"))
            if overall["avg"] is not None:
                holder.add(_row("Latency", f"avg {overall['avg']:.1f} ms · median {overall['median']:.1f} ms · "
                                           f"min {overall['min']:.1f} · max {overall['max']:.1f} ms"))
            chart = LatencyChart(height=180)
            series = []
            for i, (tid, pts) in enumerate(sorted(perf["series"].items())):
                t = targets.get(tid)
                series.append({"name": t.name if t else str(tid), "color": SERIES[i % len(SERIES)], "points": pts})
            chart.set_data(perf["start"], perf["end"], perf["bucket"], series,
                           interval=float(app.settings.get("interval_seconds")))
            frame = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, margin_top=10)
            frame.add_css_class("nw-chart-frame")
            frame.append(chart)
            inspect = label("Hover the chart to inspect values.", ("dim-label", "caption"), wrap=True)
            chart.on_inspect = lambda text: inspect.set_label(text or "Hover the chart to inspect values.")
            frame.append(inspect)
            holder.add(frame)
            per = Adw.PreferencesGroup(title="Per target")
            for tid, p in perf["per_target"].items():
                t = targets.get(tid)
                lat = f"{p['avg']:.1f} ms avg" if p["avg"] is not None else "no replies"
                loss = f"{p['loss_pct']:.0f}% loss" if p["loss_pct"] is not None else ""
                per.add(_row(t.display if t else str(tid), f"{lat} · {loss} · {p['checks']} checks"))
            page.add(per)
        if data["errors"]:
            reasons = Adw.PreferencesGroup(title="Failure reasons")
            for r in data["errors"][:12]:
                t = targets.get(r["target_id"])
                try:
                    cat = ErrorCategory(r["error_category"]).label
                except ValueError:
                    cat = r["error_category"]
                reasons.add(_row(t.address if t else "?", f"{cat} × {r['n']}"))
            page.add(reasons)
        o = data["outage"]
        if o is not None and seg.state in DOWN_STATES:
            og = Adw.PreferencesGroup(title=f"Outage #{o.id}")
            og.add(_row("Status", outage_label(o)))
            og.add(_row("Start", format_timestamp(o.start, seconds=True)
                        + ("" if o.start_observed else " (monitoring started during the outage — real start unknown)")))
            og.add(_row("Detected", format_timestamp(o.detected_at, seconds=True)))
            if not o.ongoing:
                og.add(_row("End", format_timestamp(o.end, seconds=True)))
            if o.recovery_detected_at:
                og.add(_row("Recovery confirmed", format_timestamp(o.recovery_detected_at, seconds=True)))
            og.add(_row("Total outage duration", format_duration(o.full_duration, precise=True)))
            for t in o.targets:
                og.add(_row(t["address"], f"{t['failure_count']} failed checks, last: {t['last_error']}"))
            page.add(og)
            ev = Adw.PreferencesGroup(title="Evidence")
            for item in o.evidence[-10:]:
                results = item.get("results") or []
                summary = ", ".join(
                    f"{r['target']} {'ok ' + str(r['latency_ms']) + ' ms' if r['ok'] else r['error']}" for r in results
                ) or item.get("note", "")
                title = {"detected": "Failure detected", "recovered": "Recovery evidence",
                         "reclassified": "State changed"}.get(item.get("event"), item.get("event", "").replace("_", " ").capitalize())
                ev.add(_row(f"{title} · {format_timestamp(item.get('ts'), seconds=True)}", summary))
            page.add(ev)
        if data["events"]:
            ae = Adw.PreferencesGroup(title="Application events")
            for e in data["events"][:10]:
                ae.add(_row(format_timestamp(e["ts"], seconds=True), e["message"] or e["kind"]))
            page.add(ae)

    run_async(lambda: load_detail(app, seg), fill,
              lambda exc: (holder.remove(loading), holder.add(_row("Error", str(exc)))))
