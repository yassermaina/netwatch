"""Report export flow shared by the History page, Reports page, event details and Ctrl+S."""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Optional

from gi.repository import Gio, GLib, Gtk

from netwatch import reports
from netwatch.paths import default_reports_dir
from netwatch.stats import Segment
from netwatch.ui import dialogs
from netwatch.ui.background import run_async

if TYPE_CHECKING:
    from netwatch.ui.application import NetWatchApplication


def reports_dir(app: "NetWatchApplication") -> Path:
    configured = app.settings.get("reports_dir")
    return Path(configured).expanduser() if configured else default_reports_dir()


def open_path(window: Gtk.Window, path: Path) -> None:
    gfile = Gio.File.new_for_path(str(path))
    if hasattr(Gtk, "FileLauncher"):
        Gtk.FileLauncher.new(gfile).launch(window, None, None)
    else:
        Gio.AppInfo.launch_default_for_uri(gfile.get_uri(), None)


def start_export(
    app: "NetWatchApplication",
    window: Gtk.Window,
    period: str = "all",
    custom: Optional[tuple[float, float]] = None,
    focus: Optional[Segment] = None,
    fmt: Optional[str] = None,
    on_saved: Optional[Callable[[Path], None]] = None,
) -> None:
    if fmt is None:
        what = "this event" if focus else "the selected period"
        dialogs.choose(
            window, "Export Report", f"Choose a format for a report covering {what}.",
            [("cancel", "Cancel", ""), ("txt", "Text", ""), ("csv", "CSV", ""), ("html", "HTML", "suggested")],
            lambda rid: rid != "cancel" and start_export(app, window, period, custom, focus, rid, on_saved),
            default="html",
        )
        return
    folder = reports_dir(app)
    try:
        folder.mkdir(parents=True, exist_ok=True)
    except OSError:
        folder = Path.home()
    name = reports.default_filename(fmt, event=focus is not None)
    dialog = Gtk.FileDialog(title="Save Report", modal=True, initial_name=name,
                            initial_folder=Gio.File.new_for_path(str(folder)))
    flt = Gtk.FileFilter()
    flt.set_name(reports.FORMATS[fmt])
    flt.add_suffix(fmt)
    filters = Gio.ListStore.new(Gtk.FileFilter)
    filters.append(flt)
    dialog.set_filters(filters)

    def chosen(dlg, result) -> None:
        try:
            gfile = dlg.save_finish(result)
        except GLib.Error as err:
            if not err.matches(Gtk.dialog_error_quark(), Gtk.DialogError.DISMISSED) and \
                    not err.matches(Gtk.dialog_error_quark(), Gtk.DialogError.CANCELLED):
                window.toast(f"Could not open the file chooser: {err.message}")
            return
        path = gfile.get_path()
        if not path:
            window.toast("Please choose a local file.")
            return
        if not path.lower().endswith(f".{fmt}"):
            path += f".{fmt}"
            if os.path.exists(path):
                dialogs.confirm(window, "Replace existing file?", f"{os.path.basename(path)} already exists.",
                                "Replace", lambda: write(path))
                return
        write(path)

    def write(path: str) -> None:
        def work():
            data = reports.build_report(app.repo, app.stats, app.settings, period, custom, focus)
            reports.write_report(path, reports.render(data, fmt), fmt)
            return Path(path)

        def done(p: Path) -> None:
            window.toast(f"Report saved: {p.name}", button=("Open", lambda: open_path(window, p)))
            app.repo.log_event("report_exported", f"Exported {fmt.upper()} report", data={"path": str(p)})
            if on_saved:
                on_saved(p)

        run_async(work, done, lambda exc: dialogs.message(window, "Export failed", str(exc)))

    dialog.save(window, None, chosen)
