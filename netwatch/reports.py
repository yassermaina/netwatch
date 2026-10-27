"""Report generation (TXT, CSV, HTML).

Reports only state what the stored records show.  Outages whose start or
recovery was not observed are labelled as such, and coverage gaps are listed.
"""

from __future__ import annotations

import csv
import html
import io
import socket
import time
from dataclasses import dataclass, field
from typing import Optional

from netwatch import APP_NAME, __version__
from netwatch.models import DOWN_STATES, ConnState
from netwatch.settings import SettingsStore
from netwatch.stats import HistoryData, OutageInfo, ReliabilityService, Segment
from netwatch.storage.repository import Repository
from netwatch.timefmt import (
    format_duration,
    format_duration_long,
    format_iso_local,
    format_iso_utc,
    format_percent,
)

FORMATS = {"txt": "Plain text", "csv": "CSV (spreadsheet)", "html": "HTML (offline web page)"}

STATE_COLORS = {
    ConnState.ONLINE: "#34c759",
    ConnState.NO_INTERNET: "#ff9f0a",
    ConnState.NOT_CONNECTED: "#8e8e93",
    ConnState.UNKNOWN: "#5e5ce6",
    ConnState.PAUSED: "#c7c7cc",
    ConnState.NOT_RUNNING: "#d1d1d6",
}


@dataclass
class ReportData:
    title: str
    generated_at: float
    range_start: float
    range_end: float
    history: HistoryData
    settings: dict
    targets: dict
    interruptions: list = field(default_factory=list)
    limitations: list = field(default_factory=list)
    focus: Optional[Segment] = None
    hostname: str = ""


def outage_label(o: OutageInfo) -> str:
    if o.ongoing:
        return "ongoing"
    if o.end_observed:
        return "recovered"
    return {
        "monitoring_stopped": "monitoring stopped before recovery",
        "monitoring_paused": "monitoring paused before recovery",
        "monitoring_interrupted": "monitoring interrupted before recovery",
        "evidence_lost": "evidence lost before recovery",
    }.get(o.status, o.status)


def build_report(
    repo: Repository,
    stats: ReliabilityService,
    settings: SettingsStore,
    period: str = "all",
    custom: Optional[tuple[float, float]] = None,
    focus: Optional[Segment] = None,
    now: Optional[float] = None,
) -> ReportData:
    now = now if now is not None else time.time()
    if focus is not None:
        custom = (focus.start, focus.end)
    history = stats.history(period, now, custom)
    s = history.summary
    titles = {"24h": "Last 24 hours", "7d": "Last 7 days", "30d": "Last 30 days", "all": "All retained history"}
    title = "Event report" if focus else (titles.get(period, "Custom range") if not custom else "Custom range")
    interruptions = [
        seg for seg in history.segments if seg.state in (ConnState.NOT_RUNNING, ConnState.PAUSED, ConnState.UNKNOWN)
    ]
    cfg = settings.all()
    limitations = []
    if s.coverage_pct is not None and s.coverage_pct < 99.9:
        limitations.append(
            f"Monitoring covered {format_percent(s.coverage_pct, 1)} of the requested range "
            f"({format_duration_long(s.monitored)} of {format_duration_long(s.range_duration)}). "
            "Uptime and downtime are calculated over monitored time only; nothing is assumed about unmonitored periods."
        )
    if s.unknown > 0:
        limitations.append(
            f"{format_duration_long(s.unknown)} were recorded as Unknown (NetWatch was running but had insufficient "
            "evidence, e.g. right after start or when checks could not run)."
        )
    if any(not o.start_observed for o in s.outages):
        limitations.append(
            "Some outages were already in progress when monitoring started; their real start is unknown."
        )
    if any(not o.end_observed and not o.ongoing for o in s.outages):
        limitations.append(
            "Some outages ended because monitoring stopped, paused or was interrupted; recovery was not observed."
        )
    if any(o.ongoing for o in s.outages):
        limitations.append("An outage is still ongoing at report time.")
    limitations.append(
        f"Times are detection times. Checks ran every {cfg['interval_seconds']}s with a {cfg['timeout_seconds']}s "
        f"timeout; a failure was confirmed after {cfg['failure_threshold']} consecutive failed round(s) and recovery "
        f"after {cfg['recovery_threshold']} successful round(s), so boundaries are accurate to about one interval."
    )
    if cfg["retention_days"]:
        limitations.append(
            f"Raw measurements older than {cfg['retention_days']} days are pruned; state and outage history is kept."
        )
    return ReportData(
        title=title,
        generated_at=now,
        range_start=s.range_start,
        range_end=s.range_end,
        history=history,
        settings=cfg,
        targets=repo.targets_by_id(),
        interruptions=interruptions,
        limitations=limitations,
        focus=focus,
        hostname=socket.gethostname(),
    )


def _seg_details(seg: Segment) -> str:
    bits = []
    if seg.targets:
        bits.append(("responding: " if seg.state == ConnState.ONLINE else "failing: ") + seg.targets)
    local = seg.local
    if local and local.describe():
        bits.append(local.describe())
    if seg.reason and seg.state != ConnState.ONLINE:
        bits.append(seg.reason)
    return "; ".join(bits)


def seg_network(seg: Segment, r: "ReportData") -> str:
    """Network of an entry; a disconnection is attributed to the network that dropped."""
    if seg.network:
        return seg.network
    o = r.history.outages_by_id.get(seg.outage_id) if seg.outage_id else None
    return (o.network or "") if o is not None else ""


def network_breakdown(r: "ReportData") -> list[tuple[str, float, float, int]]:
    """(network, monitored seconds, downtime seconds, outages) per network, most used first."""
    stats: dict[str, list] = {}
    for seg in r.history.segments:
        name = seg_network(seg, r)
        if not name or not seg.state.is_monitored:
            continue
        entry = stats.setdefault(name, [0.0, 0.0, 0])
        entry[0] += seg.duration
        if seg.state.is_down:
            entry[1] += seg.duration
    for o in r.history.summary.outages:
        if o.network:
            stats.setdefault(o.network, [0.0, 0.0, 0])[2] += 1
    return sorted(((k, v[0], v[1], v[2]) for k, v in stats.items()), key=lambda x: -x[1])


def _outage_targets(o: OutageInfo) -> str:
    return ", ".join(f"{t['address']} ({t['failure_count']} failed, last: {t['last_error']})" for t in o.targets)


# ----------------------------------------------------------------- TXT
def _t(ts: Optional[float]) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts)) if ts is not None else "now"


def render_txt(r: ReportData) -> str:
    s = r.history.summary
    lo = s.longest_outage
    out = io.StringIO()
    w = out.write
    w(f"{APP_NAME} connectivity report — {r.title}\n")
    w("=" * 60 + "\n")
    w(
        f"Generated:        {_t(r.generated_at)} {time.strftime('%Z (UTC%z)', time.localtime(r.generated_at))}"
        f" on {r.hostname}\n"
    )
    w(f"Requested range:  {_t(r.range_start)} → {_t(r.range_end)} ({format_duration_long(s.range_duration)})\n")
    w(f"Monitored:        {format_duration_long(s.monitored)} ({format_percent(s.coverage_pct, 1)} coverage)\n")
    w(f"Uptime:           {format_percent(s.uptime_pct)} of monitored time\n")
    w(
        f"Total downtime:   {format_duration(s.downtime, precise=True)} ({format_percent(s.downtime_pct)} of monitored time)\n"
    )
    w(f"Outages:          {s.outage_count}\n")
    if lo:
        w(
            f"Longest outage:   {format_duration(lo.duration_in_range, precise=True)}"
            f" ({outage_label(lo)}, started {_t(lo.start)})\n"
        )
    w(
        f"Not monitored:    app not running {format_duration_long(s.not_running)}, paused {format_duration_long(s.paused)},"
        f" unknown {format_duration_long(s.unknown)}\n"
    )
    active_targets = ", ".join(t.address for t in r.targets.values() if t.enabled)
    w(f"Targets:          {active_targets or 'none'}\n\n")

    w("Networks\n--------\n")
    breakdown = network_breakdown(r)
    if not breakdown:
        w("No network names were recorded in this range.\n")
    for name, monitored, down, count in breakdown:
        w(f"{name:<28} monitored {format_duration_long(monitored):>9}  downtime {format_duration(down, precise=True):>9}"
          f"  outages {count}\n")
    w("\n")

    w("Outages\n-------\n")
    if not s.outages:
        w("No outages recorded in this range.\n")
    for o in s.outages:
        start = _t(o.start) + ("" if o.start_observed else " (start not observed)")
        end = "ongoing" if o.ongoing else _t(o.end)
        w(
            f"#{o.id}  {o.classification.short_label:<14} {start} → {end}  "
            f"{format_duration(o.full_duration, precise=True)}  [{outage_label(o)}]"
            + (f"  network: {o.network}" if o.network else "")
            + "\n"
        )
        if o.targets:
            w(f"      targets: {_outage_targets(o)}\n")
    w("\nMonitoring interruptions\n------------------------\n")
    if not r.interruptions:
        w("None.\n")
    for seg in r.interruptions:
        end = "now" if seg.ongoing else _t(seg.end)
        w(f"{seg.state.short_label:<16} {_t(seg.start)} → {end}  {format_duration(seg.duration)}\n")
    w("\nEvent timeline\n--------------\n")
    for seg in r.history.segments:
        end = "now" if seg.ongoing else _t(seg.end)
        details = _seg_details(seg)
        network = seg_network(seg, r)
        w(
            f"{_t(seg.start)} → {end}  {seg.state.short_label:<16} {format_duration(seg.duration):>8}"
            + (f"  [{network}]" if network else "")
            + (f"  {details}" if details else "")
            + "\n"
        )
    w("\nLimitations\n-----------\n")
    for item in r.limitations:
        w(f"* {item}\n")
    w(f"\nGenerated by {APP_NAME} {__version__}. All times local; durations computed from stored records.\n")
    return out.getvalue()


# ----------------------------------------------------------------- CSV
CSV_COLUMNS = [
    "record_type",
    "state",
    "network",
    "start_utc",
    "end_utc",
    "start_local",
    "end_local",
    "duration_seconds",
    "duration",
    "ongoing",
    "outage_id",
    "start_observed",
    "end_observed",
    "targets",
    "interface",
    "details",
]


def render_csv(r: ReportData) -> str:
    s = r.history.summary
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\r\n")
    writer.writerow(CSV_COLUMNS)

    def summary(metric: str, value: str) -> None:
        writer.writerow(["summary", metric] + [""] * (len(CSV_COLUMNS) - 3) + [value])

    summary("report", r.title)
    summary("generated_local", format_iso_local(r.generated_at))
    summary("range_start_local", format_iso_local(r.range_start))
    summary("range_end_local", format_iso_local(r.range_end))
    summary("monitored_seconds", f"{s.monitored:.0f}")
    summary("coverage_percent", f"{s.coverage_pct:.3f}" if s.coverage_pct is not None else "")
    summary("uptime_percent", f"{s.uptime_pct:.4f}" if s.uptime_pct is not None else "")
    summary("downtime_seconds", f"{s.downtime:.0f}")
    summary("outage_count", str(s.outage_count))
    lo = s.longest_outage
    summary("longest_outage_seconds", f"{lo.duration_in_range:.0f}" if lo else "0")
    for seg in r.history.segments:
        local = seg.local
        writer.writerow(
            [
                "unmonitored" if seg.synthetic else ("paused" if seg.state == ConnState.PAUSED else "state"),
                seg.state.short_label,
                seg_network(seg, r),
                format_iso_utc(seg.start),
                "" if seg.ongoing else format_iso_utc(seg.end),
                format_iso_local(seg.start),
                "" if seg.ongoing else format_iso_local(seg.end),
                f"{seg.duration:.0f}",
                format_duration(seg.duration, precise=True),
                "yes" if seg.ongoing else "no",
                seg.outage_id or "",
                "",
                "",
                seg.targets or "",
                local.describe() if local else "",
                seg.reason or "",
            ]
        )
    for o in s.outages:
        writer.writerow(
            [
                "outage",
                o.classification.short_label,
                o.network or "",
                format_iso_utc(o.start),
                "" if o.ongoing else format_iso_utc(o.end),
                format_iso_local(o.start),
                "" if o.ongoing else format_iso_local(o.end),
                f"{o.full_duration:.0f}",
                format_duration(o.full_duration, precise=True),
                "yes" if o.ongoing else "no",
                o.id,
                "yes" if o.start_observed else "no",
                "yes" if o.end_observed else "no",
                _outage_targets(o),
                "",
                outage_label(o),
            ]
        )
    return out.getvalue()


# ----------------------------------------------------------------- HTML
def _timeline_svg(r: ReportData, width: int = 960, height: int = 36) -> str:
    span = max(1.0, r.range_end - r.range_start)
    rects = []
    for seg in r.history.segments:
        x = (seg.start - r.range_start) / span * width
        w = max(0.75, seg.duration / span * width)
        color = STATE_COLORS[seg.state]
        title = html.escape(
            f"{seg.state.label}: {format_iso_local(seg.start)} – "
            f"{'now' if seg.ongoing else format_iso_local(seg.end)} ({format_duration(seg.duration)})"
        )
        pattern = (
            ' fill="url(#hatch)"' if seg.state in (ConnState.NOT_RUNNING, ConnState.PAUSED) else f' fill="{color}"'
        )
        rects.append(
            f'<rect x="{x:.2f}" y="0" width="{w:.2f}" height="{height}"{pattern}><title>{title}</title></rect>'
        )
    return (
        f'<svg viewBox="0 0 {width} {height}" class="timeline" role="img" aria-label="Event timeline" '
        f'preserveAspectRatio="none"><defs><pattern id="hatch" width="6" height="6" patternUnits="userSpaceOnUse" '
        f'patternTransform="rotate(45)"><rect width="6" height="6" fill="#e5e5ea"/><line x1="0" y1="0" x2="0" y2="6" '
        f'stroke="#b0b0b8" stroke-width="2"/></pattern></defs>{"".join(rects)}</svg>'
    )


def render_html(r: ReportData) -> str:
    s = r.history.summary
    e = html.escape
    lo = s.longest_outage

    def card(label: str, value: str, cls: str = "") -> str:
        return f'<div class="card"><div class="label">{e(label)}</div><div class="value {cls}">{e(value)}</div></div>'

    outage_rows = (
        "".join(
            f"<tr><td>#{o.id}</td><td><span class='dot' style='background:{STATE_COLORS[o.classification]}'></span>"
            f"{e(o.classification.short_label)}</td>"
            f"<td>{e(format_iso_local(o.start))}{'' if o.start_observed else ' <em>(start not observed)</em>'}</td>"
            f"<td>{'ongoing' if o.ongoing else e(format_iso_local(o.end))}</td>"
            f"<td class='num'>{e(format_duration(o.full_duration, precise=True))}</td><td>{e(outage_label(o))}</td>"
            f"<td>{e(o.network or '—')}</td><td>{e(_outage_targets(o))}</td></tr>"
            for o in s.outages
        )
        or "<tr><td colspan='8' class='empty'>No outages recorded in this range.</td></tr>"
    )
    seg_rows = "".join(
        f"<tr><td><span class='dot' style='background:{STATE_COLORS[seg.state]}'></span>{e(seg.state.label)}</td>"
        f"<td>{e(format_iso_local(seg.start))}</td><td>{'now' if seg.ongoing else e(format_iso_local(seg.end))}</td>"
        f"<td class='num'>{e(format_duration(seg.duration, precise=True))}</td>"
        f"<td>{e(seg_network(seg, r) or '—')}</td><td>{e(_seg_details(seg))}</td></tr>"
        for seg in reversed(r.history.segments)
    )
    legend = "".join(
        f"<span><i style='background:{STATE_COLORS[st]}'></i>{e(st.label)}</span>"
        for st in (
            ConnState.ONLINE,
            ConnState.NO_INTERNET,
            ConnState.NOT_CONNECTED,
            ConnState.UNKNOWN,
            ConnState.NOT_RUNNING,
        )
    )
    network_rows = (
        "".join(
            f"<tr><td>{e(name)}</td><td class='num'>{e(format_duration_long(monitored))}</td>"
            f"<td class='num'>{e(format_duration(down, precise=True))}</td><td class='num'>{count}</td></tr>"
            for name, monitored, down, count in network_breakdown(r)
        )
        or "<tr><td colspan='4' class='empty'>No network names were recorded in this range.</td></tr>"
    )
    limitations = "".join(f"<li>{e(x)}</li>" for x in r.limitations)
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{e(APP_NAME)} report — {e(r.title)}</title>
<style>
:root {{ color-scheme: light dark; --bg:#ffffff; --fg:#1d1d1f; --muted:#6e6e73; --card:#f5f5f7; --line:#e5e5ea; }}
@media (prefers-color-scheme: dark) {{ :root {{ --bg:#1e2130; --fg:#f2f2f7; --muted:#9a9aa5; --card:#272b3b; --line:#353a4d; }} }}
body {{ font: 14px/1.5 system-ui, -apple-system, "Cantarell", "Segoe UI", sans-serif; background: var(--bg); color: var(--fg);
       max-width: 1040px; margin: 0 auto; padding: 32px 20px; }}
h1 {{ font-size: 24px; margin: 0 0 4px; }} h2 {{ font-size: 17px; margin: 32px 0 10px; }}
.muted {{ color: var(--muted); }}
.cards {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(190px, 1fr)); gap: 12px; margin-top: 20px; }}
.card {{ background: var(--card); border-radius: 12px; padding: 14px 16px; }}
.label {{ color: var(--muted); font-size: 13px; }} .value {{ font-size: 26px; font-weight: 700; font-variant-numeric: tabular-nums; }}
.good {{ color: #28a745; }} .warn {{ color: #f08c00; }}
.timeline {{ width: 100%; height: 36px; border-radius: 8px; overflow: hidden; display: block; }}
.axis {{ display: flex; justify-content: space-between; color: var(--muted); font-size: 12px; margin-top: 4px; }}
.legend {{ display: flex; flex-wrap: wrap; gap: 16px; margin-top: 10px; font-size: 13px; }}
.legend i {{ display: inline-block; width: 10px; height: 10px; border-radius: 50%; margin-right: 6px; }}
table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
th, td {{ text-align: left; padding: 7px 8px; border-bottom: 1px solid var(--line); vertical-align: top; }}
th {{ color: var(--muted); font-weight: 600; }} td.num {{ text-align: right; font-variant-numeric: tabular-nums; white-space: nowrap; }}
.dot {{ display: inline-block; width: 9px; height: 9px; border-radius: 50%; margin-right: 7px; }}
.empty {{ color: var(--muted); text-align: center; padding: 18px; }}
footer {{ margin-top: 40px; color: var(--muted); font-size: 12px; }}
</style></head><body>
<h1>{e(APP_NAME)} connectivity report</h1>
<div class="muted">{e(r.title)} · {e(format_iso_local(r.range_start))} → {e(format_iso_local(r.range_end))}<br>
Generated {e(format_iso_local(r.generated_at))} on {e(r.hostname)}</div>
<p class="muted">Monitored for {e(format_duration_long(s.monitored))} out of {e(format_duration_long(s.range_duration))}
({e(format_percent(s.coverage_pct, 1))} coverage).</p>
<div class="cards">
{card("Uptime", format_percent(s.uptime_pct), "good")}
{card("Total downtime", f"{format_duration(s.downtime)} ({format_percent(s.downtime_pct, 1)})", "warn")}
{card("Outages", str(s.outage_count))}
{card("Longest outage", (format_duration(lo.duration_in_range) + (" (ongoing)" if lo.ongoing else "")) if lo else "—")}
</div>
<h2>Event timeline</h2>
{_timeline_svg(r)}
<div class="axis"><span>{e(format_iso_local(r.range_start))}</span><span>{e(format_iso_local(r.range_end))}</span></div>
<div class="legend">{legend}</div>
<h2>Networks</h2>
<table><thead><tr><th>Network</th><th>Monitored</th><th>Downtime</th><th>Outages</th></tr></thead>
<tbody>{network_rows}</tbody></table>
<h2>Outages</h2>
<table><thead><tr><th>ID</th><th>Type</th><th>Start</th><th>End</th><th>Duration</th><th>Status</th><th>Network</th><th>Targets</th></tr></thead>
<tbody>{outage_rows}</tbody></table>
<h2>Events</h2>
<table><thead><tr><th>State</th><th>Start</th><th>End</th><th>Duration</th><th>Network</th><th>Details</th></tr></thead>
<tbody>{seg_rows}</tbody></table>
<h2>Limitations of the collected data</h2>
<ul>{limitations}</ul>
<footer>Generated by {e(APP_NAME)} {e(__version__)}. Uptime = online time ÷ monitored time; unmonitored time is
excluded from both uptime and downtime.</footer>
</body></html>
"""


RENDERERS = {"txt": render_txt, "csv": render_csv, "html": render_html}


def render(report: ReportData, fmt: str) -> str:
    return RENDERERS[fmt](report)


def write_report(path: str, content: str, fmt: str) -> None:
    encoding = "utf-8-sig" if fmt == "csv" else "utf-8"
    with open(path, "w", encoding=encoding, newline="" if fmt == "csv" else None) as fh:
        fh.write(content)


def default_filename(fmt: str, generated_at: Optional[float] = None, event: bool = False) -> str:
    stamp = time.strftime("%Y-%m-%d_%H%M", time.localtime(generated_at if generated_at is not None else time.time()))
    return f"netwatch-{'event' if event else 'report'}-{stamp}.{fmt}"


__all__ = ["FORMATS", "ReportData", "build_report", "render", "write_report", "default_filename", "DOWN_STATES"]
