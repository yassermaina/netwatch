"""Reliability statistics.

Calculation rules (used by every screen and report):

* Monitored time = ONLINE + NO_INTERNET + NOT_CONNECTED time.
* UNKNOWN (running without enough evidence), PAUSED and NOT_RUNNING time are
  *not* monitored: they count neither as uptime nor as downtime and are
  reported separately as coverage gaps.
* Uptime % = ONLINE / monitored × 100.  Downtime % = (NO_INTERNET +
  NOT_CONNECTED) / monitored × 100.
* Degraded connectivity (high latency / some packet loss while at least one
  target responds) is ONLINE for uptime purposes; quality is reported
  separately on the Performance page.
* State periods start at the first check supporting the new state and end at
  the first check supporting the next one; all times come from stored records.
* Overlapping records (possible after a backwards clock change) are trimmed
  so no instant is counted twice.
"""

from __future__ import annotations

import bisect
import json
import statistics
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Optional

from netwatch.models import DOWN_STATES, MONITORED_STATES, ConnState, LocalNetworkStatus
from netwatch.storage.repository import Repository
from netwatch.timefmt import day_start_of

MIN_GAP_SECONDS = 1.0


@dataclass
class Segment:
    state: ConnState
    start: float
    end: float
    ongoing: bool = False
    state_id: Optional[int] = None
    session_id: Optional[int] = None
    targets: Optional[str] = None
    interface: Optional[str] = None
    reason: Optional[str] = None
    synthetic: bool = False  # generated for a gap (app not running)
    outage_id: Optional[int] = None

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)

    @property
    def local(self) -> Optional[LocalNetworkStatus]:
        return LocalNetworkStatus.from_json(self.interface)

    @property
    def key(self) -> str:
        return f"s{self.state_id}" if self.state_id is not None else f"g{self.start:.3f}"


@dataclass
class OutageInfo:
    id: int
    classification: ConnState
    start: float
    end: float
    ongoing: bool
    status: str
    start_observed: bool
    end_observed: bool
    detected_at: float
    recovery_detected_at: Optional[float]
    duration_in_range: float
    full_duration: float
    evidence: list = field(default_factory=list)
    targets: list = field(default_factory=list)


@dataclass
class PeriodSummary:
    range_start: float
    range_end: float
    durations: dict
    outages: list
    has_data: bool

    @property
    def range_duration(self) -> float:
        return max(0.0, self.range_end - self.range_start)

    @property
    def online(self) -> float:
        return self.durations.get(ConnState.ONLINE, 0.0)

    @property
    def downtime(self) -> float:
        return sum(self.durations.get(s, 0.0) for s in DOWN_STATES)

    @property
    def monitored(self) -> float:
        return sum(self.durations.get(s, 0.0) for s in MONITORED_STATES)

    @property
    def unknown(self) -> float:
        return self.durations.get(ConnState.UNKNOWN, 0.0)

    @property
    def paused(self) -> float:
        return self.durations.get(ConnState.PAUSED, 0.0)

    @property
    def not_running(self) -> float:
        return self.durations.get(ConnState.NOT_RUNNING, 0.0)

    @property
    def coverage_pct(self) -> Optional[float]:
        return self.monitored / self.range_duration * 100 if self.range_duration > 0 else None

    @property
    def uptime_pct(self) -> Optional[float]:
        return self.online / self.monitored * 100 if self.monitored > 0 else None

    @property
    def downtime_pct(self) -> Optional[float]:
        return self.downtime / self.monitored * 100 if self.monitored > 0 else None

    @property
    def outage_count(self) -> int:
        return len(self.outages)

    @property
    def longest_outage(self) -> Optional[OutageInfo]:
        return max(self.outages, key=lambda o: o.duration_in_range, default=None)


@dataclass
class DayStats:
    day: date
    start: float
    end: float
    durations: dict
    outage_count: int
    in_range: bool = True

    @property
    def monitored(self) -> float:
        return sum(self.durations.get(s, 0.0) for s in MONITORED_STATES)

    @property
    def downtime(self) -> float:
        return sum(self.durations.get(s, 0.0) for s in DOWN_STATES)

    @property
    def span(self) -> float:
        return max(0.0, self.end - self.start)

    @property
    def coverage_pct(self) -> float:
        return self.monitored / self.span * 100 if self.span > 0 else 0.0

    @property
    def category(self) -> str:
        """Heat-map class: online | minor | no_internet | major | not_connected | unmonitored | outside."""
        if not self.in_range or self.span <= 0:
            return "outside"
        mon = self.monitored
        if mon <= 0:
            return "unmonitored"
        down = self.downtime
        if down <= 0:
            return "online"
        frac = down / mon
        ni = self.durations.get(ConnState.NO_INTERNET, 0.0)
        nc = self.durations.get(ConnState.NOT_CONNECTED, 0.0)
        if nc > ni and frac >= 0.5:
            return "not_connected"
        if frac < 0.01:
            return "minor"
        if frac >= 0.10:
            return "major"
        return "no_internet" if ni >= nc else "not_connected"


@dataclass
class HistoryData:
    summary: PeriodSummary
    segments: list
    days: list
    outages_by_id: dict


class ReliabilityService:
    def __init__(self, repo: Repository):
        self.repo = repo

    # ------------------------------------------------------------ ranges
    def resolve_range(self, period: str, now: float) -> tuple[float, float]:
        if period == "24h":
            return now - 86400, now
        if period == "7d":
            return now - 7 * 86400, now
        if period == "30d":
            return now - 30 * 86400, now
        earliest = self.repo.earliest_record()
        return (earliest if earliest is not None and earliest < now else now - 86400), now

    # ------------------------------------------------------------ segments
    def segments(self, start: float, end: float, now: float, include_gaps: bool = True) -> list[Segment]:
        """Chronological, non-overlapping segments clipped to [start, end]."""
        end = min(end, now)
        rows = self.repo.states_between(start, end)
        segs: list[Segment] = []
        cursor = start
        for row in rows:
            s = max(row["started_at"], start)
            raw_end = row["ended_at"]
            ongoing = raw_end is None
            e = min(now if ongoing else raw_end, end)
            if segs:
                s = max(s, segs[-1].end)
            if e <= s:
                continue
            if include_gaps and s - cursor >= MIN_GAP_SECONDS:
                segs.append(Segment(ConnState.NOT_RUNNING, cursor, s, synthetic=True))
            try:
                state = ConnState(row["state"])
            except ValueError:
                state = ConnState.UNKNOWN
            segs.append(
                Segment(
                    state, s, e, ongoing=ongoing and e >= now - 1e-6, state_id=row["id"], session_id=row["session_id"],
                    targets=row["targets"], interface=row["interface"], reason=row["reason"],
                )
            )
            cursor = max(cursor, e)
        if include_gaps and end - cursor >= MIN_GAP_SECONDS:
            segs.append(Segment(ConnState.NOT_RUNNING, cursor, end, ongoing=True, synthetic=True))
        return segs

    @staticmethod
    def durations(segments: list[Segment]) -> dict:
        totals = {s: 0.0 for s in ConnState}
        for seg in segments:
            totals[seg.state] += seg.duration
        return totals

    # ------------------------------------------------------------ outages
    def outages(self, start: float, end: float, now: float) -> list[OutageInfo]:
        rows = self.repo.outages_between(start, min(end, now))
        targets = self.repo.all_outage_targets([r["id"] for r in rows])
        result = []
        for r in rows:
            ongoing = r["ended_at"] is None
            o_end = now if ongoing else r["ended_at"]
            in_range = max(0.0, min(o_end, end) - max(r["started_at"], start))
            try:
                evidence = json.loads(r["evidence"] or "[]")
            except ValueError:
                evidence = []
            result.append(
                OutageInfo(
                    id=r["id"],
                    classification=ConnState(r["classification"]),
                    start=r["started_at"],
                    end=o_end,
                    ongoing=ongoing,
                    status=r["status"],
                    start_observed=bool(r["start_observed"]),
                    end_observed=bool(r["end_observed"]),
                    detected_at=r["detected_at"],
                    recovery_detected_at=r["recovery_detected_at"],
                    duration_in_range=in_range,
                    full_duration=max(0.0, o_end - r["started_at"]),
                    evidence=evidence,
                    targets=[dict(t) for t in targets.get(r["id"], [])],
                )
            )
        return result

    # ------------------------------------------------------------ summaries
    def summary(self, start: float, end: float, now: float, segments: Optional[list[Segment]] = None) -> PeriodSummary:
        end = min(end, now)
        segs = segments if segments is not None else self.segments(start, end, now)
        totals = self.durations(segs)
        outages = self.outages(start, end, now)
        has_data = any(not s.synthetic for s in segs)
        return PeriodSummary(start, end, totals, outages, has_data)

    def daily(self, start: float, end: float, segments: list[Segment], outages: list[OutageInfo],
              now: float) -> list[DayStats]:
        """Per local calendar day statistics, padded to whole weeks (Mon–Sun)."""
        first = datetime.fromtimestamp(start).date()
        last = datetime.fromtimestamp(min(end, now)).date()
        grid_start = first - timedelta(days=first.weekday())
        grid_end = last + timedelta(days=6 - last.weekday())
        days: list[DayStats] = []
        d = grid_start
        while d <= grid_end:
            ds, de = day_start_of(d), day_start_of(d + timedelta(days=1))
            in_range = d >= first and d <= last
            days.append(DayStats(d, max(ds, start), min(de, end, now), {s: 0.0 for s in ConnState}, 0, in_range))
            d += timedelta(days=1)
        index_by_day = {x.day: i for i, x in enumerate(days)}
        for seg in segments:
            t = seg.start
            while t < seg.end:
                day = datetime.fromtimestamp(t).date()
                i = index_by_day.get(day)
                boundary = day_start_of(day + timedelta(days=1))
                chunk_end = min(seg.end, boundary)
                if i is not None:
                    days[i].durations[seg.state] += chunk_end - t
                t = chunk_end
        for o in outages:
            i = index_by_day.get(datetime.fromtimestamp(max(o.start, start)).date())
            if i is not None:
                days[i].outage_count += 1
        return days

    def history(self, period: str, now: float, custom: Optional[tuple[float, float]] = None) -> HistoryData:
        start, end = custom if custom else self.resolve_range(period, now)
        segs = self.segments(start, end, now)
        summary = self.summary(start, end, now, segs)
        outages_by_id = {o.id: o for o in summary.outages}
        # link down segments to their outage
        starts = [o.start for o in summary.outages]
        for seg in segs:
            if seg.state in DOWN_STATES and summary.outages:
                i = bisect.bisect_right(starts, seg.start + 1e-6) - 1
                if i >= 0 and summary.outages[i].end >= seg.start:
                    seg.outage_id = summary.outages[i].id
        days = self.daily(start, end, segs, summary.outages, now)
        return HistoryData(summary, segs, days, outages_by_id)

    # ------------------------------------------------------------ performance
    def performance(self, start: float, end: float, buckets: int = 240) -> dict:
        span = max(1.0, end - start)
        bucket = max(1.0, span / buckets)
        rows = self.repo.measurement_buckets(start, end, bucket)
        series: dict[int, list] = {}
        for r in rows:
            series.setdefault(r["target_id"], []).append(
                {
                    "t": start + (r["b"] + 0.5) * bucket,
                    "n": r["n"],
                    "ok": r["ok"] or 0,
                    "avg": r["avg_ms"],
                    "min": r["min_ms"],
                    "max": r["max_ms"],
                }
            )
        values: dict[int, list[float]] = {}
        for r in self.repo.latency_values(start, end):
            values.setdefault(r["target_id"], []).append(r["latency_ms"])
        counts = {r["target_id"]: r for r in self.repo.check_counts(start, end)}
        per_target = {}
        all_values: list[float] = []
        for tid in set(values) | set(counts):
            v = values.get(tid, [])
            all_values.extend(v)
            c = counts.get(tid)
            n = c["n"] if c else 0
            ok = (c["ok"] or 0) if c else 0
            per_target[tid] = _latency_stats(v) | {"checks": n, "ok": ok,
                                                   "loss_pct": (n - ok) / n * 100 if n else None}
        total_n = sum(p["checks"] for p in per_target.values())
        total_ok = sum(p["ok"] for p in per_target.values())
        overall = _latency_stats(all_values) | {
            "checks": total_n, "ok": total_ok, "loss_pct": (total_n - total_ok) / total_n * 100 if total_n else None
        }
        return {"start": start, "end": end, "bucket": bucket, "series": series, "per_target": per_target,
                "overall": overall}

    def recent_quality(self, now: float, window: float = 300) -> dict:
        counts = self.repo.check_counts(now - window, now + 1)
        n = sum(r["n"] for r in counts)
        ok = sum(r["ok"] or 0 for r in counts)
        values = [r["latency_ms"] for r in self.repo.latency_values(now - window, now + 1)]
        return {"checks": n, "loss_pct": (n - ok) / n * 100 if n else None,
                "median_ms": statistics.median(values) if values else None}


def _latency_stats(values: list[float]) -> dict:
    if not values:
        return {"avg": None, "median": None, "min": None, "max": None, "p95": None, "jitter": None}
    ordered = sorted(values)
    p95 = ordered[min(len(ordered) - 1, int(round(0.95 * (len(ordered) - 1))))]
    jitter = statistics.pstdev(ordered) if len(ordered) > 1 else 0.0
    return {"avg": sum(ordered) / len(ordered), "median": statistics.median(ordered), "min": ordered[0],
            "max": ordered[-1], "p95": p95, "jitter": jitter}
