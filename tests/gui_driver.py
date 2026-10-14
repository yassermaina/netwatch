"""Drives the real GTK application through scripted scenarios and prints JSON results.

Run by tests/test_gui.py inside ``dbus-run-session -- xvfb-run``.  Network
checks are replaced by a scripted adapter so no internet access is needed.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import netwatch.ui  # noqa: E402,F401
from gi.repository import Gio, GLib  # noqa: E402

from netwatch.models import ConnState, Lifecycle  # noqa: E402
from netwatch.storage.db import Database  # noqa: E402
from netwatch.storage.repository import Repository  # noqa: E402
from netwatch.settings import DEFAULT_TARGETS, SettingsStore  # noqa: E402
from netwatch.ui.application import NetWatchApplication  # noqa: E402
from tests.conftest import FakeAdapter, FakeProbe  # noqa: E402

DAY = 86400


def seed(path: Path) -> None:
    db = Database(path)
    db.migrate()
    repo = Repository(db)
    repo.ensure_default_targets(DEFAULT_TARGETS)
    SettingsStore(db).update({"interval_seconds": 1, "timeout_seconds": 1, "sound_enabled": False}, validate_all=False)
    now = time.time()
    # 10 days ago: 2 days monitored with one outage; 1 hour ago: one outage
    t0 = now - 10 * DAY
    s = repo.create_session(t0, {}, "test")
    a = repo.open_state(s, ConnState.ONLINE, t0, targets="1.1.1.1")
    repo.close_state(a, t0 + DAY)
    b = repo.open_state(s, ConnState.NO_INTERNET, t0 + DAY, targets="1.1.1.1, 8.8.8.8")
    repo.close_state(b, t0 + DAY + 600)
    o = repo.create_outage(s, ConnState.NO_INTERNET, t0 + DAY, t0 + DAY + 10, True, [])
    repo.close_outage(o, t0 + DAY + 600, "recovered", True, t0 + DAY + 610)
    c = repo.open_state(s, ConnState.ONLINE, t0 + DAY + 600)
    repo.close_state(c, t0 + 2 * DAY)
    repo.end_session(s, t0 + 2 * DAY)
    t1 = now - 7200
    s2 = repo.create_session(t1, {}, "test")
    a = repo.open_state(s2, ConnState.ONLINE, t1)
    repo.close_state(a, t1 + 3000)
    b = repo.open_state(s2, ConnState.NOT_CONNECTED, t1 + 3000)
    repo.close_state(b, t1 + 3120)
    o = repo.create_outage(s2, ConnState.NOT_CONNECTED, t1 + 3000, t1 + 3005, True, [])
    repo.close_outage(o, t1 + 3120, "recovered", True)
    c = repo.open_state(s2, ConnState.ONLINE, t1 + 3120)
    repo.close_state(c, t1 + 3600)
    repo.end_session(s2, t1 + 3600)
    db.close()


def seed_large(path: Path, n: int = 21000) -> None:
    db = Database(path)
    db.migrate()
    repo = Repository(db)
    repo.ensure_default_targets(DEFAULT_TARGETS)
    SettingsStore(db).update({"interval_seconds": 5, "sound_enabled": False}, validate_all=False)
    t = time.time() - 365 * DAY
    sid = repo.create_session(t, {}, "test")
    rows, outages = [], []
    for i in range(n):
        st = ConnState.ONLINE if i % 2 == 0 else ConnState.NO_INTERNET
        d = 1400.0 if st == ConnState.ONLINE else 90.0
        rows.append((sid, st.value, t, t + d))
        if st != ConnState.ONLINE:
            outages.append((sid, st.value, t, t, t + d, d, "recovered", 1, 1, "[]"))
        t += d
    with repo.tx() as conn:
        conn.executemany(
            "INSERT INTO connection_states(session_id, state, started_at, ended_at) VALUES (?,?,?,?)", rows
        )
        conn.executemany(
            "INSERT INTO outages(session_id, classification, started_at, detected_at, ended_at, duration, "
            "status, start_observed, end_observed, evidence) VALUES (?,?,?,?,?,?,?,?,?,?)",
            outages,
        )
    repo.end_session(sid, t)
    db.close()


class TestApp(NetWatchApplication):
    def _init_backend(self) -> None:
        super()._init_backend()
        self.fake = FakeAdapter(time.time)
        self.engine.adapters = {"icmp": self.fake}
        self.engine.probe = FakeProbe()


def run_scenarios(db_path: Path, mode: str) -> dict:
    results: dict = {}
    app = TestApp(db_path, non_unique=True)
    steps = []

    def step(fn):
        steps.append(fn)
        return fn

    def wait_until(cond, timeout=8.0):
        deadline = time.time() + timeout
        ctx = GLib.MainContext.default()
        while time.time() < deadline:
            while ctx.pending():
                ctx.iteration(False)
            if cond():
                return True
            time.sleep(0.02)
        return False

    def history():
        return app.window.pages["history"]

    if mode == "full":

        @step
        def periods():
            app.window.navigate("history")
            h = history()
            assert wait_until(lambda: h.data is not None)
            all_count = h.data.summary.outage_count
            all_label = h.tile_outages.value.get_label()
            all_days = len(h.calendar.days)
            h.set_period("24h")
            assert wait_until(
                lambda: h.data is not None and h.period == "24h" and h.data.summary.range_duration < DAY + 60
            )
            results["periods"] = {
                "all_outages": all_count,
                "all_tile": all_label,
                "all_days": all_days,
                "day_outages": h.data.summary.outage_count,
                "day_tile": h.tile_outages.value.get_label(),
                "day_days": len(h.calendar.days),
                "timeline_start_matches": abs(h.timeline.start - h.data.summary.range_start) < 1,
                "coverage_text": h.coverage_label.get_label(),
            }
            h.set_period("all")
            assert wait_until(lambda: h.period == "all" and h.data.summary.range_duration > 9 * DAY)

        @step
        def timeline_selection():
            h = history()
            down = [s for s in h.data.segments if s.state == ConnState.NO_INTERNET][0]
            h._on_timeline_activate(down)
            row = h.listbox.get_selected_row()
            results["timeline"] = {
                "selected_key": row.seg.key if row else None,
                "expected_key": down.key,
                "outage_linked": down.outage_id is not None,
            }

        @step
        def calendar_filter():
            h = history()
            day = next(d for d in h.data.days if d.downtime > 0)
            h.calendar.selected = day.day
            h._on_day_selected(day)
            from netwatch.timefmt import day_start_of
            from datetime import timedelta

            ds, de = day_start_of(day.day), day_start_of(day.day + timedelta(days=1))
            ok = all(s.end > ds and s.start < de for s in h.filtered)
            results["calendar"] = {
                "filtered": len(h.filtered),
                "all_overlap_day": ok,
                "day_outages": day.outage_count,
                "chip_visible": h.day_chip.get_visible(),
            }
            h._on_day_selected(None, from_calendar=False)
            results["calendar"]["cleared"] = len(h.filtered) == len(h.events)

        @step
        def search_and_state_filter():
            h = history()
            h.state_filter.set_selected(1)  # outages only
            only_down = all(s.state.is_down for s in h.filtered)
            h.state_filter.set_selected(0)
            h.search.set_text("not running")
            h._apply_filters()
            results["filters"] = {
                "outages_only": only_down and len(h.filtered) >= 0,
                "search_hits": len(h.filtered),
                "search_all_match": all(s.state == ConnState.NOT_RUNNING for s in h.filtered),
            }
            h.search.set_text("")
            h._apply_filters()

        @step
        def live_outage_and_hide():
            eng = app.engine
            assert wait_until(lambda: eng.tick_count >= 2 and eng.snapshot.state == ConnState.ONLINE)
            for t in app.repo.list_targets():
                app.fake.set(t.address, False)
            assert wait_until(lambda: eng.snapshot.state == ConnState.NO_INTERNET)
            app.set_muted(True)
            app.window.close()  # hide on close
            hidden = not app.window.get_visible()
            before = eng.tick_count
            wait_until(lambda: eng.tick_count >= before + 2, timeout=5)
            for t in app.repo.list_targets():
                app.fake.set(t.address, True)
            assert wait_until(lambda: eng.snapshot.state == ConnState.ONLINE)
            outages = app.repo.db.query("SELECT * FROM outages WHERE session_id = ?", (eng.session_id,))
            results["background"] = {
                "hidden": hidden,
                "ticks_while_hidden": eng.tick_count - before,
                "lifecycle": eng.lifecycle.value,
                "muted": bool(app.settings.get("muted")),
                "outages_recorded_while_muted": len(outages),
                "outage_recovered": bool(outages) and outages[-1]["status"] == "recovered",
                "alert_plays": list(app.alerts.plays),
            }
            app.show_window()
            app.set_muted(False)

        @step
        def pause_resume():
            app.toggle_pause()
            assert wait_until(lambda: app.engine.lifecycle == Lifecycle.PAUSED)
            app.toggle_pause()
            assert wait_until(lambda: app.engine.lifecycle == Lifecycle.RUNNING)
            paused = app.repo.db.query("SELECT COUNT(*) n FROM connection_states WHERE state = 'paused'")[0]["n"]
            results["pause"] = {"paused_states": paused}

        @step
        def tray():
            results["tray"] = {"available": bool(app.tray and app.tray.available)}
            if app.tray and app.tray.available:
                conn = Gio.DBusConnection.new_for_address_sync(
                    Gio.dbus_address_get_for_bus_sync(Gio.BusType.SESSION, None),
                    Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION,
                    None,
                    None,
                )

                def call(path, iface, method, params):
                    # async + main loop iteration: the tray answers from this same thread
                    box = {}
                    conn.call(
                        app.tray.service,
                        path,
                        iface,
                        method,
                        params,
                        None,
                        Gio.DBusCallFlags.NONE,
                        3000,
                        None,
                        lambda c, res: box.setdefault("r", c.call_finish(res)),
                    )
                    assert wait_until(lambda: "r" in box, timeout=4)
                    return box["r"].unpack()

                layout = call("/MenuBar", "com.canonical.dbusmenu", "GetLayout", GLib.Variant("(iias)", (0, -1, [])))
                labels = [child[1].get("label", "") for child in layout[1][2]]
                muted_before = bool(app.settings.get("muted"))
                call(
                    "/MenuBar",
                    "com.canonical.dbusmenu",
                    "Event",
                    GLib.Variant("(isvu)", (5, "clicked", GLib.Variant("s", ""), 0)),
                )
                wait_until(lambda: bool(app.settings.get("muted")) != muted_before, timeout=3)
                icon = call(
                    "/StatusNotifierItem",
                    "org.freedesktop.DBus.Properties",
                    "Get",
                    GLib.Variant("(ss)", ("org.kde.StatusNotifierItem", "IconName")),
                )[0]
                results["tray"].update(
                    {"labels": labels, "mute_toggled": bool(app.settings.get("muted")) != muted_before, "icon": icon}
                )
                app.set_muted(False)

        @step
        def event_detail():
            from netwatch.ui.event_detail import load_detail, show_event_detail

            h = history()
            down = next(seg for seg in h.data.segments if seg.state == ConnState.NO_INTERNET)
            show_event_detail(app, h, down)  # builds the dialog; data loads asynchronously
            wait_until(lambda: False, timeout=1.0)
            data = load_detail(app, down)
            results["detail"] = {"outage_id": data["outage"].id if data["outage"] else None,
                                 "expected": down.outage_id}

        @step
        def settings_validation():
            page = app.window.pages["settings"]
            app.window.navigate("settings")
            row_interval = page.fields["interval_seconds"][0]
            row_timeout = page.fields["timeout_seconds"][0]
            row_timeout.set_value(5)
            row_interval.set_value(3)  # timeout >= interval -> invalid
            invalid_blocked = not page.save_btn.get_sensitive() and "shorter" in page.status.get_label()
            row_timeout.set_value(2)
            valid_enabled = page.save_btn.get_sensitive()
            saved = page.save()
            results["settings"] = {"invalid_blocked": invalid_blocked, "valid_enabled": valid_enabled,
                                   "saved": saved, "interval": app.settings.get("interval_seconds"),
                                   "unsaved_after": page.has_unsaved()}
            row_interval.set_value(1)
            row_timeout.set_value(1)
            page.save()

        @step
        def export_reports():
            from netwatch import reports

            out = {}
            for fmt in ("txt", "csv", "html"):
                data = reports.build_report(app.repo, app.stats, app.settings, "all")
                path = db_path.parent / f"report.{fmt}"
                reports.write_report(str(path), reports.render(data, fmt), fmt)
                out[fmt] = path.stat().st_size
            results["reports"] = out

        @step
        def all_pages():
            errors = []
            for key in ("overview", "performance", "reports", "settings", "about", "history"):
                try:
                    app.window.navigate(key)
                    wait_until(lambda: False, timeout=0.6)
                except Exception as exc:  # pragma: no cover
                    errors.append(f"{key}: {exc}")
            results["pages"] = {"errors": errors, "back": app.window.go_back()}
    elif mode == "large":

        @step
        def large():
            gaps = []
            last = [time.monotonic()]

            def beat():
                now = time.monotonic()
                gaps.append(now - last[0])
                last[0] = now
                return True

            GLib.timeout_add(20, beat)
            h = history()
            started = time.monotonic()
            app.window.navigate("history")
            assert wait_until(lambda: h.data is not None and len(h.events) > 20000, timeout=30)
            load = time.monotonic() - started
            stall_load = max(gaps)
            gaps.clear()
            for key in ("30d", "7d", "all"):
                h.set_period(key)
                assert wait_until(
                    lambda key=key: h.period == key and h.data is not None and not h.spinner.get_spinning(), timeout=30
                )
            stall_periods = max(gaps)
            gaps.clear()
            last[0] = time.monotonic()
            h._on_timeline_activate(h.events[-1])  # oldest event: forces incremental loading up to it
            assert wait_until(lambda: h.listbox.get_selected_row() is not None, timeout=30)
            wait_until(lambda: False, timeout=0.3)
            selected = h.listbox.get_selected_row()
            newer_visible = h.newer_button.get_visible()
            h._render_newer()
            results["large"] = {
                "events": len(h.events),
                "load_seconds": round(load, 2),
                "newer_visible": newer_visible,
                "rows_built": len(h.rows),
                "max_stall_seconds": round(max(stall_load, stall_periods, max(gaps)), 3),
                "stalls": [round(stall_load, 3), round(stall_periods, 3), round(max(gaps), 3)],
                "rendered": h.rendered,
                "selected_oldest": bool(selected and selected.seg.key == h.events[-1].key),
            }
    else:  # empty

        @step
        def empty():
            app.window.navigate("history")
            h = history()
            assert wait_until(lambda: h.data is not None)
            results["empty"] = {
                "status_visible": h.empty.get_visible(),
                "log_visible": h.log_section.get_visible(),
                "uptime_tile": h.tile_uptime.value.get_label(),
                "lifecycle": app.engine.lifecycle.value,
            }

    def run_steps() -> bool:
        try:
            for fn in steps:
                fn()
        except Exception:
            import traceback

            results["error"] = traceback.format_exc()
        results["runner_active_before_quit"] = app.runner.active_count()
        app.quit()
        return False

    GLib.timeout_add(800, run_steps)
    app.run([sys.argv[0]])
    db = Database(db_path)
    rows = db.query("SELECT status, end_reason FROM monitoring_sessions ORDER BY id")
    results["sessions"] = [(r["status"], r["end_reason"]) for r in rows]
    results["open_states"] = db.query_one("SELECT COUNT(*) n FROM connection_states WHERE ended_at IS NULL")["n"]
    db.close()
    results["lifecycle_after_quit"] = app.engine.lifecycle.value
    results["runner_active_after_quit"] = app.runner.active_count()
    import threading

    results["monitor_threads"] = [t.name for t in threading.enumerate() if t.name == "netwatch-monitor"]
    return results


class FakeWatcher:
    """Minimal org.kde.StatusNotifierWatcher on its own bus connection."""

    XML = """<node><interface name="org.kde.StatusNotifierWatcher">
      <method name="RegisterStatusNotifierItem"><arg type="s" direction="in"/></method>
    </interface></node>"""

    def __init__(self):
        self.items = []
        self.conn = Gio.DBusConnection.new_for_address_sync(
            Gio.dbus_address_get_for_bus_sync(Gio.BusType.SESSION, None),
            Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION,
            None,
            None,
        )
        info = Gio.DBusNodeInfo.new_for_xml(self.XML).interfaces[0]
        self.conn.register_object("/StatusNotifierWatcher", info, self._call, None, None)
        self.conn.call_sync(
            "org.freedesktop.DBus",
            "/org/freedesktop/DBus",
            "org.freedesktop.DBus",
            "RequestName",
            GLib.Variant("(su)", ("org.kde.StatusNotifierWatcher", 4)),
            None,
            Gio.DBusCallFlags.NONE,
            3000,
            None,
        )

    def _call(self, conn, sender, path, iface, method, params, invocation):
        self.items.append(params.unpack()[0])
        invocation.return_value(None)


if __name__ == "__main__":
    db_path = Path(sys.argv[1])
    mode = sys.argv[2]
    with_tray = len(sys.argv) > 3 and sys.argv[3] == "tray"
    if mode == "full":
        seed(db_path)
    if mode == "large":
        seed_large(db_path)
    watcher = FakeWatcher() if with_tray else None
    if mode == "empty":
        d = Database(db_path)
        d.migrate()
        SettingsStore(d).update({"start_monitoring_on_launch": False}, validate_all=False)
        d.close()
    out = run_scenarios(db_path, mode)
    if watcher:
        out["watcher_items"] = watcher.items
    print("RESULT " + json.dumps(out))
