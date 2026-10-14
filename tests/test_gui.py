"""End-to-end GUI scenarios against the real GTK application (Xvfb + private D-Bus session).

Skipped automatically when GTK4/Libadwaita, xvfb-run or dbus-run-session are unavailable.
"""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _gtk_available() -> bool:
    code = (
        "import gi; gi.require_version('Gtk','4.0'); gi.require_version('Adw','1'); "
        "gi.require_foreign('cairo'); from gi.repository import Gtk, Adw"
    )
    return subprocess.run([sys.executable, "-c", code], capture_output=True).returncode == 0


pytestmark = pytest.mark.skipif(
    not (shutil.which("xvfb-run") and shutil.which("dbus-run-session") and _gtk_available()),
    reason="needs GTK4/Libadwaita, xvfb-run and dbus-run-session",
)


def run_driver(tmp_path, mode, tray=False):
    env = dict(os.environ, GTK_A11Y="none", NO_AT_BRIDGE="1")
    for var in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME", "XDG_STATE_HOME"):
        env[var] = str(tmp_path / var.lower())
    cmd = [
        "dbus-run-session",
        "--",
        "xvfb-run",
        "-a",
        sys.executable,
        str(ROOT / "tests" / "gui_driver.py"),
        str(tmp_path / f"{mode}.db"),
        mode,
    ] + (["tray"] if tray else [])
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=180, env=env, cwd=ROOT)
    line = next((ln for ln in proc.stdout.splitlines() if ln.startswith("RESULT ")), None)
    assert line, f"driver produced no result\nstdout:{proc.stdout[-2000:]}\nstderr:{proc.stderr[-4000:]}"
    result = json.loads(line[len("RESULT ") :])
    assert "error" not in result, result["error"]
    return result


@pytest.fixture(scope="module")
def full(tmp_path_factory):
    return run_driver(tmp_path_factory.mktemp("gui_full"), "full", tray=True)


def test_period_change_updates_all_views(full):
    p = full["periods"]
    assert p["all_outages"] == 2 and p["all_tile"] == "2"
    assert p["day_outages"] == 1 and p["day_tile"] == "1"
    assert p["all_days"] > p["day_days"]
    assert p["timeline_start_matches"]
    assert "out of 1d 0h" in p["coverage_text"]


def test_timeline_click_selects_matching_event(full):
    t = full["timeline"]
    assert t["selected_key"] == t["expected_key"] and t["outage_linked"]


def test_calendar_click_filters_event_log(full):
    c = full["calendar"]
    assert c["filtered"] > 0 and c["all_overlap_day"] and c["chip_visible"] and c["cleared"]


def test_event_log_filters(full):
    f = full["filters"]
    assert f["outages_only"] and f["search_hits"] >= 1 and f["search_all_match"]


def test_monitoring_continues_while_hidden_and_muted(full):
    b = full["background"]
    assert b["hidden"] and b["ticks_while_hidden"] >= 2 and b["lifecycle"] == "running"
    assert b["muted"] and b["outages_recorded_while_muted"] == 1 and b["outage_recovered"]
    assert b["alert_plays"] == []  # muted (and sound disabled in this profile)


def test_pause_resume_from_ui(full):
    assert full["pause"]["paused_states"] == 1


def test_tray_menu_over_dbus(full):
    t = full["tray"]
    assert t["available"] and full["watcher_items"]
    assert t["labels"][0] == "Open NetWatch" and "Quit NetWatch" in t["labels"]
    assert t["mute_toggled"] and t["icon"].endswith("online-symbolic")


def test_event_detail_refers_to_selected_outage(full):
    assert full["detail"]["outage_id"] == full["detail"]["expected"] is not None


def test_settings_validation_and_save(full):
    st = full["settings"]
    assert st["invalid_blocked"] and st["valid_enabled"] and st["saved"]
    assert st["interval"] == 3 and not st["unsaved_after"]


def test_reports_and_pages(full):
    assert all(size > 500 for size in full["reports"].values())
    assert full["pages"]["errors"] == []


def test_clean_exit_without_orphans(full):
    assert full["lifecycle_after_quit"] == "stopped"
    assert full["runner_active_after_quit"] == 0 and full["monitor_threads"] == []
    assert full["open_states"] == 0
    assert full["sessions"][-1] == ["stopped", "quit"]


def test_missing_tray_does_not_prevent_monitoring(tmp_path):
    r = run_driver(tmp_path, "full", tray=False)
    assert r["tray"]["available"] is False
    assert r["background"]["ticks_while_hidden"] >= 2 and r["background"]["outage_recovered"]


def test_empty_history_state(tmp_path):
    r = run_driver(tmp_path, "empty")
    e = r["empty"]
    assert e["status_visible"] and not e["log_visible"]
    assert e["uptime_tile"] == "—" and e["lifecycle"] == "stopped"


def test_large_history_keeps_ui_responsive(tmp_path):
    r = run_driver(tmp_path, "large")
    big = r["large"]
    assert big["events"] > 20000
    assert big["selected_oldest"]  # timeline selection jumps to the oldest event
    assert big["newer_visible"] and big["rows_built"] < 300  # only a window of rows is ever built
    # heavy work runs on a worker thread; the main loop must never stall for long
    assert big["max_stall_seconds"] < 1.5, big
